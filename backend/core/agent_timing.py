"""Read-only aggregation for Codex SubagentStart/SubagentStop telemetry.

The lifecycle records are written by the global Codex hook with a project
allowlist.  This module
joins those records with the existing usage ledger so the workbench can show
agent runtime alongside estimated/actual token accounting without making the
workflow runner responsible for timing instrumentation.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


VIDEO_STAGES = {
    "story_design",
    "asset_planning",
    "character_design",
    "visual_design",
    "storyboard_binding",
    "storyboard_generation",
    "video_binding",
    "video_generation",
    "edit_post",
}


def _empty_metrics() -> Dict[str, Any]:
    return {
        "nodes": 0,
        "completed_nodes": 0,
        "running_nodes": 0,
        "failed_nodes": 0,
        "stopped_nodes": 0,
        "total_runtime_ms": 0,
        "average_runtime_ms": None,
        "p50_runtime_ms": None,
        "p95_runtime_ms": None,
        "total_queue_wait_ms": 0,
        "average_queue_wait_ms": None,
        "total_node_ms": 0,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_total_tokens": 0,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "actual_total_tokens": None,
        "tokens_per_runtime_minute": None,
    }


def _read_records(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def timing_records(workspace_root: Path, project_id: str, episode_id: str) -> List[Dict[str, Any]]:
    path = Path(workspace_root) / "agent_timing" / project_id / (episode_id + ".jsonl")
    return _read_records(path)


def _epoch_ms(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _percentile(values: List[int], fraction: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return int(ordered[index])


def _actual_total(metrics: Mapping[str, Any]) -> Optional[int]:
    input_tokens = metrics.get("actual_input_tokens")
    output_tokens = metrics.get("actual_output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return int(input_tokens or 0) + int(output_tokens or 0)


def _finalize_metrics(metrics: Dict[str, Any], runtime_values: List[int], queue_values: List[int]) -> Dict[str, Any]:
    metrics["average_runtime_ms"] = (
        int(sum(runtime_values) / len(runtime_values)) if runtime_values else None
    )
    metrics["p50_runtime_ms"] = _percentile(runtime_values, 0.50)
    metrics["p95_runtime_ms"] = _percentile(runtime_values, 0.95)
    metrics["average_queue_wait_ms"] = (
        int(sum(queue_values) / len(queue_values)) if queue_values else None
    )
    metrics["estimated_total_tokens"] = (
        metrics["estimated_input_tokens"] + metrics["estimated_output_tokens"]
    )
    metrics["actual_total_tokens"] = _actual_total(metrics)
    denominator = metrics["total_runtime_ms"] / 60000
    if denominator > 0 and metrics["estimated_total_tokens"]:
        metrics["tokens_per_runtime_minute"] = int(metrics["estimated_total_tokens"] / denominator)
    return metrics


def _add_tokens(metrics: Dict[str, Any], record: Mapping[str, Any]) -> None:
    metrics["estimated_input_tokens"] += int(record.get("estimated_input_tokens", 0) or 0)
    metrics["estimated_output_tokens"] += int(record.get("estimated_output_tokens", 0) or 0)
    for field in ("actual_input_tokens", "actual_output_tokens"):
        value = record.get(field)
        if value is not None:
            metrics[field] = int(metrics[field] or 0) + int(value)


def _usage_by_execution(records: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        execution_id = record.get("execution_id")
        if execution_id:
            grouped[str(execution_id)].append(record)
    return grouped


def _status_for(start: Mapping[str, Any], stop: Optional[Mapping[str, Any]], usage: List[Mapping[str, Any]]) -> str:
    result = next(
        (item for item in reversed(usage) if item.get("kind") in {"agent_result", "review_round"}),
        None,
    )
    if result and result.get("status") == "failed":
        return "failed"
    if stop is None:
        return "running"
    if result and result.get("status") == "success":
        return "success"
    return "stopped"


def _join_runs(
    status: Mapping[str, Any],
    hook_records: Iterable[Mapping[str, Any]],
    usage_records: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    starts = [record for record in hook_records if record.get("event") == "subagent_start"]
    stops = [record for record in hook_records if record.get("event") == "subagent_stop"]
    stop_by_node = {record.get("node_id"): record for record in stops if record.get("node_id")}
    stop_by_agent = {
        (record.get("session_id"), record.get("agent_id")): record
        for record in stops
        if record.get("session_id") and record.get("agent_id")
    }
    usage_by_execution = _usage_by_execution(usage_records)
    runs: List[Dict[str, Any]] = []
    for start in starts:
        stage_id = start.get("stage_id")
        if stage_id not in VIDEO_STAGES or start.get("project_id") != status.get("project_id"):
            continue
        if start.get("episode_id") != status.get("episode_id"):
            continue
        stop = stop_by_node.get(start.get("node_id")) or stop_by_agent.get(
            (start.get("session_id"), start.get("agent_id"))
        )
        execution_id = start.get("execution_id")
        usage = usage_by_execution.get(str(execution_id), []) if execution_id else []
        request_record = next((item for item in usage if item.get("kind") == "agent_request"), None)
        result_record = next(
            (item for item in reversed(usage) if item.get("kind") in {"agent_result", "review_round"}),
            None,
        )
        started_ms = _epoch_ms(start.get("started_epoch_ms") or start.get("started_at"))
        stopped_ms = _epoch_ms(stop.get("stopped_epoch_ms") or stop.get("stopped_at")) if stop else None
        requested_ms = _epoch_ms(
            start.get("request_created_at")
            or (request_record or {}).get("ts")
        )
        result_ms = _epoch_ms((result_record or {}).get("ts"))
        runtime_ms = max(0, stopped_ms - started_ms) if started_ms is not None and stopped_ms is not None else None
        queue_wait_ms = max(0, started_ms - requested_ms) if started_ms is not None and requested_ms is not None else None
        commit_ms = max(0, result_ms - stopped_ms) if result_ms is not None and stopped_ms is not None else None
        total_node_ms = (
            max(0, (result_ms or stopped_ms) - requested_ms)
            if requested_ms is not None and (result_ms is not None or stopped_ms is not None)
            else None
        )
        estimated_input = sum(int(item.get("estimated_input_tokens", 0) or 0) for item in usage if item.get("kind") == "agent_request")
        estimated_output = sum(int(item.get("estimated_output_tokens", 0) or 0) for item in usage if item.get("kind") in {"agent_result", "review_round"})
        actual_input_values = [item.get("actual_input_tokens") for item in usage if item.get("actual_input_tokens") is not None]
        actual_output_values = [item.get("actual_output_tokens") for item in usage if item.get("actual_output_tokens") is not None]
        error = next((item.get("error") for item in reversed(usage) if item.get("error")), None)
        run = {
            "node_id": start.get("node_id"),
            "project_id": start.get("project_id"),
            "episode_id": start.get("episode_id"),
            "request_id": start.get("request_id"),
            "request_kind": start.get("request_kind"),
            "stage_id": stage_id,
            "task_id": start.get("task_id"),
            "execution_id": execution_id,
            "stage_revision": start.get("stage_revision"),
            "task_attempt": start.get("task_attempt"),
            "session_id": start.get("session_id"),
            "agent_id": start.get("agent_id"),
            "agent_type": start.get("agent_type"),
            "model": start.get("model"),
            "status": _status_for(start, stop, usage),
            "started_at": start.get("started_at"),
            "ended_at": stop.get("stopped_at") if stop else None,
            "runtime_ms": runtime_ms,
            "queue_wait_ms": queue_wait_ms,
            "commit_ms": commit_ms,
            "total_node_ms": total_node_ms,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "estimated_total_tokens": estimated_input + estimated_output,
            "actual_input_tokens": sum(actual_input_values) if actual_input_values else None,
            "actual_output_tokens": sum(actual_output_values) if actual_output_values else None,
            "error": error,
            "last_assistant_message": stop.get("last_assistant_message") if stop else None,
        }
        runs.append(run)
    return sorted(runs, key=lambda item: item.get("started_at") or "", reverse=True)


def _summarize_group(runs: List[Mapping[str, Any]]) -> Dict[str, Any]:
    metrics = _empty_metrics()
    runtime_values: List[int] = []
    queue_values: List[int] = []
    for run in runs:
        metrics["nodes"] += 1
        status = run.get("status")
        if status == "success":
            metrics["completed_nodes"] += 1
        elif status == "running":
            metrics["running_nodes"] += 1
        elif status == "failed":
            metrics["failed_nodes"] += 1
        elif status == "stopped":
            metrics["stopped_nodes"] += 1
        runtime_ms = run.get("runtime_ms")
        if runtime_ms is not None:
            runtime_values.append(int(runtime_ms))
            metrics["total_runtime_ms"] += int(runtime_ms)
        queue_ms = run.get("queue_wait_ms")
        if queue_ms is not None:
            queue_values.append(int(queue_ms))
            metrics["total_queue_wait_ms"] += int(queue_ms)
        if run.get("total_node_ms") is not None:
            metrics["total_node_ms"] += int(run["total_node_ms"])
        _add_tokens(metrics, run)
    return _finalize_metrics(metrics, runtime_values, queue_values)


def summarize_agent_timing(
    status: Mapping[str, Any],
    workspace_root: Path,
    usage_records: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    project_id = str(status["project_id"])
    episode_id = str(status["episode_id"])
    hook_records = timing_records(workspace_root, project_id, episode_id)
    runs = _join_runs(status, hook_records, usage_records)
    stages: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    tasks: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        stages[str(run["stage_id"])].append(run)
        tasks[(str(run["stage_id"]), str(run.get("task_id") or "—"))].append(run)
    stage_summary = {
        stage_id: _summarize_group(group)
        for stage_id, group in stages.items()
    }
    task_summary = []
    for (stage_id, task_id), group in tasks.items():
        item = _summarize_group(group)
        item.update({"stage_id": stage_id, "task_id": task_id})
        task_summary.append(item)
    task_summary.sort(key=lambda item: (-(item.get("total_runtime_ms") or 0), item["stage_id"], item["task_id"]))
    totals = _summarize_group(runs)
    return {
        "source": "hook" if hook_records else "not_started",
        "schema_version": "agent_timing.v1",
        "log_path": f"workspace/agent_timing/{project_id}/{episode_id}.jsonl",
        "generated_at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "totals": totals,
        "stages": stage_summary,
        "tasks": task_summary,
        "recent": runs[:50],
    }
