"""Narrow, read-only workbench projection for the one retained legacy episode."""

import copy
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from backend.core.flow import STAGES


LEGACY_WORKBENCH_EPISODE = ("meituan_sunce", "s01e01")
LEGACY_STAGE_MAP = {
    "story_design": "story_design",
    "prompts": "story_design",
    "character_design": "character_design",
    "scene_design": "visual_design",
    "prop_design": "visual_design",
    "storyboard_generation": "storyboard_generation",
    "final_video_prompts": "video_binding",
    "video_generation": "video_generation",
    "edit_post": "edit_post",
}


def is_retained_legacy_episode(project_id: str, episode_id: str) -> bool:
    return (project_id, episode_id) == LEGACY_WORKBENCH_EPISODE


def _empty_stage(stage: Any, *, state: str = "pending") -> Dict[str, Any]:
    return {
        "label": stage.label,
        "state": state,
        "skills": list(stage.skills),
        "depends_on": list(stage.depends_on),
        "execution_mode": stage.execution_mode,
        "task_unit": stage.task_unit,
        "attempt": 0,
        "revision": 1,
        "started_at": None,
        "completed_at": None,
        "inputs": [],
        "outputs": [],
        "handoff": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "review": {"state": "not_required", "reason": ""},
        "error": None,
    }


def _combined_state(*states: str) -> str:
    priority = ("blocked", "repairing", "reviewing", "running", "waiting_confirmation", "pending")
    for state in priority:
        if state in states:
            return state
    if states and all(state == "skipped" for state in states):
        return "skipped"
    return "done"


def project_meituan_status(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Project the retained v1 episode into the v2 viewer contract only."""
    if not is_retained_legacy_episode(str(raw.get("project_id")), str(raw.get("episode_id"))):
        raise ValueError("legacy workbench projection is limited to 美团孙策")
    if raw.get("flow_version") != "1.0":
        return copy.deepcopy(dict(raw))

    projected = copy.deepcopy(dict(raw))
    old_stages = raw.get("stages", {})
    stages = {stage.id: _empty_stage(stage) for stage in STAGES}

    for stage in STAGES:
        sources = [
            copy.deepcopy(value)
            for source_id, value in old_stages.items()
            if LEGACY_STAGE_MAP.get(source_id) == stage.id
        ]
        if not sources:
            continue
        merged = stages[stage.id]
        merged.update(sources[0])
        merged.update({
            "label": stage.label,
            "skills": list(stage.skills),
            "depends_on": list(stage.depends_on),
            "execution_mode": stage.execution_mode,
            "task_unit": stage.task_unit,
            "state": _combined_state(*(str(item.get("state", "pending")) for item in sources)),
            "outputs": [path for item in sources for path in item.get("outputs", [])],
        })

    # These stages never existed in v1. They are shown as historical skips, not fabricated work.
    stages["asset_planning"] = _empty_stage(next(stage for stage in STAGES if stage.id == "asset_planning"), state="skipped")
    stages["storyboard_binding"] = _empty_stage(next(stage for stage in STAGES if stage.id == "storyboard_binding"), state="skipped")
    stages["video_binding"] = _empty_stage(next(stage for stage in STAGES if stage.id == "video_binding"), state="skipped")

    for asset in projected.get("assets", {}).values():
        source_stage = asset.get("stage")
        asset["stage"] = LEGACY_STAGE_MAP.get(source_stage, source_stage)
        if source_stage == "prompts":
            name = Path(str(asset.get("path", ""))).name
            if name.startswith("shot"):
                asset["task_id"] = name.split("_", 1)[0]
        if source_stage in {"scene_design", "prop_design"}:
            asset.setdefault("metadata", {})["legacy_source_stage"] = source_stage

    for task in projected.get("tasks", {}).values():
        task["stage"] = LEGACY_STAGE_MAP.get(task.get("stage"), task.get("stage"))

    current = str(projected.get("run", {}).get("current_stage") or "story_design")
    projected["run"]["current_stage"] = LEGACY_STAGE_MAP.get(current, "story_design")
    projected["stages"] = stages
    projected["flow_version"] = "2.0"
    projected["workbench_compatibility"] = {
        "mode": "retained_meituan_sunce_v1",
        "source_flow_version": "1.0",
        "read_only": True,
    }
    return projected


def load_retained_legacy_status(workspace_root: Path) -> Dict[str, Any]:
    path = Path(workspace_root) / "status" / LEGACY_WORKBENCH_EPISODE[0] / (LEGACY_WORKBENCH_EPISODE[1] + ".json")
    with path.open("r", encoding="utf-8") as handle:
        return project_meituan_status(json.load(handle))
