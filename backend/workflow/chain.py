import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from backend.core.assets import archive_asset, resolve_asset_path
from backend.core.authorization import ProviderCallNotAuthorized
from backend.core.style import expand_style
from backend.core.flow import STAGE_BY_ID, STAGE_IDS, STAGES, transitive_downstream
from backend.core.status import (
    is_dialogue_direct,
    normalize_video_session_id,
    resolved_shot_count,
    resolved_storyboard_shots,
    utc_now,
)
from backend.core.storage import organize_completed_episode, restore_intermediate_storage
from backend.core.tasks import reset_stage_tasks, set_task_state
from .contracts import validate_output_names


def _clear_agent_request(status: Dict[str, Any]) -> None:
    """Invalidate any handoff whose stage revision is about to change."""
    status["agent_request"] = {"state": "clear", "request": None}
    if "incremental_request" in status:
        status["incremental_request"] = {"state": "clear", "request": None}


@dataclass(frozen=True)
class FlowDecision:
    action: str
    stage_id: Optional[str]
    reason: str = ""


def _first_incomplete(status: Dict[str, Any]) -> Optional[str]:
    for stage_id in STAGE_IDS:
        if status["stages"][stage_id]["state"] not in {"done", "skipped", "preserved"}:
            return stage_id
    return None


def _stage_complete(status: Dict[str, Any], stage_id: str) -> bool:
    return status["stages"][stage_id]["state"] in {"done", "skipped", "preserved"}


def _shot_key(shot_number: int) -> str:
    return "shot%02d" % shot_number


def _video_confirmation(status: Dict[str, Any]) -> Dict[str, Any]:
    return status.setdefault("confirmations", {}).setdefault(
        "video_generation",
        {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        },
    )


def _video_shot_approval(status: Dict[str, Any], shot_number: int) -> Dict[str, Any]:
    confirmation = _video_confirmation(status)
    shots = confirmation.setdefault("shots", {})
    return shots.setdefault(
        _shot_key(shot_number),
        {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "generated_at": None,
        },
    )


def _validate_video_shot(status: Dict[str, Any], shot_number: int) -> int:
    if isinstance(shot_number, bool) or not isinstance(shot_number, int):
        raise ValueError("video shot number must be an integer")
    if "creative_brief" not in status:
        if shot_number < 1:
            raise ValueError("video shot number must be positive")
        return shot_number
    shot_count = resolved_shot_count(status["creative_brief"])
    if not 1 <= shot_number <= shot_count:
        raise ValueError("video shot number must be between 1 and %d" % shot_count)
    return shot_number


DIALOGUE_DIRECT_SKIP_STAGES = {
    "storyboard_binding",
    "storyboard_generation",
}


