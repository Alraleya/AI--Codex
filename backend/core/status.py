import copy
import hashlib
import json
import os
import re
import tempfile
import time
from numbers import Real
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .flow import FLOW_ID, FLOW_VERSION, STAGE_IDS, STAGES
from .models import DEFAULT_MODEL, MODEL_OPTIONS
from .status_time import utc_now
from .style import expand_style
from .tasks import ensure_task, initial_tasks, link_task_asset, validate_tasks


SCHEMA_VERSION = "1.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
STAGE_STATES = {
    "pending",
    "running",
    "reviewing",
    "repairing",
    "waiting_confirmation",
    "done",
    "skipped",
    "preserved",
    "blocked",
}
RUN_STATES = {
    "idle",
    "running",
    "paused",
    "waiting_agent",
    "waiting_user_decision",
    "waiting_confirmation",
    "blocked",
    "done",
}
IMAGE_PROVIDERS = {"codex_imagegen", "jimeng_image_cli"}
MIN_VIDEO_SHOT_DURATION_SEC = 4
MAX_VIDEO_SHOT_DURATION_SEC = 15
SHOT_DURATION_EXCEPTION_FIELDS = {"min_sec", "reason"}
DEVELOPMENT_BRIEF_FIELDS = {
    "logline",
    "protagonist",
    "goal",
    "obstacle",
    "stakes",
    "emotional_turn",
    "beats",
    "ending_payoff",
    "visual_anchor",
    "continuity_checks",
}
SHOT_PLAN_FIELDS = {"state", "shot_count", "durations_sec", "reason"}
STORYBOARD_PLAN_FIELDS = {"state", "shots"}
STORYBOARD_SHOT_FIELDS = {
    "shot_number",
    "columns",
    "rows",
    "panel_count",
    "timepoints_sec",
    "reason",
}
STORYBOARD_SHOT_OPTIONAL_FIELDS = {"storyboard_required"}
ANNOTATION_STATES = {"clear", "pending"}
REPAIR_STATES = {"clear", "planned", "processing", "blocked"}
AGENT_REQUEST_STATES = {"clear", "waiting"}
ASSET_REUSE_STATES = {"not_ready", "waiting_confirmation", "confirmed"}
PRODUCTION_MODES = {"pending", "storyboard", "dialogue_direct"}
STORYBOARD_DECISIONS = {"pending", "yes", "no"}
VIDEO_MODEL_OPTIONS = {"pending", "fast", "mini"}
VIDEO_SESSION_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
ASPECT_RATIO_PATTERN = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
SCRIPT_STYLE_PATTERN = re.compile(
    r"^\s*(?:[#>*\-\s]*)?(?:视觉风格|画面风格|风格)\s*[:：]\s*(.*?)\s*$"
)
SCRIPT_ASPECT_RATIO_PATTERN = re.compile(
    r"^\s*(?:[#>*\-\s]*)?(?:画幅|画面比例|宽高比)\s*[:：]\s*([1-9][0-9]*:[1-9][0-9]*)\s*$"
)
SCRIPT_DURATION_PATTERN = re.compile(
    r"^\s*(?:[#>*\-\s]*)?(?:总时长|目标时长)\s*[:：]\s*([1-9][0-9]*(?:\.[0-9]+)?)\s*(?:秒|S|s)?\s*$"
)


def normalize_production_mode(value: Any) -> str:
    if value not in PRODUCTION_MODES:
        raise ValueError("production_mode must be pending, storyboard, or dialogue_direct")
    return str(value)


def normalize_storyboard_decision(value: Any) -> str:
    if value not in STORYBOARD_DECISIONS:
        raise ValueError("storyboard_decision must be pending, yes, or no")
    return str(value)


def normalize_video_model(value: Any) -> str:
    if value not in VIDEO_MODEL_OPTIONS:
        raise ValueError("video_model must be pending, fast, or mini")
    return str(value)


def normalize_video_session_id(value: Any) -> Optional[str]:
    """Validate the episode-scoped Dreamina session identifier."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not VIDEO_SESSION_ID_PATTERN.fullmatch(value):
        raise ValueError("video_session_id must contain only digits")
    return value


def normalize_aspect_ratio(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("aspect_ratio must be a string")
    value = value.strip()
    if not ASPECT_RATIO_PATTERN.fullmatch(value):
        raise ValueError("aspect_ratio must use the form WIDTH:HEIGHT")
    return value


def _script_labeled_value(script: Optional[str], pattern: re.Pattern) -> Optional[str]:
    if not isinstance(script, str):
        return None
    for line in script.splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip(" `*_\t")
            return value or None
    return None


def extract_script_style(script: Optional[str]) -> Optional[str]:
    """Read an explicit style line without inventing a visual direction."""
    return _script_labeled_value(script, SCRIPT_STYLE_PATTERN)


def extract_script_aspect_ratio(script: Optional[str]) -> Optional[str]:
    value = _script_labeled_value(script, SCRIPT_ASPECT_RATIO_PATTERN)
    return normalize_aspect_ratio(value)


def extract_script_duration(script: Optional[str]) -> Optional[Real]:
    value = _script_labeled_value(script, SCRIPT_DURATION_PATTERN)
    if not value:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def new_video_session_id() -> str:
    """Allocate an episode-scoped numeric provider session id before production."""
    return str(time.time_ns())


def is_dialogue_direct(creative_brief: Mapping[str, Any]) -> bool:
    return (
        creative_brief.get("production_mode", "storyboard") == "dialogue_direct"
        or creative_brief.get("storyboard_decision", "yes") == "no"
    )


def normalize_shot_duration_exception(value: Any) -> Optional[Dict[str, Any]]:
    """Validate a user-approved per-episode reduction of the shot floor."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value.keys()) != SHOT_DURATION_EXCEPTION_FIELDS:
        raise ValueError("shot_duration_exception has unexpected fields")
    min_sec = value.get("min_sec")
    if isinstance(min_sec, bool) or not isinstance(min_sec, int) or min_sec != 3:
        raise ValueError("shot_duration_exception min_sec must be 3")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("shot_duration_exception reason is required")
    if len(reason.strip()) > 500:
        raise ValueError("shot_duration_exception reason is too long")
    return {"min_sec": min_sec, "reason": reason.strip()}


