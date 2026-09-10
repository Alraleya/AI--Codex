from typing import Any, Dict, Iterable, Mapping, Optional

from .flow import STAGE_BY_ID
from .status_time import utc_now


TASK_STATES = {
    "pending",
    "queued",
    "running",
    "reviewing",
    "passed",
    "failed",
    "redo_requested",
}


def task_key(stage_id: str, task_id: str) -> str:
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown task stage: %s" % stage_id)
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id is required")
    return "%s:%s" % (stage_id, task_id.strip())


def new_task(
    stage_id: str,
    task_id: str,
    *,
    unit: Optional[str] = None,
    executor: Optional[str] = None,
) -> Dict[str, Any]:
    definition = STAGE_BY_ID[stage_id]
    return {
        "id": task_id,
        "task_id": task_key(stage_id, task_id),
        "stage": stage_id,
        "unit": unit or definition.task_unit,
        "executor": executor or definition.execution_mode,
        "state": "pending",
        "attempt": 0,
        "execution_id": None,
        "execution_history": [],
        "asset_ids": [],
        "annotation_ids": [],
        "started_at": None,
        "completed_at": None,
        "review": {"state": "pending", "reason": ""},
        "review_feedback": "",
        "error": None,
        "updated_at": utc_now(),
    }


def initial_tasks() -> Dict[str, Dict[str, Any]]:
    tasks: Dict[str, Dict[str, Any]] = {}
    for definition in STAGE_BY_ID.values():
        if definition.execution_mode != "main_agent":
            continue
        key = task_key(definition.id, definition.id)
        tasks[key] = new_task(definition.id, definition.id)
    return tasks


def ensure_task(
    status: Dict[str, Any],
    stage_id: str,
    task_id: str,
    *,
    unit: Optional[str] = None,
    executor: Optional[str] = None,
) -> Dict[str, Any]:
    tasks = status.setdefault("tasks", {})
    key = task_key(stage_id, task_id)
    task = tasks.get(key)
    if task is None:
        task = new_task(stage_id, task_id, unit=unit, executor=executor)
        tasks[key] = task
    return task


def set_task_state(
    status: Dict[str, Any],
    stage_id: str,
    task_id: str,
    state: str,
    *,
    error: Optional[str] = None,
    review_reason: Optional[str] = None,
) -> Dict[str, Any]:
    if state not in TASK_STATES:
        raise ValueError("invalid task state: %s" % state)
    task = ensure_task(status, stage_id, task_id)
    previous = task["state"]
    if state == "running" and previous != "running":
        task["attempt"] += 1
        task["started_at"] = utc_now()
        task["completed_at"] = None
        execution_id = "%s.e%04d" % (task["task_id"], task["attempt"])
        task["execution_id"] = execution_id
        task["execution_history"].append(execution_id)
    if state in {"passed", "failed"}:
        task["completed_at"] = utc_now()
    elif state in {"pending", "queued", "redo_requested"}:
        task["completed_at"] = None
    task["state"] = state
    task["error"] = error if state == "failed" else None
    if review_reason is not None:
        task["review"] = {
            "state": "passed" if state == "passed" else "failed",
            "reason": review_reason,
        }
    elif state in {"pending", "queued", "running", "redo_requested"}:
        task["review"] = {"state": "pending", "reason": ""}
        if state != "queued":
            task["review_feedback"] = ""
    task["updated_at"] = utc_now()
    return task


def link_task_asset(
    status: Dict[str, Any], stage_id: str, task_id: str, asset_id: str
) -> None:
    task = ensure_task(status, stage_id, task_id)
    if asset_id not in task["asset_ids"]:
        task["asset_ids"].append(asset_id)
        task["updated_at"] = utc_now()


def link_task_annotation(
    status: Dict[str, Any], stage_id: str, task_id: str, annotation_id: str
) -> None:
    task = ensure_task(status, stage_id, task_id)
    if annotation_id not in task["annotation_ids"]:
        task["annotation_ids"].append(annotation_id)
        task["updated_at"] = utc_now()


def reset_stage_tasks(status: Dict[str, Any], stage_ids: Iterable[str]) -> None:
    selected = set(stage_ids)
    for task in status.setdefault("tasks", {}).values():
        if task.get("stage") not in selected:
            continue
        task.update(
            {
                "state": "pending",
                "asset_ids": [],
                "started_at": None,
                "completed_at": None,
                "execution_id": None,
                "review": {"state": "pending", "reason": ""},
                "review_feedback": "",
                "error": None,
                "updated_at": utc_now(),
            }
        )


def validate_tasks(tasks: Mapping[str, Any]) -> None:
    if not isinstance(tasks, Mapping):
        raise ValueError("tasks must be an object")
    for key, task in tasks.items():
        if not isinstance(task, Mapping):
            raise ValueError("task %s must be an object" % key)
        stage_id = task.get("stage")
        task_id = task.get("id")
        if key != task_key(stage_id, task_id):
            raise ValueError("task key does not match stage and id: %s" % key)
        if task.get("task_id") != key:
            raise ValueError("task_id does not match task key: %s" % key)
        if task.get("state") not in TASK_STATES:
            raise ValueError("invalid task state: %s" % key)
        if task.get("executor") not in {"main_agent", "parallel_tasks"}:
            raise ValueError("invalid task executor: %s" % key)
        if not isinstance(task.get("asset_ids"), list) or not isinstance(
            task.get("annotation_ids"), list
        ):
            raise ValueError("task links must be lists: %s" % key)
        if not isinstance(task.get("review_feedback", ""), str):
            raise ValueError("task review_feedback must be a string: %s" % key)
        execution_id = task.get("execution_id")
        history = task.get("execution_history")
        if not isinstance(history, list) or any(
            not isinstance(item, str) for item in history
        ) or len(history) != len(set(history)):
            raise ValueError("task execution_history is invalid: %s" % key)
        if execution_id is not None and execution_id not in history:
            raise ValueError("task execution_id is not in history: %s" % key)
