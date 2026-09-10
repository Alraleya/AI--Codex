"""Append-only execution accounting for the workbench.

The provider adapters do not expose a common token-usage contract yet.  This
module therefore keeps exact call counts separate from optional token fields:
token values are only populated when a provider reports them, while prompt
length estimates are explicitly labelled as estimates in the API.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .status import utc_now, validate_id


def estimate_tokens(value: Any) -> int:
    """Return a deliberately conservative text-token estimate.

    This is not presented as provider billing.  It is only useful for spotting
    unusually large handoffs and repeated prompts before provider telemetry is
    available.
    """

    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return int(math.ceil(len(value) / 3.2))


class UsageLedger:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)

    def path_for(self, project_id: str, episode_id: str) -> Path:
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        return self.workspace_root / "usage" / project_id / (episode_id + ".jsonl")

    def record(
        self,
        project_id: str,
        episode_id: str,
        *,
        kind: str,
        status: str,
        stage_id: Optional[str] = None,
        task_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        attempt: Optional[int] = None,
        provider: Optional[str] = None,
        actual_input_tokens: Optional[int] = None,
        actual_output_tokens: Optional[int] = None,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        image_count: int = 0,
        video_seconds: float = 0,
        duration_ms: Optional[int] = None,
        repair_plan_id: Optional[str] = None,
        affected_tasks: Iterable[str] = (),
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = {
            "ts": utc_now(),
            "project_id": project_id,
            "episode_id": episode_id,
            "stage_id": stage_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "kind": kind,
            "status": status,
            "attempt": attempt,
            "provider": provider,
            "actual_input_tokens": actual_input_tokens,
            "actual_output_tokens": actual_output_tokens,
            "estimated_input_tokens": int(estimated_input_tokens or 0),
            "estimated_output_tokens": int(estimated_output_tokens or 0),
            "image_count": int(image_count or 0),
            "video_seconds": float(video_seconds or 0),
            "duration_ms": duration_ms,
            "repair_plan_id": repair_plan_id,
            "affected_tasks": list(affected_tasks),
            "error": error,
        }
        path = self.path_for(project_id, episode_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return entry

    def records(self, project_id: str, episode_id: str) -> List[Dict[str, Any]]:
        path = self.path_for(project_id, episode_id)
        if not path.is_file():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result


def _empty_counter() -> Dict[str, Any]:
    return {
        "task_attempts": 0,
        "provider_calls": 0,
        "successful_provider_calls": 0,
        "reuse_count": 0,
        "failure_count": 0,
        "review_rounds": 0,
        "repair_plans": 0,
        "image_generations": 0,
        "video_generations": 0,
        "video_seconds": 0,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
    }


def _add_counter(target: Dict[str, Any], entry: Mapping[str, Any]) -> None:
    for field in (
        "task_attempts", "provider_calls", "successful_provider_calls", "reuse_count",
        "failure_count", "review_rounds", "repair_plans", "image_generations",
        "video_generations", "video_seconds", "estimated_input_tokens", "estimated_output_tokens",
    ):
        target[field] += entry.get(field, 0) or 0
    for field in ("actual_input_tokens", "actual_output_tokens"):
        value = entry.get(field)
        if value is not None:
            target[field] = (target[field] or 0) + value


def _counter_for_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate the compact ledger event into dashboard counters."""
    counter = dict(record)
    kind = record.get("kind")
    state = record.get("status")
    if kind == "provider_call":
        counter["provider_calls"] = 1
        counter["successful_provider_calls"] = 1 if state == "success" else 0
        counter["image_generations"] = int(record.get("image_count", 0) or 0) if state == "success" else 0
        counter["video_generations"] = 1 if state == "success" and record.get("video_seconds", 0) else 0
    elif kind == "reuse":
        counter["reuse_count"] = 1
    elif kind == "review_round":
        counter["review_rounds"] = 1
    elif kind == "repair_plan":
        counter["repair_plans"] = 1
    if state == "failed" or kind == "failure":
        counter["failure_count"] = 1
    return counter


def summarize_episode(status: Mapping[str, Any], workspace_root: Path, events: Iterable[Mapping[str, Any]], ledger: UsageLedger) -> Dict[str, Any]:
    project_id = status["project_id"]
    episode_id = status["episode_id"]
    records = ledger.records(project_id, episode_id)
    source = "ledger" if records else "not_started"

    totals = _empty_counter()
    stages: Dict[str, Dict[str, Any]] = defaultdict(_empty_counter)
    tasks: Dict[str, Dict[str, Any]] = {}
    normalized_recent = []
    for record in records:
        stage_id = record.get("stage_id")
        counter = _counter_for_record(record)
        _add_counter(totals, counter)
        if stage_id:
            _add_counter(stages[stage_id], counter)
        task_id = record.get("task_id")
        if stage_id and task_id:
            item = tasks.setdefault(f"{stage_id}:{task_id}", {"stage_id": stage_id, "task_id": task_id, **_empty_counter(), "last_error": None})
            _add_counter(item, counter)
            item["task_attempts"] = max(item["task_attempts"], int(record.get("attempt", 0) or 0))
            if record.get("error"):
                item["last_error"] = record["error"]
        normalized_recent.append({key: record.get(key) for key in ("ts", "kind", "status", "stage_id", "task_id", "provider", "error", "repair_plan_id") if record.get(key) is not None})

    # Task attempts are owned by status.json. Provider records must never
    # inflate this number when a task has several external calls.
    totals["task_attempts"] = sum(item["task_attempts"] for item in tasks.values())
    for stage_id in stages:
        stages[stage_id]["task_attempts"] = sum(
            item["task_attempts"]
            for item in tasks.values()
            if item.get("stage_id") == stage_id
        )
    return {
        "source": source,
        "generated_at": utc_now(),
        "totals": totals,
        "stages": dict(stages),
        "tasks": sorted(tasks.values(), key=lambda item: (-item["task_attempts"], item["stage_id"], item["task_id"])),
        "recent": sorted(normalized_recent, key=lambda item: item.get("ts", ""), reverse=True)[:60],
    }