def min_video_shot_duration(creative_brief: Mapping[str, Any]) -> int:
    exception = normalize_shot_duration_exception(
        creative_brief.get("shot_duration_exception")
    )
    return exception["min_sec"] if exception is not None else MIN_VIDEO_SHOT_DURATION_SEC


def empty_annotation_summary() -> Dict[str, Any]:
    """Return the token-light annotation cursor embedded in status.json."""
    return {
        "state": "clear",
        "latest_revision": 0,
        "handled_revision": 0,
        "pending_count": 0,
        "redo_count": 0,
        "manifest_path": "annotations/index.json",
        "updated_at": None,
    }


def empty_repair_summary() -> Dict[str, Any]:
    return {
        "state": "clear",
        "latest_revision": 0,
        "active_plan_id": None,
        "manifest_path": "repairs/index.json",
        "updated_at": None,
    }


def empty_agent_request() -> Dict[str, Any]:
    """Return the authoritative handoff slot for a native Codex child Agent.

    The request is intentionally stored in status.json rather than a queue or a
    runner-owned database.  A workbench can therefore remain read-only and a
    restarted Codex coordinator can resume the exact same review request.
    """
    return {"state": "clear", "request": None}


def empty_asset_reuse() -> Dict[str, Any]:
    """Return the episode-level, user-confirmed design-asset reuse decision."""
    return {
        "state": "not_ready",
        "asset_plan_sha256": None,
        "items": [],
        "proposed_at": None,
        "confirmed_at": None,
    }


def empty_incremental_request() -> Dict[str, Any]:
    """Return the confirmation-gated, shot-local production request slot."""
    return {"state": "clear", "request": None}


def normalize_shot_plan(
    value: Mapping[str, Any],
    target_duration_sec: Real,
    *,
    require_resolved: Optional[bool] = None,
    enforce_video_duration_limits: bool = False,
    min_duration_sec: int = MIN_VIDEO_SHOT_DURATION_SEC,
) -> Dict[str, Any]:
    """Validate the model-owned shot plan stored inside the creative brief."""
    if not isinstance(value, Mapping) or set(value.keys()) != SHOT_PLAN_FIELDS:
        raise ValueError("shot_plan has unexpected fields")
    state = value.get("state")
    if state not in {"pending", "resolved"}:
        raise ValueError("shot_plan state is invalid")
    if require_resolved is True and state != "resolved":
        raise ValueError("story_design must resolve the shot plan")
    if require_resolved is False and state != "pending":
        raise ValueError("new episodes must begin with a pending shot plan")

    if state == "pending":
        if value.get("shot_count") is not None or value.get("durations_sec") != []:
            raise ValueError("pending shot_plan cannot contain shots")
        if value.get("reason") not in {"", None}:
            raise ValueError("pending shot_plan cannot contain a decision reason")
        return {
            "state": "pending",
            "shot_count": None,
            "durations_sec": [],
            "reason": "",
        }

    shot_count = value.get("shot_count")
    if isinstance(shot_count, bool) or not isinstance(shot_count, int):
        raise ValueError("shot_plan shot_count must be an integer")
    if not 1 <= shot_count <= 30:
        raise ValueError("shot_plan shot_count must be between 1 and 30")
    durations = value.get("durations_sec")
    if not isinstance(durations, list) or len(durations) != shot_count:
        raise ValueError("shot_plan durations must match shot_count")
    normalized_durations = []
    for duration in durations:
        if isinstance(duration, bool) or not isinstance(duration, Real) or duration <= 0:
            raise ValueError("shot_plan durations must be positive numbers")
        number = float(duration)
        if enforce_video_duration_limits and not (min_duration_sec <= number <= MAX_VIDEO_SHOT_DURATION_SEC):
            raise ValueError(
                "each new video shot must be between %d and %d seconds"
                % (min_duration_sec, MAX_VIDEO_SHOT_DURATION_SEC)
            )
        normalized_durations.append(int(number) if number.is_integer() else number)
    if abs(sum(normalized_durations) - target_duration_sec) > 0.001:
        raise ValueError("shot_plan durations must sum to target_duration_sec")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("shot_plan reason is required")
    if len(reason.strip()) > 1000:
        raise ValueError("shot_plan reason is too long")
    return {
        "state": "resolved",
        "shot_count": shot_count,
        "durations_sec": normalized_durations,
        "reason": reason.strip(),
    }