def compute_revision_fingerprint(status: Dict[str, Any], episode_dir: Path) -> str:
    video_index = STAGE_IDS.index("video_generation")
    records = [
        asset
        for asset in status["assets"].values()
        if STAGE_IDS.index(asset["stage"]) < video_index
    ]
    records.sort(key=lambda asset: (asset["stage"], asset["path"]))
    digest = hashlib.sha256()
    if not records:
        raise ValueError("cannot fingerprint an episode without upstream assets")
    for record in records:
        path = resolve_asset_path(episode_dir, record["path"])
        if not path.is_file():
            raise ValueError("fingerprinted asset is missing: %s" % record["path"])
        digest.update(record["stage"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def compute_video_shot_fingerprint(
    status: Dict[str, Any], episode_dir: Path, shot_number: int
) -> str:
    """Bind approval to the current upstream assets and this shot's final prompt."""
    _validate_video_shot(status, shot_number)
    prompt_path = episode_dir / ("shot%02d_prompt_video.txt" % shot_number)
    if not prompt_path.is_file():
        raise ValueError("video prompt is missing: %s" % prompt_path.name)
    digest = hashlib.sha256()
    digest.update(compute_revision_fingerprint(status, episode_dir).encode("ascii"))
    digest.update(b"\0")
    brief = status.get("creative_brief", {})
    digest.update(str(brief.get("video_model", "fast")).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(brief.get("video_session_id") or "").encode("utf-8"))
    digest.update(b"\0")
    shot_aspect_ratios = brief.get("video_shot_aspect_ratios") or {}
    digest.update(str(shot_aspect_ratios.get("shot%02d" % shot_number) or "").encode("utf-8"))
    digest.update(b"\0")
    digest.update(prompt_path.name.encode("utf-8"))
    digest.update(b"\0")
    with prompt_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_video_supplement_fingerprint(
    status: Dict[str, Any],
    episode_dir: Path,
    unit_id: str,
    prompt_path: Path,
    references_path: Path,
    duration_sec: float,
) -> str:
    """Bind an explicitly added supplemental video unit to its current inputs."""
    if not isinstance(unit_id, str) or not re.fullmatch(r"[0-9]{2}[a-z]", unit_id):
        raise ValueError("supplemental video unit id must look like 08b")
    if not prompt_path.is_file():
        raise ValueError("supplemental video prompt is missing: %s" % prompt_path.name)
    if not references_path.is_file():
        raise ValueError(
            "supplemental video references are missing: %s" % references_path.name
        )
    if float(duration_sec) < 4 or float(duration_sec) > 15:
        raise ValueError("supplemental video duration must be between 4 and 15 seconds")
    digest = hashlib.sha256()
    digest.update(compute_revision_fingerprint(status, episode_dir).encode("ascii"))
    for value in (unit_id, str(status.get("creative_brief", {}).get("video_model", "fast")),
                  str(status.get("creative_brief", {}).get("video_session_id") or ""),
                  "%g" % float(duration_sec)):
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
    for path in (prompt_path, references_path):
        digest.update(b"\0")
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    try:
        binding = json.loads(references_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError("supplemental video references are invalid") from error
    for item in binding.get("references", []):
        raw_reference = Path(item.get("path", ""))
        reference = (
            raw_reference
            if raw_reference.is_absolute()
            else resolve_asset_path(episode_dir, str(raw_reference))
        )
        if not reference.is_file():
            raise ValueError("supplemental reference image is missing: %s" % item.get("path", ""))
        digest.update(b"\0")
        digest.update(str(item.get("asset_id") or "").encode("utf-8"))
        with reference.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _video_supplement_approvals(status: Dict[str, Any]) -> Dict[str, Any]:
    return status.setdefault("confirmations", {}).setdefault(
        "video_generation_supplements", {}
    )


def is_video_supplement_confirmation_valid(
    status: Dict[str, Any],
    episode_dir: Path,
    unit_id: str,
    prompt_path: Path,
    references_path: Path,
    duration_sec: float,
) -> bool:
    approval = _video_supplement_approvals(status).get(unit_id, {})
    if approval.get("state") != "approved" or not approval.get("revision_fingerprint"):
        return False
    try:
        current = compute_video_supplement_fingerprint(
            status, episode_dir, unit_id, prompt_path, references_path, duration_sec
        )
    except ValueError:
        return False
    return current == approval["revision_fingerprint"]


def approve_video_supplement(
    status: Dict[str, Any],
    episode_dir: Path,
    unit_id: str,
    prompt_path: Path,
    references_path: Path,
    duration_sec: float,
    fingerprint: str,
) -> str:
    current = compute_video_supplement_fingerprint(
        status, episode_dir, unit_id, prompt_path, references_path, duration_sec
    )
    if current != fingerprint:
        raise ProviderCallNotAuthorized(
            "supplemental video approval does not match the current revision fingerprint"
        )
    _video_supplement_approvals(status)[unit_id] = {
        "state": "approved",
        "revision_fingerprint": fingerprint,
        "approved_at": utc_now(),
        "generated_at": None,
    }
    return fingerprint


def consume_video_supplement_approval(status: Dict[str, Any], unit_id: str) -> None:
    approval = _video_supplement_approvals(status).setdefault(unit_id, {})
    approval.update({"state": "generated", "generated_at": utc_now()})


def is_video_confirmation_valid(status: Dict[str, Any], episode_dir: Path) -> bool:
    confirmation = status["confirmations"]["video_generation"]
    if confirmation["state"] != "approved" or not confirmation["revision_fingerprint"]:
        return False
    try:
        current = compute_revision_fingerprint(status, episode_dir)
    except ValueError:
        return False
    return current == confirmation["revision_fingerprint"]


def is_video_shot_confirmation_valid(
    status: Dict[str, Any], episode_dir: Path, shot_number: int
) -> bool:
    _validate_video_shot(status, shot_number)
    if "creative_brief" not in status:
        return is_video_confirmation_valid(status, episode_dir)
    approval = _video_shot_approval(status, shot_number)
    if (
        approval.get("state") == "required"
        and not status.get("confirmations", {}).get("video_generation", {}).get("shots")
        and is_video_confirmation_valid(status, episode_dir)
    ):
        return True
    if approval.get("state") != "approved" or not approval.get("revision_fingerprint"):
        return False
    try:
        current = compute_video_shot_fingerprint(status, episode_dir, shot_number)
    except ValueError:
        return False
    return current == approval["revision_fingerprint"]


def consume_video_shot_approval(status: Dict[str, Any], shot_number: int) -> None:
    approval = _video_shot_approval(status, shot_number)
    approval.update({"state": "generated", "generated_at": utc_now()})


def require_video_shot_approval(status: Dict[str, Any], shot_number: int) -> None:
    approval = _video_shot_approval(status, shot_number)
    approval.update(
        {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "generated_at": None,
        }
    )


def flow_next(status: Dict[str, Any], episode_dir: Optional[Path] = None) -> FlowDecision:
    if not status["creative_brief"].get("style"):
        return FlowDecision(
            "wait_style",
            "story_design",
            "开始制作前需要选择视觉风格；剧本未提供风格，流程不会自动代入默认值",
        )
    if not status["creative_brief"].get("aspect_ratio"):
        return FlowDecision(
            "wait_aspect_ratio",
            "story_design",
            "开始制作前需要选择画幅比例；剧本未提供画幅，流程不会自动代入默认值",
        )
    if status["creative_brief"].get("video_model", "fast") == "pending":
        return FlowDecision(
            "wait_video_model",
            "story_design",
            "开始制作前需要选择视频模型：fast 或 mini",
        )
    if status["creative_brief"].get("storyboard_decision", "yes") == "pending":
        return FlowDecision(
            "wait_user_decision",
            "story_design",
            "开始制作前需要确认是否生成故事板",
        )
    stage_id = _first_incomplete(status)
    if stage_id is None:
        return FlowDecision("done", None)
    stage = status["stages"][stage_id]
    if (
        stage_id == "character_design"
        and status.get("asset_reuse", {}).get("state") == "waiting_confirmation"
    ):
        return FlowDecision(
            "wait_asset_reuse",
            stage_id,
            "character and visual design require a confirmed reuse decision",
        )
    unmet = [
        dependency
        for dependency in STAGE_BY_ID[stage_id].depends_on
        if not _stage_complete(status, dependency)
    ]
    if unmet:
        return FlowDecision("blocked", stage_id, "unmet dependencies: %s" % ", ".join(unmet))
    if stage["state"] in {"running", "reviewing", "repairing"}:
        return FlowDecision("blocked", stage_id, "stage is already running")
    if is_dialogue_direct(status["creative_brief"]) and stage_id in DIALOGUE_DIRECT_SKIP_STAGES:
        return FlowDecision(
            "skip",
            stage_id,
            "对话直通模式跳过故事板参考绑定和故事板生成",
        )
    if (
        stage_id == "storyboard_generation"
        and not any(
            shot.get("storyboard_required", True)
            for shot in resolved_storyboard_shots(status["creative_brief"])
        )
    ):
        return FlowDecision(
            "skip",
            stage_id,
            "第一阶段锁定方案判定所有视频单元均无需渲染故事板",
        )
    if stage_id == "video_generation":
        if not normalize_video_session_id(
            status["creative_brief"].get("video_session_id")
        ):
            return FlowDecision(
                "wait_video_session",
                stage_id,
                "video generation requires an episode-scoped sessionId",
            )
        legacy_approved = episode_dir is not None and is_video_confirmation_valid(
            status, episode_dir
        )
        shot_approved = episode_dir is not None and any(
            is_video_shot_confirmation_valid(status, episode_dir, number)
            for number in range(1, resolved_shot_count(status["creative_brief"]) + 1)
        )
        if not legacy_approved and not shot_approved:
            return FlowDecision(
                "wait_confirmation",
                stage_id,
                "video generation requires approval for one specific shot",
            )
    return FlowDecision("run", stage_id)


def begin_stage(status: Dict[str, Any], episode_dir: Path) -> str:
    decision = flow_next(status, episode_dir)
    if decision.action != "run" or decision.stage_id is None:
        raise RuntimeError(decision.reason or "no stage is ready to run")
    stage = status["stages"][decision.stage_id]
    stage["state"] = "running"
    stage["attempt"] += 1
    stage["started_at"] = utc_now()
    stage["completed_at"] = None
    stage["error"] = None
    status["run"].update(
        {
            "state": "running",
            "current_stage": decision.stage_id,
            "last_error": None,
            "waiting_for": None,
        }
    )
    if stage.get("execution_mode") == "main_agent":
        set_task_state(status, decision.stage_id, decision.stage_id, "running")
    return decision.stage_id


def wait_for_video_confirmation(status: Dict[str, Any]) -> None:
    if _first_incomplete(status) != "video_generation":
        raise RuntimeError("video_generation is not the next stage")
    stage = status["stages"]["video_generation"]
    stage["state"] = "waiting_confirmation"
    status["run"].update(
        {
            "state": "waiting_confirmation",
            "current_stage": "video_generation",
            "waiting_for": "video_shot_approval",
        }
    )


def wait_for_video_model(status: Dict[str, Any]) -> None:
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "story_design",
            "waiting_for": "video_model",
        }
    )


def wait_for_style(status: Dict[str, Any]) -> None:
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "story_design",
            "waiting_for": "style",
        }
    )


