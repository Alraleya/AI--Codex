"""Episode file layout helpers.

The workflow writes canonical files at the episode root while a production is
active because downstream stages consume those paths.  Once the whole episode
is complete, transient prompt and run artifacts are moved under
``intermediate/``.  Final deliverables keep their canonical paths.
"""

from __future__ import annotations

import filecmp
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping

from .assets import resolve_asset_path
from .status import utc_now


INTERMEDIATE_DIR = "intermediate"
_PROMPT_FILE = re.compile(r".+_prompt\.txt$")
_RUN_METADATA_FILES = (
    "agent_result_*.json",
    "storyboard_repair_notes*.json",
)


def _is_intermediate_asset(asset: Mapping[str, Any]) -> bool:
    path = PurePosixPath(str(asset.get("path", "")))
    if not path.parts or path.parts[0] == INTERMEDIATE_DIR:
        return False
    if asset.get("kind") != "prompt":
        return False
    # The final video prompt is a user-facing deliverable and is deliberately
    # kept at its existing canonical path for video confirmation and download.
    return asset.get("stage") in {
        "character_design",
        "visual_design",
    } or bool(_PROMPT_FILE.fullmatch(path.name))


def _move_file(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not filecmp.cmp(source, target, shallow=False):
            raise ValueError("intermediate storage path collision: %s" % target)
        source.unlink()
        return
    os.replace(source, target)


def _move_directory_contents(source_dir: Path, target_dir: Path) -> bool:
    if not source_dir.is_dir():
        return False
    moved = False
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        target = target_dir / source.relative_to(source_dir)
        _move_file(source, target)
        moved = True
    for directory in sorted(
        (item for item in source_dir.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        source_dir.rmdir()
    except OSError:
        pass
    return moved


def organize_completed_episode(status: Dict[str, Any], episode_dir: Path) -> bool:
    """Move transient files after an episode reaches the terminal done state."""
    if status.get("run", {}).get("state") != "done":
        return False
    changed = False
    now = utc_now()
    for asset in status.get("assets", {}).values():
        if not _is_intermediate_asset(asset):
            continue
        original_path = str(asset["path"])
        source = resolve_asset_path(episode_dir, original_path)
        if not source.is_file():
            continue
        target_path = "%s/%s/%s" % (
            INTERMEDIATE_DIR,
            asset.get("stage") or "other",
            Path(original_path).name,
        )
        target = resolve_asset_path(episode_dir, target_path)
        _move_file(source, target)
        metadata = dict(asset.get("metadata") or {})
        metadata.update(
            {
                "storage_class": "intermediate",
                "storage_original_path": original_path,
                "storage_archived_at": now,
            }
        )
        asset["path"] = target_path
        asset["metadata"] = metadata
        changed = True

    metadata_dir = resolve_asset_path(episode_dir, "%s/run_metadata" % INTERMEDIATE_DIR)
    for pattern in _RUN_METADATA_FILES:
        for source in sorted(episode_dir.glob(pattern)):
            _move_file(source, metadata_dir / source.name)
            changed = True
    if _move_directory_contents(
        episode_dir / "storyboard_reference_packages",
        resolve_asset_path(episode_dir, "%s/reference_packages" % INTERMEDIATE_DIR),
    ):
        changed = True
    return changed


def restore_intermediate_storage(status: Dict[str, Any], episode_dir: Path) -> bool:
    """Restore transient prompt assets before reopening a completed episode."""
    changed = False
    for asset in status.get("assets", {}).values():
        metadata = dict(asset.get("metadata") or {})
        if metadata.get("storage_class") != "intermediate":
            continue
        original_path = metadata.get("storage_original_path")
        if not isinstance(original_path, str) or not original_path:
            continue
        source = resolve_asset_path(episode_dir, str(asset["path"]))
        target = resolve_asset_path(episode_dir, original_path)
        if source.is_file():
            if target.exists() and not filecmp.cmp(source, target, shallow=False):
                raise ValueError("cannot restore intermediate asset over existing file: %s" % original_path)
            _move_file(source, target)
        asset["path"] = original_path
        metadata["storage_class"] = "active"
        metadata.pop("storage_archived_at", None)
        asset["metadata"] = metadata
        changed = True
    return changed