def resolved_shot_count(creative_brief: Mapping[str, Any]) -> int:
    plan = normalize_shot_plan(
        creative_brief.get("shot_plan"),
        creative_brief["target_duration_sec"],
        require_resolved=True,
        min_duration_sec=min_video_shot_duration(creative_brief),
    )
    return int(plan["shot_count"])


def normalize_storyboard_plan(
    value: Mapping[str, Any],
    shot_plan: Mapping[str, Any],
    *,
    require_resolved: Optional[bool] = None,
    min_duration_sec: int = MIN_VIDEO_SHOT_DURATION_SEC,
) -> Dict[str, Any]:
    """Validate the first-stage locked multi-panel plan for every video unit."""
    if not isinstance(value, Mapping) or set(value.keys()) != STORYBOARD_PLAN_FIELDS:
        raise ValueError("storyboard_plan has unexpected fields")
    state = value.get("state")
    if state not in {"pending", "resolved"}:
        raise ValueError("storyboard_plan state is invalid")
    if require_resolved is True and state != "resolved":
        raise ValueError("story_design must resolve the storyboard plan")
    if require_resolved is False and state != "pending":
        raise ValueError("storyboard_plan must begin pending")
    shots = value.get("shots")
    if not isinstance(shots, list):
        raise ValueError("storyboard_plan shots must be a list")
    if state == "pending":
        if shots:
            raise ValueError("pending storyboard_plan cannot contain shots")
        return {"state": "pending", "shots": []}

    resolved_shots = normalize_shot_plan(
        shot_plan,
        int(round(sum(shot_plan.get("durations_sec", [])))),
        require_resolved=True,
        min_duration_sec=min_duration_sec,
    )
    if len(shots) != resolved_shots["shot_count"]:
        raise ValueError("storyboard_plan shots must match shot_count")
    normalized_shots = []
    for expected_number, (item, duration) in enumerate(
        zip(shots, resolved_shots["durations_sec"]), 1
    ):
        if not isinstance(item, Mapping) or not STORYBOARD_SHOT_FIELDS.issubset(item.keys()):
            raise ValueError("storyboard_plan shot has unexpected fields")
        if set(item.keys()) - STORYBOARD_SHOT_FIELDS - STORYBOARD_SHOT_OPTIONAL_FIELDS:
            raise ValueError("storyboard_plan shot has unexpected fields")
        if item.get("shot_number") != expected_number:
            raise ValueError("storyboard_plan shot numbers must be sequential")
        storyboard_required = item.get("storyboard_required", True)
        if not isinstance(storyboard_required, bool):
            raise ValueError("storyboard_required must be a boolean")
        columns = item.get("columns")
        rows = item.get("rows")
        panel_count = item.get("panel_count")
        for field, number in (("columns", columns), ("rows", rows), ("panel_count", panel_count)):
            if isinstance(number, bool) or not isinstance(number, int):
                raise ValueError("storyboard_plan %s must be an integer" % field)
        if not 2 <= columns <= 4 or not 2 <= rows <= 4:
            raise ValueError("storyboard grid must use 2-4 columns and 2-4 rows")
        if panel_count != columns * rows or not 4 <= panel_count <= 12:
            raise ValueError("storyboard panel_count must equal grid size and be 4-12")
        timepoints = item.get("timepoints_sec")
        if not isinstance(timepoints, list) or len(timepoints) != panel_count:
            raise ValueError("storyboard timepoints must match panel_count")
        normalized_timepoints = []
        previous = None
        for point in timepoints:
            if isinstance(point, bool) or not isinstance(point, Real):
                raise ValueError("storyboard timepoints must be numbers")
            number = float(point)
            if number < 0 or number > float(duration):
                raise ValueError("storyboard timepoint is outside shot duration")
            if previous is not None and number <= previous:
                raise ValueError("storyboard timepoints must increase strictly")
            normalized_timepoints.append(int(number) if number.is_integer() else number)
            previous = number
        if abs(float(normalized_timepoints[0])) > 0.001:
            raise ValueError("storyboard P01 must be exactly shot-local 0.0 seconds")
        if abs(float(normalized_timepoints[-1]) - float(duration)) > 0.001:
            raise ValueError("storyboard last panel must equal the shot duration")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("storyboard grid decision reason is required")
        if len(reason.strip()) > 1000:
            raise ValueError("storyboard grid decision reason is too long")
        normalized_shots.append(
            {
                "shot_number": expected_number,
                "columns": columns,
                "rows": rows,
                "panel_count": panel_count,
                "timepoints_sec": normalized_timepoints,
                "reason": reason.strip(),
                "storyboard_required": storyboard_required,
            }
        )
    return {"state": "resolved", "shots": normalized_shots}


def resolved_storyboard_shots(creative_brief: Mapping[str, Any]) -> list:
    plan = normalize_storyboard_plan(
        creative_brief.get("storyboard_plan"),
        creative_brief.get("shot_plan"),
        require_resolved=True,
        min_duration_sec=min_video_shot_duration(creative_brief),
    )
    return plan["shots"]