def wait_for_aspect_ratio(status: Dict[str, Any]) -> None:
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "story_design",
            "waiting_for": "aspect_ratio",
        }
    )


def wait_for_video_session(status: Dict[str, Any]) -> None:
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "video_generation",
            "waiting_for": "video_session_id",
        }
    )


def approve_video_shot(
    status: Dict[str, Any], episode_dir: Path, shot_number: int
) -> str:
    _validate_video_shot(status, shot_number)
    # A single generated shot can be removed for replacement while the other
    # shots remain complete. In that case remove-video-shot deliberately keeps
    # the stage-level outputs intact, so the stage may still say "done" even
    # though this specific task is pending and the run is waiting for approval.
    # Reopen the stage at this approval boundary instead of rejecting the
    # explicitly requested replacement.
    shot_task = status.get("tasks", {}).get(
        "video_generation:shot%02d" % shot_number, {}
    )
    has_active_shot_asset = any(
        asset.get("stage") == "video_generation"
        and asset.get("task_id") == "shot%02d" % shot_number
        and asset.get("status") == "active"
        for asset in status.get("assets", {}).values()
    )
    replacement_recovery = (
        status["stages"]["video_generation"].get("state") == "done"
        and (
            shot_task.get("state") == "pending"
            # Status loading migrates a pending task to passed while its
            # completed stage is still marked done; an empty asset list is the
            # durable signal that this shot was deliberately removed.
            or not shot_task.get("asset_ids")
            or not has_active_shot_asset
        )
        and status.get("run", {}).get("state") == "waiting_confirmation"
        and status.get("run", {}).get("waiting_for") == "video_shot_approval"
    )
    if replacement_recovery:
        status["stages"]["video_generation"]["state"] = "waiting_confirmation"
    if not replacement_recovery and _first_incomplete(status) != "video_generation":
        raise RuntimeError("video_generation is not ready for confirmation")
    for stage_id in STAGE_IDS[: STAGE_IDS.index("video_generation")]:
        if not _stage_complete(status, stage_id):
            raise RuntimeError("upstream stage is incomplete: %s" % stage_id)
    fingerprint = compute_video_shot_fingerprint(status, episode_dir, shot_number)
    approval = _video_shot_approval(status, shot_number)
    approval.update(
        {
            "state": "approved",
            "revision_fingerprint": fingerprint,
            "approved_at": utc_now(),
            "generated_at": None,
        }
    )
    status["stages"]["video_generation"]["state"] = "pending"
    status["run"].update(
        {
            "state": "idle",
            "current_stage": "video_generation",
            "waiting_for": None,
        }
    )
    return fingerprint


