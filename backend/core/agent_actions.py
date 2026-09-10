"""State-machine contracts for work performed by native Codex Agents."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .flow import STAGE_BY_ID
from .status_time import utc_now
from .tasks import ensure_task, set_task_state
ACTION_KINDS = {
    "stage_generation",
    "storyboard_sequence_review",
}


def clear_agent_request(status: Dict[str, Any]) -> None:
    status["agent_request"] = {"state": "clear", "request": None}


def request_agent(
    status: Dict[str, Any],
    *,
    kind: str,
    stage_id: str,
    task_id: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create the single outstanding, revision-bound native Agent request."""
    if kind not in ACTION_KINDS:
        raise ValueError("unknown native Agent request kind: %s" % kind)
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown native Agent request stage: %s" % stage_id)
    if status.get("agent_request", {}).get("state") == "waiting":
        raise RuntimeError("another native Agent request is already waiting")
    stage = status["stages"][stage_id]
    task_id = task_id or stage_id
    task = ensure_task(status, stage_id, task_id)
    if task["state"] != "running":
        set_task_state(status, stage_id, task_id, "running")
    execution_id = task.get("execution_id")
    if not execution_id:
        raise RuntimeError("task has no active execution: %s" % task["task_id"])
    identifier = "%s.%s" % (execution_id, kind)
    request = {
        "id": identifier,
        "kind": kind,
        "stage_id": stage_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "stage_revision": stage["revision"],
        "created_at": utc_now(),
        "payload": dict(payload or {}),
    }
    status["agent_request"] = {"state": "waiting", "request": request}
    status["run"].update(
        {"state": "waiting_agent", "current_stage": stage_id, "last_error": None}
    )
    status["stages"][stage_id]["state"] = "reviewing" if kind != "stage_generation" else "running"
    return request


def require_agent_request(
    status: Mapping[str, Any], request_id: str
) -> Dict[str, Any]:
    slot = status.get("agent_request", {})
    request = slot.get("request") if isinstance(slot, Mapping) else None
    if slot.get("state") != "waiting" or not isinstance(request, dict):
        raise RuntimeError("there is no native Agent request waiting")
    if request.get("id") != request_id:
        raise RuntimeError("native Agent result does not match the waiting request")
    stage_id = request.get("stage_id")
    if request.get("stage_revision") != status["stages"][stage_id]["revision"]:
        raise RuntimeError("native Agent request belongs to a stale stage revision")
    return request