def storyboard_required_shots(creative_brief: Mapping[str, Any]) -> list:
    """Return only shots whose planning result requires a rendered board."""
    return [shot for shot in resolved_storyboard_shots(creative_brief) if shot.get("storyboard_required", True)]


def normalize_development_brief(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value.keys()) != DEVELOPMENT_BRIEF_FIELDS:
        raise ValueError("development_brief has unexpected fields")

    def text(field: str, limit: int) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("development_brief %s is required" % field)
        item = item.strip()
        if len(item) > limit:
            raise ValueError("development_brief %s is too long" % field)
        return item

    beats = value.get("beats")
    if not isinstance(beats, list) or not 1 <= len(beats) <= 8:
        raise ValueError("development_brief beats must contain between 1 and 8 items")
    normalized_beats = []
    beat_fields = {"time_range", "visual_action", "dramatic_purpose", "sound_cue"}
    for beat in beats:
        if not isinstance(beat, Mapping) or set(beat.keys()) != beat_fields:
            raise ValueError("development_brief beat has unexpected fields")

        def beat_text(field: str, limit: int) -> str:
            item = beat.get(field)
            if not isinstance(item, str) or not item.strip():
                raise ValueError("development_brief beat %s is required" % field)
            item = item.strip()
            if len(item) > limit:
                raise ValueError("development_brief beat %s is too long" % field)
            return item

        normalized_beats.append(
            {
                "time_range": beat_text("time_range", 60),
                "visual_action": beat_text("visual_action", 1000),
                "dramatic_purpose": beat_text("dramatic_purpose", 500),
                "sound_cue": beat_text("sound_cue", 500),
            }
        )

    checks = value.get("continuity_checks")
    if not isinstance(checks, list) or len(checks) > 6:
        raise ValueError("development_brief continuity_checks is invalid")
    normalized_checks = []
    for item in checks:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
            raise ValueError("development_brief continuity_checks contains an invalid item")
        normalized_checks.append(item.strip())

    return {
        "logline": text("logline", 500),
        "protagonist": text("protagonist", 300),
        "goal": text("goal", 500),
        "obstacle": text("obstacle", 500),
        "stakes": text("stakes", 500),
        "emotional_turn": text("emotional_turn", 500),
        "beats": normalized_beats,
        "ending_payoff": text("ending_payoff", 700),
        "visual_anchor": text("visual_anchor", 700),
        "continuity_checks": normalized_checks,
    }


def validate_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(
            "%s must start with an ASCII letter or digit and contain only letters, digits, _ or -"
            % field
        )
    return value