def wait_for_storyboard_decision(status: Dict[str, Any]) -> None:
    if status["creative_brief"].get("storyboard_decision", "yes") != "pending":
        raise RuntimeError("storyboard decision is already set")
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "story_design",
            "waiting_for": "storyboard_decision",
        }
    )


def wait_for_asset_reuse_confirmation(status: Dict[str, Any]) -> None:
    if status.get("asset_reuse", {}).get("state") != "waiting_confirmation":
        raise RuntimeError("asset reuse proposal is not waiting for confirmation")
    status["run"].update(
        {
            "state": "waiting_user_decision",
            "current_stage": "character_design",
            "last_error": None,
            "waiting_for": "asset_reuse_confirmation",
        }
    )


def skip_stage(status: Dict[str, Any], stage_id: str, reason: str) -> None:
    """Mark an intentionally bypassed node without inventing placeholder assets."""
    if _first_incomplete(status) != stage_id:
        raise RuntimeError("cannot skip a stage out of flow order")
    stage = status["stages"][stage_id]
    if stage["state"] != "pending":
        raise RuntimeError("stage must be pending before it can be skipped")
    stage.update(
        {
            "state": "skipped",
            "started_at": utc_now(),
            "completed_at": utc_now(),
            "inputs": [],
            "outputs": [],
            "handoff": {"summary": reason, "outputs": []},
            "review": {"state": "skipped", "reason": reason},
            "error": None,
        }
    )
    for task in status.get("tasks", {}).values():
        if task.get("stage") == stage_id and task.get("state") != "failed":
            set_task_state(status, stage_id, task["id"], "passed", review_reason=reason)
    next_stage = _first_incomplete(status)
    status["run"].update(
        {"state": "done" if next_stage is None else "idle", "current_stage": next_stage, "last_error": None}
    )


def approve_video(status: Dict[str, Any], episode_dir: Path) -> str:
    if _first_incomplete(status) != "video_generation":
        raise RuntimeError("video_generation is not ready for confirmation")
    for stage_id in STAGE_IDS[: STAGE_IDS.index("video_generation")]:
        if not _stage_complete(status, stage_id):
            raise RuntimeError("upstream stage is incomplete: %s" % stage_id)
    fingerprint = compute_revision_fingerprint(status, episode_dir)
    confirmation = status["confirmations"]["video_generation"]
    confirmation.update(
        {
            "state": "approved",
            "revision_fingerprint": fingerprint,
            "approved_at": utc_now(),
            "shots": confirmation.get("shots", {}),
        }
    )
    status["stages"]["video_generation"]["state"] = "pending"
    status["run"].update(
        {"state": "idle", "current_stage": "video_generation", "waiting_for": None}
    )
    return fingerprint


def pass_review(status: Dict[str, Any], stage_id: str, reason: str = "") -> None:
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    stage = status["stages"][stage_id]
    stage["review"] = {"state": "passed", "reason": reason}
    if stage["state"] in {"running", "repairing"}:
        stage["state"] = "reviewing"
    if stage.get("execution_mode") == "main_agent":
        set_task_state(status, stage_id, stage_id, "reviewing")


def record_stage_handoff(
    status: Dict[str, Any], stage_id: str, summary: str, output_names: Iterable[str]
) -> None:
    """Store the small handoff used to start the next node."""
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("stage handoff summary is required")
    summary = summary.strip()
    if len(summary) > 1200:
        raise ValueError("stage handoff summary is too long")
    status["stages"][stage_id]["handoff"] = {
        "summary": summary,
        "outputs": sorted(set(output_names)),
    }


def pause_run(status: Dict[str, Any]) -> None:
    """Pause only between nodes; never leave a half-finished stage marked paused."""
    next_stage = _first_incomplete(status)
    if next_stage is None:
        status["run"].update(
            {"state": "done", "current_stage": None, "last_error": None}
        )
        return
    if status["stages"][next_stage]["state"] in {"running", "reviewing", "repairing"}:
        raise RuntimeError("cannot pause while a stage is still running")
    status["run"].update(
        {"state": "paused", "current_stage": next_stage, "last_error": None}
    )


def recover_interrupted_run_and_pause(status: Dict[str, Any]) -> str:
    """Recover a process interrupted during a provider call, then pause safely.

    Completed text/image assets remain registered. Only in-flight task markers
    are reset so the next explicit advance can reuse valid checkpoints.
    """
    if status["run"].get("state") != "running":
        raise RuntimeError("episode is not in a running state")
    stage_id = status["run"].get("current_stage")
    if stage_id not in STAGE_BY_ID:
        raise RuntimeError("running episode has no current stage")
    stage = status["stages"][stage_id]
    if stage.get("state") not in {"running", "reviewing", "repairing"}:
        raise RuntimeError("current stage is not recoverable: %s" % stage_id)
    stage.update({"state": "pending", "started_at": None, "completed_at": None, "error": None})
    for task in status.get("tasks", {}).values():
        if task.get("stage") == stage_id and task.get("state") in {
            "queued", "running", "reviewing", "redo_requested"
        }:
            set_task_state(status, stage_id, task["id"], "pending")
    _clear_agent_request(status)
    status["run"].update({"state": "paused", "current_stage": stage_id, "last_error": None})
    return stage_id


