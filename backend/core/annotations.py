import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .events import EventLog
from .lock import EpisodeLock
from .status import StatusStore, utc_now, validate_id
from .tasks import link_task_annotation, set_task_state


MAX_ANNOTATION_LENGTH = 4000
ANNOTATION_TYPES = {"suggestion", "redo"}
ANNOTATION_STATUSES = {
    "open",
    "planned",
    "processing",
    "verifying",
    "resolved",
    "dismissed",
    "superseded",
}
TERMINAL_STATUSES = {"resolved", "dismissed", "superseded"}
STATUS_TRANSITIONS = {
    "open": {"planned", "resolved", "dismissed", "superseded"},
    "planned": {"open", "processing", "resolved", "dismissed", "superseded"},
    "processing": {"verifying", "resolved", "dismissed", "superseded"},
    "verifying": {"processing", "resolved", "dismissed", "superseded"},
    "resolved": set(),
    "dismissed": set(),
    "superseded": set(),
}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class AnnotationStore:
    """Persist typed feedback while status.json remains the discovery cursor."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)
        self.statuses = StatusStore(self.workspace_root)
        self.events = EventLog(self.workspace_root)

    def annotation_dir(self, project_id: str, episode_id: str) -> Path:
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        return (
            self.workspace_root
            / "projects"
            / project_id
            / "episodes"
            / episode_id
            / "annotations"
        )

    def manifest_path(self, project_id: str, episode_id: str) -> Path:
        return self.annotation_dir(project_id, episode_id) / "index.json"

    def _load_manifest(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        path = self.manifest_path(project_id, episode_id)
        if not path.is_file():
            return {"schema_version": "2.0", "latest_revision": 0, "entries": []}
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") not in {"1.0", "2.0"}
            or not isinstance(manifest.get("entries"), list)
        ):
            raise ValueError("invalid annotation manifest")
        manifest["schema_version"] = "2.0"
        for entry in manifest["entries"]:
            if entry.get("status") == "handled":
                entry["status"] = "resolved"
            entry.setdefault("type", "suggestion")
            entry.setdefault("task_id", entry.get("stage"))
            entry.setdefault("asset_revision", None)
            entry.setdefault("updated_at", entry.get("created_at"))
        return manifest

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("annotation content is required")
        normalized = content.strip()
        if len(normalized) > MAX_ANNOTATION_LENGTH:
            raise ValueError("annotation content is too long")
        return normalized

    @staticmethod
    def _normalize_type(annotation_type: Any) -> str:
        if annotation_type not in ANNOTATION_TYPES:
            raise ValueError("annotation type must be suggestion or redo")
        return str(annotation_type)

    def _annotation_path(
        self, project_id: str, episode_id: str, annotation_id: str
    ) -> Path:
        return self.annotation_dir(project_id, episode_id) / (annotation_id + ".json")

    def _read_annotation(
        self, project_id: str, episode_id: str, annotation_id: str
    ) -> Dict[str, Any]:
        path = self._annotation_path(project_id, episode_id, annotation_id)
        with path.open("r", encoding="utf-8") as handle:
            annotation = json.load(handle)
        if annotation.get("status") == "handled":
            annotation["status"] = "resolved"
        annotation.setdefault("type", "suggestion")
        annotation.setdefault("task_id", annotation.get("stage"))
        annotation.setdefault("asset_revision", None)
        annotation.setdefault("updated_at", annotation.get("created_at"))
        annotation.setdefault("repair_plan_id", None)
        annotation.setdefault("resolution", None)
        return annotation

    def _recompute_summary(
        self, status: Dict[str, Any], manifest: Mapping[str, Any]
    ) -> None:
        entries = sorted(manifest["entries"], key=lambda item: int(item["revision"]))
        handled_revision = 0
        pending_count = 0
        redo_count = 0
        contiguous = True
        for entry in entries:
            state = "resolved" if entry.get("status") == "handled" else entry.get("status")
            terminal = state in TERMINAL_STATUSES
            if contiguous and terminal:
                handled_revision = int(entry["revision"])
            else:
                contiguous = False
            if not terminal:
                pending_count += 1
                if entry.get("type", "suggestion") == "redo":
                    redo_count += 1
        summary = status["annotations"]
        summary.update(
            {
                "state": "pending" if pending_count else "clear",
                "latest_revision": int(manifest.get("latest_revision", 0)),
                "handled_revision": handled_revision,
                "pending_count": pending_count,
                "redo_count": redo_count,
                "updated_at": utc_now(),
            }
        )

    def add(
        self,
        project_id: str,
        episode_id: str,
        stage_id: str,
        content: str,
        *,
        annotation_type: str = "suggestion",
        task_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        locator: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_content = self._normalize_content(content)
        normalized_type = self._normalize_type(annotation_type)
        if locator is not None and not isinstance(locator, Mapping):
            raise ValueError("annotation locator must be an object")
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if stage_id not in status["stages"]:
                raise ValueError("unknown annotation stage: %s" % stage_id)

            asset_revision = None
            resolved_task_id = task_id
            if asset_id is not None:
                if asset_id not in status["assets"]:
                    raise ValueError("unknown annotation asset: %s" % asset_id)
                asset = status["assets"][asset_id]
                if asset["stage"] != stage_id:
                    raise ValueError("annotation asset belongs to another stage")
                asset_task_id = asset.get("task_id") or stage_id
                if resolved_task_id is not None and resolved_task_id != asset_task_id:
                    raise ValueError("annotation task does not match asset task")
                resolved_task_id = asset_task_id
                asset_revision = int(asset.get("asset_revision", 1))
            resolved_task_id = resolved_task_id or stage_id

            summary = status["annotations"]
            revision = int(summary["latest_revision"]) + 1
            annotation_id = "a_%06d" % revision
            created_at = utc_now()
            relative_path = "annotations/%s.json" % annotation_id
            annotation = {
                "id": annotation_id,
                "revision": revision,
                "stage": stage_id,
                "task_id": resolved_task_id,
                "asset_id": asset_id,
                "asset_revision": asset_revision,
                "type": normalized_type,
                "locator": dict(locator or {}),
                "content": normalized_content,
                "created_at": created_at,
                "updated_at": created_at,
                "status": "open",
                "repair_plan_id": None,
                "resolution": None,
            }
            directory = self.annotation_dir(project_id, episode_id)
            _atomic_json(directory / (annotation_id + ".json"), annotation)

            manifest = self._load_manifest(project_id, episode_id)
            manifest["latest_revision"] = revision
            manifest["entries"].append(
                {
                    "id": annotation_id,
                    "revision": revision,
                    "stage": stage_id,
                    "task_id": resolved_task_id,
                    "asset_id": asset_id,
                    "asset_revision": asset_revision,
                    "type": normalized_type,
                    "path": relative_path,
                    "status": "open",
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
            _atomic_json(self.manifest_path(project_id, episode_id), manifest)
            link_task_annotation(status, stage_id, resolved_task_id, annotation_id)
            if normalized_type == "redo":
                set_task_state(status, stage_id, resolved_task_id, "redo_requested")
            self._recompute_summary(status, manifest)
            self.statuses.save(status)
            self.events.append(
                project_id,
                episode_id,
                "info",
                "工作台新增%s %s"
                % ("重做要求" if normalized_type == "redo" else "修改建议", annotation_id),
                stage=stage_id,
                runner="workbench",
                meta={
                    "annotation_id": annotation_id,
                    "annotation_type": normalized_type,
                    "task_id": resolved_task_id,
                    "asset_id": asset_id,
                    "asset_revision": asset_revision,
                },
            )
        return annotation

    def list(
        self, project_id: str, episode_id: str, *, pending_only: bool = False
    ) -> List[Dict[str, Any]]:
        self.statuses.load(project_id, episode_id)
        manifest = self._load_manifest(project_id, episode_id)
        annotations: List[Dict[str, Any]] = []
        for summary in manifest["entries"]:
            try:
                annotation = self._read_annotation(
                    project_id, episode_id, summary["id"]
                )
            except (OSError, json.JSONDecodeError):
                continue
            if pending_only and annotation["status"] in TERMINAL_STATUSES:
                continue
            annotations.append(annotation)
        return annotations

    def transition_locked(
        self,
        status: Dict[str, Any],
        annotation_ids: Iterable[str],
        target_status: str,
        *,
        reason: Optional[str] = None,
        repair_plan_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if target_status not in ANNOTATION_STATUSES:
            raise ValueError("invalid annotation status: %s" % target_status)
        project_id = status["project_id"]
        episode_id = status["episode_id"]
        manifest = self._load_manifest(project_id, episode_id)
        entries = {entry["id"]: entry for entry in manifest["entries"]}
        changed: List[Dict[str, Any]] = []
        for annotation_id in dict.fromkeys(annotation_ids):
            if annotation_id not in entries:
                raise ValueError("unknown annotation: %s" % annotation_id)
            annotation = self._read_annotation(project_id, episode_id, annotation_id)
            current = annotation["status"]
            if current == target_status:
                changed.append(annotation)
                continue
            if target_status not in STATUS_TRANSITIONS[current]:
                raise ValueError(
                    "annotation %s cannot transition from %s to %s"
                    % (annotation_id, current, target_status)
                )
            now = utc_now()
            annotation.update(
                {
                    "status": target_status,
                    "updated_at": now,
                    "repair_plan_id": repair_plan_id
                    if repair_plan_id is not None
                    else annotation.get("repair_plan_id"),
                }
            )
            if target_status in TERMINAL_STATUSES:
                annotation["resolution"] = {
                    "status": target_status,
                    "reason": (reason or "").strip(),
                    "resolved_at": now,
                }
            _atomic_json(
                self._annotation_path(project_id, episode_id, annotation_id), annotation
            )
            entry = entries[annotation_id]
            entry.update(
                {
                    "status": target_status,
                    "updated_at": now,
                    "repair_plan_id": annotation.get("repair_plan_id"),
                }
            )
            changed.append(annotation)
        _atomic_json(self.manifest_path(project_id, episode_id), manifest)
        self._recompute_summary(status, manifest)
        return changed

    def transition(
        self,
        project_id: str,
        episode_id: str,
        annotation_ids: Iterable[str],
        target_status: str,
        *,
        reason: Optional[str] = None,
        repair_plan_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            changed = self.transition_locked(
                status,
                annotation_ids,
                target_status,
                reason=reason,
                repair_plan_id=repair_plan_id,
            )
            self.statuses.save(status)
        return changed

    def mark_handled(
        self, project_id: str, episode_id: str, through_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            summary = status["annotations"]
            latest = int(summary["latest_revision"])
            target = latest if through_revision is None else through_revision
            if isinstance(target, bool) or not isinstance(target, int):
                raise ValueError("annotation revision must be an integer")
            if not int(summary["handled_revision"]) <= target <= latest:
                raise ValueError("annotation revision is outside the pending range")
            manifest = self._load_manifest(project_id, episode_id)
            to_resolve = [
                entry["id"]
                for entry in manifest["entries"]
                if int(entry["revision"]) <= target
                and entry.get("status") not in TERMINAL_STATUSES
                and entry.get("status") != "handled"
            ]
            redo_without_repair = [
                entry["id"]
                for entry in manifest["entries"]
                if entry["id"] in to_resolve
                and entry.get("type", "suggestion") == "redo"
            ]
            if redo_without_repair:
                raise ValueError(
                    "redo annotations require a completed repair plan: %s"
                    % ", ".join(redo_without_repair)
                )
            if to_resolve:
                self.transition_locked(
                    status,
                    to_resolve,
                    "resolved",
                    reason="Codex 已确认修改落盘或明确判定无需修改",
                )
            else:
                self._recompute_summary(status, manifest)
            self.statuses.save(status)
            self.events.append(
                project_id,
                episode_id,
                "decision",
                "标注已处理至 revision %d" % target,
                stage=status["run"].get("current_stage"),
                runner="codex",
                meta={"handled_revision": status["annotations"]["handled_revision"]},
            )
        return status["annotations"]