def make_creative_brief(
    topic: str,
    hook: str,
    style: Optional[str] = None,
    # The command-level creator passes None when the user omitted duration;
    # the legacy in-process helper keeps its historical value for callers that
    # already provide a complete creative brief directly.
    target_duration_sec: Optional[Real] = 7,
    aspect_ratio: Optional[str] = None,
    extra_style_constraints: str = "",
    development_brief: Optional[Mapping[str, Any]] = None,
    provided_script: Optional[str] = None,
    production_mode: Optional[str] = None,
    storyboard_decision: Optional[str] = None,
    video_model: Optional[str] = None,
    video_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not topic.strip():
        raise ValueError("topic is required")
    if provided_script is not None:
        if not isinstance(provided_script, str) or not provided_script.strip():
            raise ValueError("provided_script must be a non-empty string")
        if len(provided_script) > 200_000:
            raise ValueError("provided_script is too long")
    script_style = extract_script_style(provided_script)
    if style is None:
        style = script_style
    elif script_style and style.strip() != script_style:
        raise ValueError("style conflicts with the explicit style in provided_script")
    script_duration = extract_script_duration(provided_script)
    if script_duration is not None and target_duration_sec in {None, 7}:
        # A provided script is the content authority.  The legacy in-process
        # helper used 7 seconds as a default, so an explicit script duration
        # must still win over that compatibility value.
        target_duration_sec = script_duration
    if target_duration_sec is None:
        raise ValueError(
            "target_duration_sec is required; provide it explicitly or add 总时长 to the script"
        )
    if (
        isinstance(target_duration_sec, bool)
        or not isinstance(target_duration_sec, Real)
        or target_duration_sec < MIN_VIDEO_SHOT_DURATION_SEC
    ):
        raise ValueError(
            "new episode target_duration_sec must be at least %d seconds"
            % MIN_VIDEO_SHOT_DURATION_SEC
        )
    script_aspect_ratio = extract_script_aspect_ratio(provided_script)
    if aspect_ratio is None:
        aspect_ratio = script_aspect_ratio
    elif script_aspect_ratio and aspect_ratio.strip() != script_aspect_ratio:
        raise ValueError("aspect_ratio conflicts with the explicit ratio in provided_script")
    aspect_ratio = normalize_aspect_ratio(aspect_ratio)
    if storyboard_decision is None:
        storyboard_decision = {
            "pending": "pending",
            "storyboard": "yes",
            "dialogue_direct": "no",
        }.get(production_mode, "no")
    if production_mode is None:
        production_mode = {
            "yes": "storyboard",
            "no": "dialogue_direct",
        }.get(storyboard_decision, "pending")
    production_mode = normalize_production_mode(production_mode)
    storyboard_decision = normalize_storyboard_decision(storyboard_decision)
    video_model = "pending" if video_model is None else video_model
    video_model = normalize_video_model(video_model)
    video_session_id = normalize_video_session_id(video_session_id)
    if video_session_id is None:
        video_session_id = new_video_session_id()
    brief = {
        "topic": topic.strip(),
        "hook": hook.strip(),
        "style": style,
        "style_constraints": (
            expand_style(style, extra_style_constraints) if style else ""
        ),
        "target_duration_sec": target_duration_sec,
        "shot_plan": {
            "state": "pending",
            "shot_count": None,
            "durations_sec": [],
            "reason": "",
        },
        "storyboard_plan": {"state": "pending", "shots": []},
        "aspect_ratio": aspect_ratio,
        "production_mode": production_mode,
        "storyboard_decision": storyboard_decision,
        "video_model": video_model,
        "video_session_id": video_session_id,
    }
    if development_brief is not None:
        brief["development_brief"] = normalize_development_brief(development_brief)
    if provided_script is not None:
        # This is intentionally stored verbatim. The first stage may design
        # around it, but it is not allowed to rewrite the user's script.
        brief["provided_script"] = provided_script
    return brief


def _new_stage(stage: Any) -> Dict[str, Any]:
    return {
        "label": stage.label,
        "state": "pending",
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
        # Short handoff for the next node; canonical files remain the source of truth.
        "handoff": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "review": {"state": "pending", "reason": ""},
        "error": None,
    }


def new_status(
    project_id: str,
    episode_id: str,
    creative_brief: Mapping[str, Any],
    image_provider: str = "codex_imagegen",
) -> Dict[str, Any]:
    validate_id(project_id, "project_id")
    validate_id(episode_id, "episode_id")
    target_duration = creative_brief.get("target_duration_sec")
    if (
        isinstance(target_duration, bool)
        or not isinstance(target_duration, Real)
        or target_duration < MIN_VIDEO_SHOT_DURATION_SEC
    ):
        raise ValueError(
            "new episode target_duration_sec must be at least %d seconds"
            % MIN_VIDEO_SHOT_DURATION_SEC
        )
    if image_provider not in IMAGE_PROVIDERS:
        raise ValueError("unsupported image provider: %s" % image_provider)
    now = utc_now()
    provided_script = creative_brief.get("provided_script")
    status = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "episode_id": episode_id,
        "flow_id": FLOW_ID,
        "flow_version": FLOW_VERSION,
        "creative_brief": copy.deepcopy(dict(creative_brief)),
        "providers": {
            "text": "codex",
            "image": image_provider,
            "video": "jimeng_cli",
            "model": DEFAULT_MODEL,
        },
        "run": {
            "state": "idle",
            "current_stage": STAGE_IDS[0],
            "last_error": None,
            "waiting_for": None,
        },
        "stages": {stage.id: _new_stage(stage) for stage in STAGES},
        "tasks": initial_tasks(),
        "assets": {},
        "asset_history": {},
        "asset_reuse": empty_asset_reuse(),
        "annotations": empty_annotation_summary(),
        "repairs": empty_repair_summary(),
        "agent_request": empty_agent_request(),
        "incremental_request": empty_incremental_request(),
        "script_lock": (
            {
                "state": "locked",
                "source_path": "inputs/original_script.md",
                "sha256": hashlib.sha256(provided_script.encode("utf-8")).hexdigest(),
            }
            if isinstance(provided_script, str)
            else None
        ),
        "confirmations": {
            "video_generation": {
                "state": "required",
                "revision_fingerprint": None,
                "approved_at": None,
                "shots": {},
            }
        },
        "metrics": {
            "input_tokens": 0,
            "output_tokens": 0,
            "image_generations": 0,
            "video_generations": 0,
        },
        "created_at": now,
        "updated_at": now,
    }
    validate_status(status)
    return status