def cancel_waiting_agent_and_pause(status: Dict[str, Any]) -> str:
    """Cancel an unsubmitted native Agent handoff at a stable stage boundary."""
    if status["run"].get("state") != "waiting_agent":
        raise RuntimeError("no native Agent handoff is waiting")
    request = status.get("agent_request", {}).get("request")
    if not isinstance(request, dict):
        raise RuntimeError("waiting_agent state has no request")
    stage_id = request["stage_id"]
    stage = status["stages"][stage_id]
    # Candidate text lives only inside the cancelled handoff. Candidate images
    # are unregistered and will be regenerated/reviewed on the next advance.
    stage.update({"state": "pending", "started_at": None, "error": None})
    for task in status.get("tasks", {}).values():
        if task.get("stage") != stage_id:
            continue
        if task.get("state") in {"running", "reviewing", "queued"}:
            set_task_state(status, stage_id, task["id"], "pending")
    _clear_agent_request(status)
    status["run"].update(
        {"state": "paused", "current_stage": stage_id, "last_error": None}
    )
    return stage_id


def complete_stage(
    status: Dict[str, Any], stage_id: str, output_names: Iterable[str], episode_dir: Path
) -> None:
    if _first_incomplete(status) != stage_id:
        raise RuntimeError("cannot complete a stage out of flow order")
    stage = status["stages"][stage_id]
    if stage["state"] not in {"running", "reviewing", "repairing"}:
        raise RuntimeError("stage must be active before completion")
    outputs = validate_output_names(stage_id, output_names, status["creative_brief"])
    registered = {
        asset["path"]
        for asset in status["assets"].values()
        if asset["stage"] == stage_id
    }
    if set(outputs) != registered:
        raise ValueError("every stage output must be registered exactly once")
    for output in outputs:
        if not resolve_asset_path(episode_dir, output).is_file():
            raise ValueError("registered output is missing: %s" % output)
    if stage["review"].get("state") != "passed":
        raise ValueError("stage review has not passed")
    stage["outputs"] = outputs
    if stage.get("handoff") is None:
        record_stage_handoff(
            status,
            stage_id,
            stage["review"].get("reason") or "阶段输出已通过合同校验",
            outputs,
        )
    else:
        # Design nodes may create text first and images second.  The handoff is
        # finalized only when the whole stage completes.
        stage["handoff"]["outputs"] = list(outputs)
    stage["state"] = "done"
    stage["completed_at"] = utc_now()
    review_reason = stage["review"].get("reason") or "阶段审查通过"
    for task in status.get("tasks", {}).values():
        if task.get("stage") == stage_id and task.get("state") != "failed":
            set_task_state(
                status,
                stage_id,
                task["id"],
                "passed",
                review_reason=review_reason,
            )
    next_stage = _first_incomplete(status)
    status["run"].update(
        {
            "state": "done" if next_stage is None else "idle",
            "current_stage": next_stage,
            "last_error": None,
        }
    )
    if next_stage is None:
        organize_completed_episode(status, episode_dir)


def block_stage(status: Dict[str, Any], stage_id: str, error: str) -> None:
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    stage = status["stages"][stage_id]
    stage["state"] = "blocked"
    stage["error"] = error
    status["run"].update(
        {"state": "blocked", "current_stage": stage_id, "last_error": error}
    )
    for task in status.get("tasks", {}).values():
        if task.get("stage") == stage_id and task.get("state") in {
            "queued",
            "running",
            "reviewing",
            "redo_requested",
        }:
            set_task_state(status, stage_id, task["id"], "failed", error=error)


