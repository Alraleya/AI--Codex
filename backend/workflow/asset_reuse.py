"""Deterministic discovery and confirmation of cross-episode design reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.core.assets import resolve_asset_path, validate_image, validate_text
from backend.core.status import StatusStore, utc_now


DESIGN_STAGE_BY_TYPE = {
    "character": "character_design",
    "scene": "visual_design",
    "prop": "visual_design",
}
DESIGN_ROLE_BY_TYPE = {
    "character": "character_sheet",
    "scene": "scene_sheet",
    "prop": "prop_sheet",
}
MANIFEST_COLLECTION_BY_TYPE = {
    "character": "characters",
    "scene": "scenes",
    "prop": "props",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_relative(workspace_root: Path, path: Path) -> str:
    root = Path(workspace_root).resolve()
    target = Path(path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("reuse source escapes workspace") from error


def resolve_reuse_source_path(workspace_root: Path, relative_path: str) -> Path:
    root = Path(workspace_root).resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("reuse source escapes workspace") from error
    return target


def _valid_source_triplet(
    image_path: Path,
    spec_path: Path,
    prompt_path: Path,
    *,
    expected_image_sha256: Optional[str] = None,
) -> Optional[str]:
    try:
        if validate_image(image_path) != "image/png":
            return None
        validate_text(spec_path)
        validate_text(prompt_path)
        actual_sha = sha256_file(image_path)
    except (OSError, ValueError):
        return None
    if expected_image_sha256 and actual_sha != expected_image_sha256:
        return None
    return actual_sha


def _public_makeup_sources(
    workspace_root: Path, project_id: str
) -> Dict[str, Dict[str, Any]]:
    makeup_root = (
        Path(workspace_root) / "projects" / project_id / "makeup"
    ).resolve()
    manifest_path = makeup_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    usage = manifest.get("usage", {})
    if usage.get("lookup_first") is not True or usage.get("reuse_before_regenerate") is not True:
        return {}
    if usage.get("allow_direct_reference") is not True:
        return {}

    sources: Dict[str, Dict[str, Any]] = {}
    for asset_type, collection_name in MANIFEST_COLLECTION_BY_TYPE.items():
        for entry in manifest.get(collection_name, []):
            if not isinstance(entry, Mapping):
                continue
            asset_id = entry.get("id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            try:
                image_path = (makeup_root / str(entry["sheet"])).resolve()
                spec_path = (makeup_root / str(entry["spec"])).resolve()
                prompt_path = (makeup_root / str(entry["prompt"])).resolve()
                for path in (image_path, spec_path, prompt_path):
                    path.relative_to(makeup_root)
            except (KeyError, ValueError):
                continue
            actual_sha = _valid_source_triplet(
                image_path,
                spec_path,
                prompt_path,
                expected_image_sha256=entry.get("sha256"),
            )
            if actual_sha is None:
                continue
            sources[asset_id] = {
                "asset_id": asset_id,
                "type": asset_type,
                "name": str(entry.get("display_name") or asset_id),
                "source": {
                    "kind": "project_makeup",
                    "episode_id": None,
                    "image_path": _workspace_relative(workspace_root, image_path),
                    "image_sha256": actual_sha,
                    "spec_path": _workspace_relative(workspace_root, spec_path),
                    "prompt_path": _workspace_relative(workspace_root, prompt_path),
                },
            }
    return sources


def _registered_asset_for_path(
    source_status: Mapping[str, Any],
    stage_id: str,
    relative_path: str,
    kind: str,
) -> Optional[Mapping[str, Any]]:
    revision = source_status.get("stages", {}).get(stage_id, {}).get("revision")
    return next(
        (
            asset
            for asset in source_status.get("assets", {}).values()
            if asset.get("status", "active") == "active"
            and asset.get("stage") == stage_id
            and asset.get("revision") == revision
            and asset.get("kind") == kind
            and asset.get("path") == relative_path
        ),
        None,
    )


def _previous_episode_sources(
    workspace_root: Path,
    project_id: str,
    current_episode_id: str,
) -> Dict[str, Dict[str, Any]]:
    workspace_root = Path(workspace_root).resolve()
    store = StatusStore(workspace_root)
    status_root = workspace_root / "status" / project_id
    candidates: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
    if not status_root.is_dir():
        return {}
    for status_path in status_root.glob("*.json"):
        episode_id = status_path.stem
        if episode_id == current_episode_id:
            continue
        try:
            source_status = store.load(project_id, episode_id)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        episode_dir = (
            workspace_root / "projects" / project_id / "episodes" / episode_id
        ).resolve()
        updated_at = str(source_status.get("updated_at") or "")
        for asset_id, image_record in source_status.get("assets", {}).items():
            if image_record.get("kind") != "image" or image_record.get("status", "active") != "active":
                continue
            metadata = image_record.get("metadata", {})
            planned_id = metadata.get("planned_asset_id") or image_record.get("task_id")
            if not isinstance(planned_id, str):
                continue
            asset_type = next(
                (
                    key
                    for key, role in DESIGN_ROLE_BY_TYPE.items()
                    if role == image_record.get("role")
                ),
                None,
            )
            if asset_type is None:
                continue
            stage_id = DESIGN_STAGE_BY_TYPE[asset_type]
            if source_status.get("stages", {}).get(stage_id, {}).get("state") not in {
                "done", "preserved"
            }:
                continue
            target_base = planned_id
            expected_image = "%s_sheet.png" % target_base
            if image_record.get("path") != expected_image:
                continue
            spec_name = "%s_sheet.md" % target_base
            prompt_name = "%s_prompt.txt" % target_base
            spec_record = _registered_asset_for_path(
                source_status, stage_id, spec_name, "document"
            )
            prompt_record = _registered_asset_for_path(
                source_status, stage_id, prompt_name, "prompt"
            )
            if spec_record is None or prompt_record is None:
                continue
            try:
                image_path = resolve_asset_path(episode_dir, expected_image)
                spec_path = resolve_asset_path(episode_dir, spec_name)
                prompt_path = resolve_asset_path(episode_dir, prompt_name)
            except ValueError:
                continue
            actual_sha = _valid_source_triplet(image_path, spec_path, prompt_path)
            if actual_sha is None:
                continue
            if image_record.get("size") != image_path.stat().st_size:
                continue
            if spec_record.get("size") != spec_path.stat().st_size:
                continue
            if prompt_record.get("size") != prompt_path.stat().st_size:
                continue
            candidate = {
                "asset_id": planned_id,
                "type": asset_type,
                "name": str(image_record.get("label") or planned_id),
                "source": {
                    "kind": "previous_episode",
                    "episode_id": episode_id,
                    "image_path": _workspace_relative(workspace_root, image_path),
                    "image_sha256": actual_sha,
                    "spec_path": _workspace_relative(workspace_root, spec_path),
                    "prompt_path": _workspace_relative(workspace_root, prompt_path),
                },
            }
            candidates.setdefault(planned_id, []).append((updated_at, candidate))
    return {
        asset_id: sorted(items, key=lambda item: item[0], reverse=True)[0][1]
        for asset_id, items in candidates.items()
    }


def reusable_asset_inventory(
    workspace_root: Path,
    project_id: str,
    current_episode_id: str,
) -> List[Dict[str, Any]]:
    """List one best reusable source per stable asset id, with public makeup first."""
    public = _public_makeup_sources(workspace_root, project_id)
    previous = _previous_episode_sources(
        workspace_root, project_id, current_episode_id
    )
    merged = dict(previous)
    merged.update(public)
    return [merged[key] for key in sorted(merged)]


def build_asset_reuse_proposal(
    workspace_root: Path,
    status: Mapping[str, Any],
    episode_dir: Path,
) -> Dict[str, Any]:
    plan_path = Path(episode_dir) / "asset_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    inventory = {
        item["asset_id"]: item
        for item in reusable_asset_inventory(
            workspace_root, status["project_id"], status["episode_id"]
        )
    }
    items = []
    for planned in plan.get("assets", []):
        candidate = inventory.get(planned.get("id"))
        if candidate is not None and candidate.get("type") != planned.get("type"):
            candidate = None
        items.append(
            {
                "asset_id": planned["id"],
                "type": planned["type"],
                "name": planned["name"],
                "recommended": "reuse" if candidate is not None else "generate",
                "decision": None,
                "source": candidate["source"] if candidate is not None else None,
            }
        )
    return {
        "state": "waiting_confirmation",
        "asset_plan_sha256": sha256_file(plan_path),
        "items": items,
        "proposed_at": utc_now(),
        "confirmed_at": None,
    }


def confirm_asset_reuse(
    asset_reuse: Mapping[str, Any],
    reuse_ids: Iterable[str],
    *,
    allow_revision: bool = False,
) -> Dict[str, Any]:
    if asset_reuse.get("state") != "waiting_confirmation" and not (
        allow_revision and asset_reuse.get("state") == "confirmed"
    ):
        raise RuntimeError("asset reuse is not waiting for confirmation")
    selected = {str(value).strip() for value in reuse_ids if str(value).strip()}
    candidate_ids = {
        item["asset_id"] for item in asset_reuse["items"] if item["source"] is not None
    }
    unknown = selected - candidate_ids
    if unknown:
        raise ValueError(
            "selected reuse assets have no verified source: %s"
            % ", ".join(sorted(unknown))
        )
    confirmed = dict(asset_reuse)
    confirmed["items"] = [
        {
            **dict(item),
            "decision": "reuse" if item["asset_id"] in selected else "generate",
        }
        for item in asset_reuse["items"]
    ]
    confirmed["state"] = "confirmed"
    confirmed["confirmed_at"] = utc_now()
    return confirmed


def decision_for_asset(
    status: Mapping[str, Any], asset_id: str
) -> Optional[Mapping[str, Any]]:
    reuse = status.get("asset_reuse", {})
    if reuse.get("state") != "confirmed":
        return None
    return next(
        (item for item in reuse.get("items", []) if item.get("asset_id") == asset_id),
        None,
    )