def validate_status(status: Mapping[str, Any]) -> None:
    if status.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    validate_id(status.get("project_id"), "project_id")
    validate_id(status.get("episode_id"), "episode_id")
    if status.get("flow_id") != FLOW_ID or status.get("flow_version") != FLOW_VERSION:
        raise ValueError("unsupported flow")
    creative_brief = status.get("creative_brief")
    if not isinstance(creative_brief, Mapping):
        raise ValueError("creative_brief must be an object")
    style = creative_brief.get("style")
    style_constraints = creative_brief.get("style_constraints", "")
    if style is not None:
        if not isinstance(style, str) or not style.strip():
            raise ValueError("style must be a non-empty string or null")
        if not isinstance(style_constraints, str) or not style_constraints.strip():
            raise ValueError("style_constraints is required after style selection")
    elif style_constraints not in {"", None}:
        raise ValueError("style_constraints must be empty while style is pending")
    normalize_aspect_ratio(creative_brief.get("aspect_ratio"))
    target_duration = creative_brief.get("target_duration_sec")
    if isinstance(target_duration, bool) or not isinstance(target_duration, Real):
        raise ValueError("target_duration_sec must be a number")
    # Persisted episodes remain readable even if they predate the 4-second
    # creation floor or contain already-resolved sub-4-second shots.
    if target_duration < 1:
        raise ValueError(
            "target_duration_sec must be at least 1 second"
        )
    normalize_shot_duration_exception(creative_brief.get("shot_duration_exception"))
    min_duration_sec = min_video_shot_duration(creative_brief)
    normalize_shot_plan(
        creative_brief.get("shot_plan"),
        target_duration,
        min_duration_sec=min_duration_sec,
    )
    normalize_storyboard_plan(
        creative_brief.get("storyboard_plan"),
        creative_brief.get("shot_plan"),
        min_duration_sec=min_duration_sec,
    )
    if "development_brief" in creative_brief:
        normalize_development_brief(creative_brief["development_brief"])
    if "provided_script" in creative_brief:
        provided_script = creative_brief["provided_script"]
        if not isinstance(provided_script, str) or not provided_script.strip():
            raise ValueError("provided_script must be a non-empty string")
        if len(provided_script) > 200_000:
            raise ValueError("provided_script is too long")
        script_lock = status.get("script_lock")
        if not isinstance(script_lock, Mapping) or script_lock.get("state") != "locked":
            raise ValueError("provided_script requires a locked script_lock")
        if script_lock.get("source_path") != "inputs/original_script.md":
            raise ValueError("script_lock source_path is invalid")
        if script_lock.get("sha256") != hashlib.sha256(provided_script.encode("utf-8")).hexdigest():
            raise ValueError("script_lock sha256 does not match provided_script")
    normalize_production_mode(creative_brief.get("production_mode", "storyboard"))
    normalize_storyboard_decision(creative_brief.get("storyboard_decision", "yes"))
    normalize_video_model(creative_brief.get("video_model", "fast"))
    normalize_video_session_id(creative_brief.get("video_session_id"))
    stages = status.get("stages")
    if not isinstance(stages, dict) or tuple(stages.keys()) != STAGE_IDS:
        raise ValueError("status must contain all flow stages in order")
    for definition in STAGES:
        stage = stages[definition.id]
        if stage.get("state") not in STAGE_STATES:
            raise ValueError("invalid state for stage %s" % definition.id)
        if tuple(stage.get("depends_on", ())) != definition.depends_on:
            raise ValueError("dependency drift for stage %s" % definition.id)
        if stage.get("execution_mode") != definition.execution_mode:
            raise ValueError("execution mode drift for stage %s" % definition.id)
        if stage.get("task_unit") != definition.task_unit:
            raise ValueError("task unit drift for stage %s" % definition.id)
        handoff = stage.get("handoff")
        if handoff is not None:
            if not isinstance(handoff, Mapping):
                raise ValueError("stage handoff must be an object or null")
            if set(handoff) != {"summary", "outputs"}:
                raise ValueError("stage handoff has unexpected fields")
            summary = handoff.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("stage handoff summary is required")
            if len(summary) > 1200:
                raise ValueError("stage handoff summary is too long")
            outputs = handoff.get("outputs")
            if not isinstance(outputs, list) or any(
                not isinstance(path, str) or not path.strip()
                for path in outputs
            ):
                raise ValueError("stage handoff outputs is invalid")
    validate_tasks(status.get("tasks"))
    run = status.get("run", {})
    if run.get("state") not in RUN_STATES:
        raise ValueError("invalid run state")
    current_stage = run.get("current_stage")
    if current_stage is not None and current_stage not in stages:
        raise ValueError("invalid current_stage")
    providers = status.get("providers", {})
    if providers.get("text") != "codex":
        raise ValueError("text provider must be codex")
    if providers.get("image") not in IMAGE_PROVIDERS:
        raise ValueError("invalid image provider")
    if providers.get("video") != "jimeng_cli":
        raise ValueError("video provider must be jimeng_cli")
    model = providers.get("model", DEFAULT_MODEL)
    if model not in MODEL_OPTIONS:
        raise ValueError("invalid Codex model")
    if not isinstance(status.get("assets"), dict):
        raise ValueError("assets must be an object")
    if not isinstance(status.get("asset_history"), dict):
        raise ValueError("asset_history must be an object")
    asset_reuse = status.get("asset_reuse")
    if not isinstance(asset_reuse, Mapping):
        raise ValueError("asset_reuse must be an object")
    if set(asset_reuse) != {
        "state",
        "asset_plan_sha256",
        "items",
        "proposed_at",
        "confirmed_at",
    }:
        raise ValueError("asset_reuse has unexpected fields")
    if asset_reuse.get("state") not in ASSET_REUSE_STATES:
        raise ValueError("invalid asset_reuse state")
    plan_sha = asset_reuse.get("asset_plan_sha256")
    if plan_sha is not None and (
        not isinstance(plan_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha)
    ):
        raise ValueError("asset_reuse asset_plan_sha256 is invalid")
    reuse_items = asset_reuse.get("items")
    if not isinstance(reuse_items, list):
        raise ValueError("asset_reuse items must be a list")
    seen_reuse_ids = set()
    for item in reuse_items:
        if not isinstance(item, Mapping) or set(item) != {
            "asset_id", "type", "name", "recommended", "decision", "source"
        }:
            raise ValueError("asset_reuse item has unexpected fields")
        asset_id = item.get("asset_id")
        asset_type = item.get("type")
        if (
            not isinstance(asset_id, str)
            or asset_id in seen_reuse_ids
            or asset_type not in {"character", "scene", "prop"}
        ):
            raise ValueError("asset_reuse item identity is invalid")
        seen_reuse_ids.add(asset_id)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("asset_reuse item name is required")
        if item.get("recommended") not in {"reuse", "generate"}:
            raise ValueError("asset_reuse recommendation is invalid")
        decision_value = item.get("decision")
        if decision_value not in {None, "reuse", "generate"}:
            raise ValueError("asset_reuse decision is invalid")
        source = item.get("source")
        if source is not None:
            if not isinstance(source, Mapping) or set(source) != {
                "kind", "episode_id", "image_path", "image_sha256",
                "spec_path", "prompt_path"
            }:
                raise ValueError("asset_reuse source has unexpected fields")
            if source.get("kind") not in {"project_makeup", "previous_episode"}:
                raise ValueError("asset_reuse source kind is invalid")
            if source.get("episode_id") is not None and not isinstance(
                source.get("episode_id"), str
            ):
                raise ValueError("asset_reuse source episode_id is invalid")
            for field in ("image_path", "spec_path", "prompt_path"):
                if not isinstance(source.get(field), str) or not source[field].strip():
                    raise ValueError("asset_reuse source path is invalid")
            if not isinstance(source.get("image_sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", source["image_sha256"]
            ):
                raise ValueError("asset_reuse source sha256 is invalid")
        if item.get("recommended") == "reuse" and source is None:
            raise ValueError("reusable asset must have a source")
        if decision_value == "reuse" and source is None:
            raise ValueError("reuse decision must have a source")
    if asset_reuse["state"] == "not_ready":
        if plan_sha is not None or reuse_items:
            raise ValueError("not-ready asset_reuse cannot contain a proposal")
    else:
        if plan_sha is None:
            raise ValueError("asset_reuse proposal requires asset_plan_sha256")
        expected_decision = None if asset_reuse["state"] == "waiting_confirmation" else "set"
        if expected_decision is None and any(item["decision"] is not None for item in reuse_items):
            raise ValueError("unconfirmed asset_reuse cannot contain decisions")
        if expected_decision == "set" and any(item["decision"] is None for item in reuse_items):
            raise ValueError("confirmed asset_reuse requires every decision")
    agent_request = status.get("agent_request")
    if not isinstance(agent_request, Mapping):
        raise ValueError("agent_request must be an object")
    if agent_request.get("state") not in AGENT_REQUEST_STATES:
        raise ValueError("invalid agent request state")
    request_payload = agent_request.get("request")
    if agent_request["state"] == "clear":
        if request_payload is not None:
            raise ValueError("clear agent request cannot contain a request")
    else:
        if not isinstance(request_payload, Mapping):
            raise ValueError("waiting agent request must contain a request")
        for field in (
            "id",
            "kind",
            "stage_id",
            "task_id",
            "execution_id",
            "stage_revision",
            "created_at",
        ):
            if not request_payload.get(field):
                raise ValueError("agent request %s is required" % field)
        if request_payload.get("stage_id") not in stages:
            raise ValueError("agent request stage is invalid")
        if request_payload.get("stage_revision") != stages[request_payload["stage_id"]]["revision"]:
            raise ValueError("agent request stage revision is stale")
        task_key = "%s:%s" % (
            request_payload["stage_id"],
            request_payload["task_id"],
        )
        task = status.get("tasks", {}).get(task_key)
        if not isinstance(task, Mapping) or task.get("execution_id") != request_payload["execution_id"]:
            raise ValueError("agent request execution is stale")
    incremental_request = status.get("incremental_request", empty_incremental_request())
    if not isinstance(incremental_request, Mapping) or incremental_request.get("state") not in AGENT_REQUEST_STATES:
        raise ValueError("invalid incremental request state")
    if incremental_request.get("state") == "clear" and incremental_request.get("request") is not None:
        raise ValueError("clear incremental request cannot contain a request")
    annotations = status.get("annotations")
    if not isinstance(annotations, Mapping):
        raise ValueError("annotations must be an object")
    if annotations.get("state") not in ANNOTATION_STATES:
        raise ValueError("invalid annotation state")
    for field in ("latest_revision", "handled_revision", "pending_count", "redo_count"):
        value = annotations.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("annotations %s must be a non-negative integer" % field)
    if annotations["handled_revision"] > annotations["latest_revision"]:
        raise ValueError("handled annotation revision cannot exceed latest revision")
    if annotations.get("manifest_path") != "annotations/index.json":
        raise ValueError("invalid annotation manifest path")
    expected_state = "pending" if annotations["pending_count"] else "clear"
    if annotations["state"] != expected_state:
        raise ValueError("annotation state does not match pending_count")
    if annotations["pending_count"] > annotations["latest_revision"]:
        raise ValueError("annotation pending_count is invalid")
    if annotations["redo_count"] > annotations["pending_count"]:
        raise ValueError("annotation redo_count is invalid")
    updated_at = annotations.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise ValueError("annotations updated_at must be a string or null")
    repairs = status.get("repairs")
    if not isinstance(repairs, Mapping):
        raise ValueError("repairs must be an object")
    if repairs.get("state") not in REPAIR_STATES:
        raise ValueError("invalid repair state")
    latest_repair = repairs.get("latest_revision")
    if isinstance(latest_repair, bool) or not isinstance(latest_repair, int) or latest_repair < 0:
        raise ValueError("repairs latest_revision must be a non-negative integer")
    active_plan_id = repairs.get("active_plan_id")
    if active_plan_id is not None and not isinstance(active_plan_id, str):
        raise ValueError("repairs active_plan_id must be a string or null")
    if repairs.get("state") == "clear" and active_plan_id is not None:
        raise ValueError("clear repair state cannot have an active plan")
    if repairs.get("manifest_path") != "repairs/index.json":
        raise ValueError("invalid repair manifest path")


class StatusStore:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)

    def path_for(self, project_id: str, episode_id: str) -> Path:
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        return self.workspace_root / "status" / project_id / (episode_id + ".json")

    def load(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        path = self.path_for(project_id, episode_id)
        with path.open("r", encoding="utf-8") as handle:
            status = json.load(handle)
        # Episodes created before video-model selection remain runnable with
        # the fast channel without rewriting active assets until an explicit save.
        status.setdefault("providers", {}).setdefault("model", DEFAULT_MODEL)
        brief = status.setdefault("creative_brief", {})
        if "video_model" not in brief:
            # Existing episodes already past story design must remain runnable;
            # only new episodes pause for the explicit model decision.
            story_state = status.get("stages", {}).get("story_design", {}).get("state")
            brief["video_model"] = "pending" if story_state == "pending" else "fast"
        brief.setdefault("video_session_id", None)
        status.setdefault("run", {}).setdefault("waiting_for", None)
        confirmation = status.setdefault("confirmations", {}).setdefault(
            "video_generation",
            {
                "state": "required",
                "revision_fingerprint": None,
                "approved_at": None,
                "shots": {},
            },
        )
        confirmation.setdefault("shots", {})
        # Older episodes remain readable; the summary is persisted on the next
        # annotation or workflow write.
        annotation_summary = status.setdefault("annotations", empty_annotation_summary())
        annotation_summary.setdefault("redo_count", 0)
        status.setdefault("asset_history", {})
        status.setdefault("asset_reuse", empty_asset_reuse())
        status.setdefault("repairs", empty_repair_summary())
        status.setdefault("agent_request", empty_agent_request())
        # The initial native-agent protocol had a parallel handoff mirror and
        # per-task dispatch ledger.  The streamlined flow has one durable
        # handoff at a time, so discard these display-only fields on load.
        status.pop("parallel_requests", None)
        tasks = status.setdefault("tasks", initial_tasks())
        for key, task in tasks.items():
            task.setdefault("task_id", key)
            task.setdefault("execution_id", None)
            task.setdefault("execution_history", [])
        for definition in STAGES:
            stage = status["stages"][definition.id]
            stage.setdefault("execution_mode", definition.execution_mode)
            stage.setdefault("task_unit", definition.task_unit)
            stage.setdefault("handoff", None)
            if definition.execution_mode == "main_agent":
                default_key = "%s:%s" % (definition.id, definition.id)
                tasks.setdefault(default_key, initial_tasks()[default_key])
        for asset_id, asset in status.setdefault("assets", {}).items():
            stage_id = asset.get("stage")
            path = str(asset.get("path", ""))
            inferred_task = stage_id
            shot_match = re.match(r"^(shot\d{2})_", path)
            if shot_match and stage_id in {"storyboard_generation", "video_generation"}:
                inferred_task = shot_match.group(1)
            elif path.endswith("_sheet.png") and stage_id in {
                "character_design",
                "visual_design",
            }:
                inferred_task = path[: -len("_sheet.png")]
            if not asset.get("task_id") or (
                asset.get("task_id") == stage_id and inferred_task != stage_id
            ):
                asset["task_id"] = inferred_task
            asset.setdefault("asset_revision", 1)
            asset.setdefault("supersedes_revision", None)
            asset.setdefault("status", "active")
            ensure_task(status, stage_id, asset["task_id"])
            link_task_asset(status, stage_id, asset["task_id"], asset_id)
        for history_id, asset in status.setdefault("asset_history", {}).items():
            asset.setdefault("asset_revision", 1)
            asset.setdefault("supersedes_revision", None)
            asset.setdefault("status", "superseded")
        for task in status["tasks"].values():
            task.pop("dispatch", None)
            stage = status["stages"][task["stage"]]
            if stage["state"] in {"done", "skipped", "preserved"} and task["state"] == "pending":
                task.update(
                    {
                        "state": "passed",
                        "completed_at": stage.get("completed_at"),
                        "review": {
                            "state": "passed",
                            "reason": stage.get("review", {}).get("reason", "")
                            or "由已完成阶段迁移",
                        },
                        "error": None,
                    }
                )
        validate_status(status)
        return status

    def save(self, status: Dict[str, Any]) -> Path:
        validate_status(status)
        status["updated_at"] = utc_now()
        path = self.path_for(status["project_id"], status["episode_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(status, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return path