def invalidate_from(status: Dict[str, Any], stage_id: str) -> tuple:
    affected = transitive_downstream(stage_id)
    _clear_agent_request(status)
    affected_set = set(affected)
    status["assets"] = {
        asset_id: asset
        for asset_id, asset in status["assets"].items()
        if asset["stage"] not in affected_set
    }
    for affected_id in affected:
        stage = status["stages"][affected_id]
        stage.update(
            {
                "state": "pending",
                "revision": stage["revision"] + 1,
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    reset_stage_tasks(status, affected)
    if stage_id == "story_design":
        status["creative_brief"]["shot_plan"] = {
            "state": "pending",
            "shot_count": None,
            "durations_sec": [],
            "reason": "",
        }
    if stage_id == "story_design":
        status["creative_brief"]["storyboard_plan"] = {
            "state": "pending",
            "shots": [],
        }
    if STAGE_IDS.index(stage_id) < STAGE_IDS.index("video_generation"):
        status["confirmations"]["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
    status["run"].update(
        {"state": "idle", "current_stage": stage_id, "last_error": None}
    )
    return affected


def restart_from_stage(
    status: Dict[str, Any],
    episode_dir: Path,
    stage_id: str,
    *,
    delete_old: bool = False,
) -> tuple:
    """Restart a stage and every downstream consumer, preserving upstream work.

    Unlike ``invalidate_from`` this includes the requested stage itself. Active
    assets are copied to immutable history before their canonical paths are
    reused by the new revision.
    """
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    restore_intermediate_storage(status, episode_dir)
    affected = transitive_downstream(stage_id)
    _clear_agent_request(status)
    affected_set = set(affected)
    active_asset_stages = {
        asset.get("stage")
        for asset in status.get("assets", {}).values()
        if asset.get("stage") in affected_set
    }
    for asset_id, asset in list(status.get("assets", {}).items()):
        if asset.get("stage") not in affected_set:
            continue
        if delete_old:
            source = resolve_asset_path(episode_dir, asset["path"])
            if source.is_file():
                source.unlink()
        else:
            archive_asset(status, episode_dir, asset_id)
        status["assets"].pop(asset_id, None)

    if delete_old:
        preserved_paths = {
            asset["path"]
            for asset in status.get("assets", {}).values()
            if asset.get("stage") not in affected_set
        }
        for child in episode_dir.iterdir():
            if child.name in {"episode.json", "annotations", "repairs", "runs", "versions"}:
                continue
            if child.is_file() and child.relative_to(episode_dir).as_posix() not in preserved_paths:
                child.unlink()
        runs_dir = episode_dir / "runs"
        for affected_id in affected:
            stage_runs = runs_dir / affected_id
            if stage_runs.exists():
                shutil.rmtree(stage_runs)
        history = status.setdefault("asset_history", {})
        preserved_history = {
            history_id: item
            for history_id, item in history.items()
            if item.get("stage") not in affected_set
        }
        preserved_history_paths = {
            item.get("path") for item in preserved_history.values() if item.get("path")
        }
        versions_dir = episode_dir / "versions"
        if versions_dir.exists():
            for child in versions_dir.rglob("*"):
                if child.is_file() and child.relative_to(episode_dir).as_posix() not in preserved_history_paths:
                    child.unlink()
            for child in sorted(versions_dir.rglob("*"), reverse=True):
                if child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
        status["asset_history"] = preserved_history

    for affected_id in affected:
        stage = status["stages"][affected_id]
        had_active_result = (
            stage["state"] != "pending"
            or bool(stage.get("outputs"))
            or affected_id in active_asset_stages
        )
        stage.update(
            {
                "state": "pending",
                "revision": stage["revision"] + (1 if had_active_result else 0),
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    reset_stage_tasks(status, affected)
    if stage_id == "story_design":
        status["creative_brief"]["storyboard_plan"] = {
            "state": "pending",
            "shots": [],
        }
    if STAGE_IDS.index(stage_id) < STAGE_IDS.index("video_generation"):
        status["confirmations"]["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
    status["run"].update(
        {"state": "idle", "current_stage": stage_id, "last_error": None}
    )
    return affected


def rollback_with_preserved_stages(
    status: Dict[str, Any],
    episode_dir: Path,
    from_stage_id: str,
    preserved_stage_ids: Sequence[str],
) -> tuple:
    """Rollback from a stage while explicitly retaining selected stage assets.

    This is intentionally confirmation-driven: callers must pass the exact
    preserved stage list. The default all-reset path remains
    ``restart_from_stage`` so no implicit destructive choice is made.
    """
    if from_stage_id not in STAGE_BY_ID:
        raise ValueError("unknown rollback stage: %s" % from_stage_id)
    restore_intermediate_storage(status, episode_dir)
    affected = transitive_downstream(from_stage_id)
    preserved = set(preserved_stage_ids)
    unknown = preserved - set(STAGE_IDS)
    if unknown:
        raise ValueError("unknown preserved stages: %s" % ", ".join(sorted(unknown)))
    outside = preserved - set(affected)
    if outside:
        raise ValueError(
            "preserved stages must be at or downstream of rollback point: %s"
            % ", ".join(sorted(outside))
        )
    for stage_id in preserved:
        if status["stages"][stage_id]["state"] not in {"done", "skipped", "preserved"}:
            raise ValueError("preserved stage is not complete: %s" % stage_id)

    reset = tuple(stage_id for stage_id in affected if stage_id not in preserved)
    _clear_agent_request(status)
    reset_set = set(reset)
    for asset_id, asset in list(status.get("assets", {}).items()):
        if asset.get("stage") in reset_set:
            archive_asset(status, episode_dir, asset_id)
            status["assets"].pop(asset_id, None)

    for stage_id in reset:
        stage = status["stages"][stage_id]
        stage.update(
            {
                "state": "pending",
                "revision": stage["revision"] + 1,
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    for stage_id in preserved:
        status["stages"][stage_id]["state"] = "preserved"
    reset_stage_tasks(status, reset)
    if "story_design" in reset_set:
        status["creative_brief"]["shot_plan"] = {
            "state": "pending", "shot_count": None, "durations_sec": [], "reason": ""
        }
    if "story_design" in reset_set:
        status["creative_brief"]["storyboard_plan"] = {"state": "pending", "shots": []}
    if any(STAGE_IDS.index(stage_id) < STAGE_IDS.index("video_generation") for stage_id in reset):
        status["confirmations"]["video_generation"] = {
            "state": "required", "revision_fingerprint": None, "approved_at": None, "shots": {}
        }
    first_reset = reset[0] if reset else None
    status["run"].update(
        {
            "state": "idle",
            "current_stage": first_reset or _first_incomplete(status),
            "last_error": None,
            "waiting_for": None,
        }
    )
    return reset


def restart_preserving_stage(
    status: Dict[str, Any], episode_dir: Path, preserved_stage_id: str
) -> tuple:
    """Restart every stage except one explicitly preserved completed stage.

    This is used for a deliberate creative reset where one authoritative asset
    family, such as character design, must remain locked while the story and
    all other downstream interpretations are rebuilt.
    """
    if preserved_stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % preserved_stage_id)
    if status["stages"][preserved_stage_id]["state"] != "done":
        raise ValueError("preserved stage must be complete: %s" % preserved_stage_id)
    restore_intermediate_storage(status, episode_dir)

    affected = tuple(stage_id for stage_id in STAGE_IDS if stage_id != preserved_stage_id)
    _clear_agent_request(status)
    affected_set = set(affected)
    preserved_asset_paths = {
        asset["path"]
        for asset in status.get("assets", {}).values()
        if asset.get("stage") == preserved_stage_id
    }
    for asset_id, asset in list(status.get("assets", {}).items()):
        if asset.get("stage") not in affected_set:
            continue
        source = resolve_asset_path(episode_dir, asset["path"])
        if source.is_file():
            source.unlink()
        status["assets"].pop(asset_id, None)

    # This reset is explicitly destructive: remove orphaned canonical outputs,
    # run payloads, and non-preserved history from this exact episode only.
    for child in episode_dir.iterdir():
        if child.name in {"episode.json", "annotations", "repairs", "runs", "versions"}:
            continue
        if child.is_file() and child.relative_to(episode_dir).as_posix() not in preserved_asset_paths:
            child.unlink()
    runs_dir = episode_dir / "runs"
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    history = status.setdefault("asset_history", {})
    preserved_history = {
        history_id: item
        for history_id, item in history.items()
        if item.get("stage") == preserved_stage_id
    }
    versions_dir = episode_dir / "versions"
    preserved_history_paths = {
        item.get("path") for item in preserved_history.values() if item.get("path")
    }
    if versions_dir.exists():
        for child in versions_dir.rglob("*"):
            if child.is_file() and child.relative_to(episode_dir).as_posix() not in preserved_history_paths:
                child.unlink()
        for child in sorted(versions_dir.rglob("*"), reverse=True):
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
    status["asset_history"] = preserved_history

    for affected_id in affected:
        stage = status["stages"][affected_id]
        had_active_result = (
            stage["state"] != "pending"
            or bool(stage.get("outputs"))
            or any(
                asset.get("stage") == affected_id
                for asset in status.get("assets", {}).values()
            )
        )
        stage.update(
            {
                "state": "pending",
                "revision": stage["revision"] + (1 if had_active_result else 0),
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    reset_stage_tasks(status, affected)
    if "story_design" in affected:
        status["creative_brief"]["shot_plan"] = {
            "state": "pending",
            "shot_count": None,
            "durations_sec": [],
            "reason": "",
        }
    if "story_design" in affected:
        status["creative_brief"]["storyboard_plan"] = {
            "state": "pending",
            "shots": [],
        }
    status["confirmations"]["video_generation"] = {
        "state": "required",
        "revision_fingerprint": None,
        "approved_at": None,
        "shots": {},
    }
    first_affected = affected[0]
    status["run"].update(
        {"state": "idle", "current_stage": first_affected, "last_error": None}
    )
    return affected


def restart_for_global_style(
    status: Dict[str, Any], episode_dir: Path, style: str, extra_constraints: str = ""
) -> tuple:
    """Change the episode's global visual language and restart from story design.

    A style change invalidates every downstream interpretation: the story-design
    prompts, character/scene sheets, storyboard prompts, and all later outputs.
    Active assets are copied to immutable history before their canonical paths are
    reused by the new revision.
    """
    restore_intermediate_storage(status, episode_dir)
    expanded = expand_style(style, extra_constraints)
    active_assets = list(status.get("assets", {}).keys())
    for asset_id in active_assets:
        archive_asset(status, episode_dir, asset_id)
    affected = invalidate_from(status, "story_design")
    status["creative_brief"]["style"] = style
    status["creative_brief"]["style_constraints"] = expanded
    development_brief = status["creative_brief"].get("development_brief")
    if isinstance(development_brief, dict):
        visual_anchor = development_brief.get("visual_anchor")
        if isinstance(visual_anchor, str):
            development_brief["visual_anchor"] = visual_anchor.replace(
                "整体采用精细三维萌宠动画质感、古风服装建筑、电影化灯光和清晰喜剧表演。",
                "整体采用邵氏偏写实电影质感、真实毛发与古风服装建筑、35毫米摄影机和戏剧化硬光，保留清晰喜剧表演。",
            )
    status["run"].update(
        {"state": "idle", "current_stage": "story_design", "last_error": None}
    )
    return affected


def invalidate_downstream(status: Dict[str, Any], stage_id: str) -> tuple:
    """Invalidate only consumers of a changed stage-level asset manifest.

    This is used by selective asset regeneration: the owning stage is rebuilt as
    one complete manifest, while already-valid sibling assets remain reusable.
    """
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    affected = transitive_downstream(stage_id)[1:]
    _clear_agent_request(status)
    affected_set = set(affected)
    active_stages = {
        asset["stage"]
        for asset in status["assets"].values()
        if asset["stage"] in affected_set
    }
    status["assets"] = {
        asset_id: asset
        for asset_id, asset in status["assets"].items()
        if asset["stage"] not in affected_set
    }
    for affected_id in affected:
        stage = status["stages"][affected_id]
        had_active_result = (
            stage["state"] != "pending"
            or bool(stage.get("outputs"))
            or affected_id in active_stages
        )
        stage.update(
            {
                "state": "pending",
                "revision": stage["revision"] + (1 if had_active_result else 0),
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    reset_stage_tasks(status, affected)
    if STAGE_IDS.index(stage_id) < STAGE_IDS.index("video_generation"):
        status["confirmations"]["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
    return affected


def remove_character_image_asset(
    status: Dict[str, Any], episode_dir: Path, character_name: str
) -> tuple:
    """Remove one selected character image while preserving its text lock."""
    if status["stages"]["character_design"]["state"] not in {
        "pending", "running", "done", "paused"
    }:
        raise RuntimeError("character_design is not at a removable boundary")
    if not isinstance(character_name, str) or not character_name.strip():
        raise ValueError("character name is required")
    name = character_name.strip()
    if name.startswith("char_"):
        name = name[len("char_") :]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("invalid character name: %s" % character_name)

    image_name = "char_%s_sheet.png" % name
    prompt_name = "char_%s_prompt.txt" % name
    prompt_exists = any(
        asset.get("stage") == "character_design"
        and asset.get("path") == prompt_name
        and asset.get("kind") == "prompt"
        for asset in status["assets"].values()
    )
    if not prompt_exists:
        raise ValueError("unknown character: %s" % name)
    image_record = next(
        (
            (asset_id, asset)
            for asset_id, asset in status["assets"].items()
            if asset.get("stage") == "character_design"
            and asset.get("path") == image_name
            and asset.get("kind") == "image"
        ),
        None,
    )
    if image_record is None:
        raise ValueError("character image is already absent: %s" % image_name)

    image_id, _image_asset = image_record
    image_path = resolve_asset_path(episode_dir, image_name)
    archived = archive_asset(status, episode_dir, image_id)
    archived["removal_reason"] = "user_requested_character_image_removal"
    image_path.unlink()
    status["assets"].pop(image_id)

    stage = status["stages"]["character_design"]
    stage["outputs"] = [path for path in stage.get("outputs", []) if path != image_name]
    if stage.get("handoff"):
        stage["handoff"]["outputs"] = [
            path for path in stage["handoff"].get("outputs", []) if path != image_name
        ]

    task = status.get("tasks", {}).get("character_design:char_%s" % name)
    if task is not None:
        task["asset_ids"] = [asset_id for asset_id in task.get("asset_ids", []) if asset_id != image_id]
        set_task_state(
            status,
            "character_design",
            "char_%s" % name,
            "passed",
            review_reason="角色文字设定保留，用户移除定妆图片",
        )

    affected = invalidate_downstream(status, "character_design")
    return image_name, image_id, affected


def remove_prop_image_asset(
    status: Dict[str, Any], episode_dir: Path, prop_name: str
) -> tuple:
    """Remove one selected prop design image while preserving textual locks."""
    if status["stages"]["visual_design"]["state"] != "done":
        raise RuntimeError("visual_design must be complete before removing an image")
    if not isinstance(prop_name, str) or not prop_name.strip():
        raise ValueError("prop name is required")
    name = prop_name.strip()
    if name.startswith("prop_"):
        name = name[len("prop_") :]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("invalid prop name: %s" % prop_name)
    image_name = "prop_%s_sheet.png" % name
    image_record = next(
        (
            (asset_id, asset)
            for asset_id, asset in status["assets"].items()
            if asset.get("stage") == "visual_design"
            and asset.get("path") == image_name
            and asset.get("kind") == "image"
        ),
        None,
    )
    if image_record is None:
        raise ValueError("prop image is already absent: %s" % image_name)
    image_id, _image_asset = image_record
    image_path = resolve_asset_path(episode_dir, image_name)
    archived = archive_asset(status, episode_dir, image_id)
    archived["removal_reason"] = "user_requested_prop_image_removal"
    if image_path.exists():
        image_path.unlink()
    status["assets"].pop(image_id)
    stage = status["stages"]["visual_design"]
    stage["outputs"] = [path for path in stage.get("outputs", []) if path != image_name]
    if stage.get("handoff"):
        stage["handoff"]["outputs"] = [
            path for path in stage["handoff"].get("outputs", []) if path != image_name
        ]
    for task in status.get("tasks", {}).values():
        task["asset_ids"] = [asset_id for asset_id in task.get("asset_ids", []) if asset_id != image_id]
    affected = invalidate_downstream(status, "visual_design")
    return image_name, image_id, affected


def resume_stage_from_checkpoint(
    status: Dict[str, Any], stage_id: str, reusable_asset_ids: Iterable[str]
) -> tuple:
    """Retry an incomplete stage without discarding verified checkpointed work.

    This is intentionally separate from ``invalidate_from``: a user-requested
    regeneration replaces the selected stage revision, while a transient provider
    retry continues the same revision and attempt history.
    """
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    stage = status["stages"][stage_id]
    _clear_agent_request(status)
    if stage["state"] not in {"blocked", "running", "reviewing", "repairing"}:
        raise ValueError("only a blocked or interrupted stage can resume partial work")

    reusable = set(reusable_asset_ids)
    unknown = reusable - set(status["assets"])
    if unknown:
        raise ValueError("reusable assets are not active: %s" % ", ".join(sorted(unknown)))
    wrong_stage = sorted(
        asset_id
        for asset_id in reusable
        if status["assets"][asset_id]["stage"] != stage_id
    )
    if wrong_stage:
        raise ValueError(
            "reusable assets belong to another stage: %s" % ", ".join(wrong_stage)
        )

    affected = transitive_downstream(stage_id)
    downstream = set(affected) - {stage_id}
    downstream_with_assets = {
        asset["stage"]
        for asset in status["assets"].values()
        if asset["stage"] in downstream
    }
    status["assets"] = {
        asset_id: asset
        for asset_id, asset in status["assets"].items()
        if asset["stage"] not in downstream
        and (asset["stage"] != stage_id or asset_id in reusable)
    }
    for affected_id in affected:
        if affected_id == stage_id:
            continue
        affected_stage = status["stages"][affected_id]
        had_active_result = (
            affected_stage["state"] != "pending"
            or bool(affected_stage.get("outputs"))
            or affected_id in downstream_with_assets
        )
        affected_stage.update(
            {
                "state": "pending",
                "revision": affected_stage["revision"]
                + (1 if had_active_result else 0),
                "started_at": None,
                "completed_at": None,
                "inputs": [],
                "outputs": [],
                "handoff": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "review": {"state": "pending", "reason": ""},
                "error": None,
            }
        )
    reset_stage_tasks(status, downstream)

    # Preserve the current revision, model usage, inputs, and passed review. They
    # describe the text design being reused. begin_stage will increment attempt.
    stage.update(
        {
            "state": "pending",
            "started_at": None,
            "completed_at": None,
            "outputs": [],
            "error": None,
        }
    )
    if STAGE_IDS.index(stage_id) < STAGE_IDS.index("video_generation"):
        status["confirmations"]["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
    status["run"].update(
        {"state": "idle", "current_stage": stage_id, "last_error": None}
    )
    return affected
