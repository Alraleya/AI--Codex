#!/usr/bin/env python3
"""Codex-facing entry point for the deterministic episode workflow."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.annotations import AnnotationStore
from backend.core.assets import archive_asset, register_asset, resolve_asset_path
from backend.core.events import EventLog
from backend.core.lock import EpisodeLock
from backend.core.models import validate_model
from backend.core.projects import Workspace
from backend.core.repairs import RepairPlanStore
from backend.core.flow import STAGE_IDS
from backend.core.status import (
    IMAGE_PROVIDERS,
    MIN_VIDEO_SHOT_DURATION_SEC,
    PRODUCTION_MODES,
    STORYBOARD_DECISIONS,
    VIDEO_MODEL_OPTIONS,
    expand_style,
    make_creative_brief,
    normalize_aspect_ratio,
    resolved_shot_count,
    resolved_storyboard_shots,
    utc_now,
)
from backend.core.tasks import ensure_task, set_task_state
from backend.workflow.chain import (
    complete_stage,
    consume_video_shot_approval,
    invalidate_from,
    pass_review,
    rollback_with_preserved_stages,
    restart_for_global_style,
)
from backend.workflow.runner import ChainRunner
from backend.workflow.asset_reuse import confirm_asset_reuse, sha256_file


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _slim_status(status: Dict[str, Any]) -> Dict[str, Any]:
    asset_counts: Dict[str, int] = {}
    task_counts: Dict[str, Dict[str, int]] = {}
    for asset in status["assets"].values():
        asset_counts[asset["stage"]] = asset_counts.get(asset["stage"], 0) + 1
    for task in status.get("tasks", {}).values():
        counts = task_counts.setdefault(task["stage"], {})
        counts[task["state"]] = counts.get(task["state"], 0) + 1
    brief = status["creative_brief"]
    last_completed = None
    for stage_id in reversed(STAGE_IDS):
        stage = status["stages"][stage_id]
        if stage["state"] in {"done", "skipped"}:
            handoff = stage.get("handoff")
            last_completed = {
                "stage_id": stage_id,
                "revision": stage["revision"],
                "handoff": handoff,
            }
            break
    return {
        "project_id": status["project_id"],
        "episode_id": status["episode_id"],
        "creative_brief": {
            "topic": brief["topic"],
            "style": brief["style"],
            "target_duration_sec": brief["target_duration_sec"],
            "aspect_ratio": brief["aspect_ratio"],
            "production_mode": brief.get("production_mode", "pending"),
            "storyboard_decision": brief.get("storyboard_decision", "pending"),
            "video_model": brief.get("video_model", "pending"),
            "video_session_id": brief.get("video_session_id"),
            "provided_script_locked": "provided_script" in brief,
            "shot_duration_exception": brief.get("shot_duration_exception"),
            "shot_plan": brief["shot_plan"],
            "storyboard_plan_state": brief["storyboard_plan"]["state"],
        },
        "run": status["run"],
        "context": {
            "mode": "fresh_child_per_node",
            "last_completed": last_completed,
        },
        "annotations": status["annotations"],
        "repairs": status["repairs"],
        "incremental_request": status.get(
            "incremental_request", {"state": "clear", "request": None}
        ),
        "agent_request": {
            "state": status["agent_request"]["state"],
            "id": (
                status["agent_request"]["request"]["id"]
                if status["agent_request"]["request"]
                else None
            ),
            "kind": (
                status["agent_request"]["request"]["kind"]
                if status["agent_request"]["request"]
                else None
            ),
            "stage_id": (
                status["agent_request"]["request"]["stage_id"]
                if status["agent_request"]["request"]
                else None
            ),
            "task_id": (
                status["agent_request"]["request"].get("task_id")
                if status["agent_request"]["request"]
                else None
            ),
            "execution_id": (
                status["agent_request"]["request"].get("execution_id")
                if status["agent_request"]["request"]
                else None
            ),
        },
        "asset_reuse": status.get("asset_reuse"),
        "stages": {
            stage_id: {
                "label": stage["label"],
                "state": stage["state"],
                "revision": stage["revision"],
                "outputs": stage["outputs"],
                "error": stage["error"],
            }
            for stage_id, stage in status["stages"].items()
        },
        "asset_counts": asset_counts,
        "task_counts": task_counts,
        "updated_at": status["updated_at"],
    }


def _set_shot_storyboard_decision(
    workspace: Workspace,
    project_id: str,
    episode_id: str,
    shot_number: int,
    decision: str,
) -> Dict[str, Any]:
    """Switch one production shot between storyboard and direct-video routing."""
    if decision not in {"yes", "no"}:
        raise ValueError("shot storyboard decision must be yes or no")
    with EpisodeLock(workspace.root, project_id, episode_id):
        status = workspace.statuses.load(project_id, episode_id)
        agent_request = status.get("agent_request", {})
        if agent_request.get("state") != "clear":
            request = agent_request.get("request") or {}
            if request.get("kind") != "storyboard_sequence_review":
                raise RuntimeError("cannot change shot routing while an Agent handoff is waiting")
            # The user explicitly replaced this review with a direct-video
            # decision for the selected shot; the already-closed reviewer is
            # no longer a live handoff.
            status["agent_request"] = {"state": "clear", "request": None}
        brief = status["creative_brief"]
        shot_count = resolved_shot_count(brief)
        if not 1 <= shot_number <= shot_count:
            raise ValueError("shot number must be between 1 and %d" % shot_count)
        if status["stages"]["video_generation"]["state"] not in {"pending", "skipped"}:
            raise RuntimeError("shot storyboard routing can only change before video generation")
        storyboard_plan = brief["storyboard_plan"]
        shot_plan = next(
            item for item in storyboard_plan["shots"]
            if item["shot_number"] == shot_number
        )
        requested = decision == "yes"
        if shot_plan.get("storyboard_required", True) == requested:
            if not requested and status["stages"]["storyboard_generation"]["state"] == "pending":
                set_task_state(
                    status,
                    "storyboard_generation",
                    "sequence_review",
                    "pending",
                )
                status["agent_request"] = {"state": "clear", "request": None}
                status["run"].update(
                    {
                        "state": "idle",
                        "current_stage": "storyboard_generation",
                        "last_error": None,
                        "waiting_for": None,
                    }
                )
                workspace.statuses.save(status)
            workspace.statuses.save(status)
            return status

        shot_plan["storyboard_required"] = requested
        episode_dir = workspace.episode_dir(project_id, episode_id)
        asset_plan_path = episode_dir / "asset_plan.json"
        asset_plan = json.loads(asset_plan_path.read_text(encoding="utf-8"))
        asset_plan_shot = next(
            item for item in asset_plan["shots"]
            if item["shot_number"] == shot_number
        )
        asset_plan_shot["storyboard_required"] = requested
        asset_plan_path.write_text(
            json.dumps(asset_plan, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        asset_plan_hash = hashlib.sha256(asset_plan_path.read_bytes()).hexdigest()
        for asset in status["assets"].values():
            if asset.get("path") == "asset_plan.json":
                asset["sha256"] = asset_plan_hash
                asset["size"] = asset_plan_path.stat().st_size

        if not requested:
            stale_names = {
                "shot%02d_storyboard.png" % shot_number,
                "shot%02d_storyboard_references.json" % shot_number,
            }
            for asset_id, asset in list(status["assets"].items()):
                if asset.get("path") not in stale_names:
                    continue
                source = resolve_asset_path(episode_dir, asset["path"])
                if source.is_file():
                    archive_asset(status, episode_dir, asset_id)
                    source.unlink()
                status["assets"].pop(asset_id, None)
            for stage_id in ("storyboard_binding", "storyboard_generation"):
                stage = status["stages"][stage_id]
                stage["outputs"] = [
                    name for name in stage.get("outputs", [])
                    if name not in stale_names
                ]
            storyboard_stage = status["stages"]["storyboard_generation"]
            storyboard_stage.update(
                {
                    "state": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "handoff": None,
                    "review": {"state": "pending", "reason": ""},
                    "error": None,
                }
            )
            set_task_state(
                status,
                "storyboard_generation",
                "sequence_review",
                "pending",
            )

        status["agent_request"] = {"state": "clear", "request": None}
        status["run"].update(
            {
                "state": "idle",
                "current_stage": "storyboard_generation" if not requested else "storyboard_binding",
                "last_error": None,
                "waiting_for": None,
            }
        )
        status.setdefault("confirmations", {})["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
        workspace.statuses.save(status)
        EventLog(workspace.root).append(
            project_id,
            episode_id,
            "decision",
            "镜头 %02d 已切换为%s故事板路由；保留剧本与视频 Prompt，按镜头增量更新下游产物"
            % (shot_number, "故事板" if requested else "直通视频"),
            stage="storyboard_generation",
            runner="codex",
            meta={"shot_number": shot_number, "storyboard_required": requested},
        )
        return status


def _accept_current_storyboards(
    workspace: Workspace,
    project_id: str,
    episode_id: str,
) -> Dict[str, Any]:
    """Accept existing storyboard files and continue without another review round."""
    with EpisodeLock(workspace.root, project_id, episode_id):
        status = workspace.statuses.load(project_id, episode_id)
        if status.get("agent_request", {}).get("state") != "clear":
            raise RuntimeError("cannot accept storyboards while an Agent handoff is waiting")
        stage_id = "storyboard_generation"
        stage = status["stages"][stage_id]
        episode_dir = workspace.episode_dir(project_id, episode_id)
        required_shots = [
            shot["shot_number"]
            for shot in resolved_storyboard_shots(status["creative_brief"])
            if shot.get("storyboard_required", True)
        ]
        outputs = []
        for shot_number in required_shots:
            name = "shot%02d_storyboard.png" % shot_number
            path = episode_dir / name
            if not path.is_file():
                raise FileNotFoundError("existing storyboard is missing: %s" % name)
            if any(asset.get("path") == name for asset in status["assets"].values()):
                set_task_state(
                    status,
                    stage_id,
                    "shot%02d" % shot_number,
                    "passed",
                    review_reason="用户确认保留当前故事板；细节差异降级为 warning",
                )
                outputs.append(name)
                continue
            history = next(
                (
                    item for item in status.get("asset_history", {}).values()
                    if item.get("stage") == stage_id
                    and item.get("original_path") == name
                ),
                None,
            )
            if history is None:
                raise ValueError("no archived storyboard record found for %s" % name)
            asset_id = history.get("logical_asset_id")
            metadata = dict(history.get("metadata") or {})
            metadata["accepted_without_rerender"] = True
            register_asset(
                status,
                episode_dir,
                asset_id,
                stage_id,
                "image",
                "storyboard_sheet",
                history.get("label") or Path(name).stem,
                name,
                metadata=metadata,
                task_id="shot%02d" % shot_number,
                asset_revision=history.get("asset_revision", 1),
            )
            set_task_state(
                status,
                stage_id,
                "shot%02d" % shot_number,
                "passed",
                review_reason="用户确认保留当前故事板；细节差异降级为警告",
            )
            outputs.append(name)

        active_plan_id = status.get("repairs", {}).get("active_plan_id")
        if active_plan_id:
            plan_store = RepairPlanStore(workspace.root)
            plan = plan_store.load(project_id, episode_id, active_plan_id)
            plan_store.supersede_locked(
                status,
                plan,
                "用户确认保留当前故事板，终止未完成的镜头级重做",
            )
        set_task_state(
            status,
            stage_id,
            "sequence_review",
            "passed",
            review_reason="用户确认保留当前故事板；外观细节不足降级为 warning",
        )
        stage.update(
            {
                "state": "done",
                "outputs": sorted(outputs),
                "handoff": {
                    "summary": "用户确认保留当前故事板；角色定妆细节不足仅记录为 warning，不阻断后续视频绑定。",
                    "outputs": sorted(outputs),
                },
                "review": {
                    "state": "passed",
                    "reason": "用户确认保留当前故事板；审查降级策略生效。",
                },
                "error": None,
                "completed_at": utc_now(),
            }
        )
        status["agent_request"] = {"state": "clear", "request": None}
        status["run"].update(
            {
                "state": "idle",
                "current_stage": "video_binding",
                "last_error": None,
                "waiting_for": None,
            }
        )
        status.setdefault("confirmations", {})["video_generation"] = {
            "state": "required",
            "revision_fingerprint": None,
            "approved_at": None,
            "shots": {},
        }
        workspace.statuses.save(status)
        EventLog(workspace.root).append(
            project_id,
            episode_id,
            "decision",
            "用户确认保留当前故事板，审查降级为警告并进入视频参考绑定",
            stage=stage_id,
            runner="codex",
            meta={"accepted_shots": required_shots, "review_policy": "warn_on_design_detail"},
        )
        return status


def _slim_episode(episode: Dict[str, Any]) -> Dict[str, Any]:
    brief = episode["creative_brief"]
    return {
        "project_id": episode["project_id"],
        "episode_id": episode["episode_id"],
        "name": episode["name"],
        "topic": brief["topic"],
        "style": brief["style"],
        "target_duration_sec": brief["target_duration_sec"],
        "video_session_id": brief.get("video_session_id"),
        "run": episode["run"],
        "annotations": episode["annotations"],
        "progress": episode["progress"],
        "updated_at": episode["updated_at"],
    }


def _load_development_brief(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("development brief must be a JSON object")
    return payload


def _load_script(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("provided script must be UTF-8 text") from error


def _parse_shot_numbers(value: str) -> list:
    try:
        numbers = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise ValueError("--shots must be a comma-separated list of integers") from error
    if not numbers or any(number <= 0 for number in numbers):
        raise ValueError("--shots must contain positive shot numbers")
    return sorted(set(numbers))


def _parse_ids(value: Optional[str]) -> list:
    if not value:
        return []
    ids = [item.strip() for item in value.split(",") if item.strip()]
    if not ids:
        raise ValueError("identifier list cannot be empty")
    return list(dict.fromkeys(ids))


def _load_repair_notes(path: Optional[Path]) -> Dict[int, str]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("repair notes must be a JSON object keyed by shot number")
    notes: Dict[int, str] = {}
    for key, value in payload.items():
        try:
            number = int(key)
        except (TypeError, ValueError) as error:
            raise ValueError("repair note keys must be shot numbers") from error
        if not isinstance(value, str):
            raise ValueError("repair notes must be strings")
        notes[number] = value
    return notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Codex-controlled AI manga production workflow"
    )
    parser.add_argument("--workspace", type=Path, default=PROJECT_ROOT / "workspace")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List projects and episodes")

    status = subparsers.add_parser("status", help="Read an episode status snapshot")
    status.add_argument("--project", required=True)
    status.add_argument("--episode", required=True)
    status.add_argument("--full", action="store_true")

    next_action = subparsers.add_parser(
        "next-action",
        help="Read the one native Codex Agent handoff waiting in status.json",
    )
    next_action.add_argument("--project", required=True)
    next_action.add_argument("--episode", required=True)

    submit_agent = subparsers.add_parser(
        "submit-agent-result",
        help="Validate a visible native Agent JSON result and advance only the state machine",
    )
    submit_agent.add_argument("--project", required=True)
    submit_agent.add_argument("--episode", required=True)
    submit_agent.add_argument("--request", required=True)
    submit_agent.add_argument("--result-file", type=Path, required=True)

    annotations = subparsers.add_parser(
        "annotations", help="Read only unhandled annotation contents"
    )
    annotations.add_argument("--project", required=True)
    annotations.add_argument("--episode", required=True)

    repairs = subparsers.add_parser("repairs", help="Read persisted repair plans")
    repairs.add_argument("--project", required=True)
    repairs.add_argument("--episode", required=True)

    supersede_repair = subparsers.add_parser(
        "supersede-repair",
        help="Retire a stale active repair plan at a blocked, stable boundary",
    )
    supersede_repair.add_argument("--project", required=True)
    supersede_repair.add_argument("--episode", required=True)

    plan_repair = subparsers.add_parser(
        "plan-repair", help="Validate and persist a repair plan without provider calls"
    )
    plan_repair.add_argument("--project", required=True)
    plan_repair.add_argument("--episode", required=True)
    plan_repair.add_argument("--stage", required=True)
    plan_repair.add_argument("--shots", required=True)
    plan_repair.add_argument("--annotations")
    plan_repair.add_argument("--repair-notes", type=Path)

    handled = subparsers.add_parser(
        "handle-annotations",
        help="Advance the annotation cursor after the requested work is committed",
    )
    handled.add_argument("--project", required=True)
    handled.add_argument("--episode", required=True)
    handled.add_argument("--through", type=int)

    create_project = subparsers.add_parser("create-project")
    create_project.add_argument("--project", required=True)
    create_project.add_argument("--name", required=True)

    create_episode = subparsers.add_parser("create-episode")
    create_episode.add_argument("--project", required=True)
    create_episode.add_argument("--episode", required=True)
    create_episode.add_argument("--name")
    create_episode.add_argument("--topic", required=True)
    create_episode.add_argument("--hook", default="")
    create_episode.add_argument(
        "--style",
        default=None,
        help="视觉风格；未指定且剧本未写明时，创建后等待用户选择，不使用默认风格",
    )
    create_episode.add_argument(
        "--duration",
        type=float,
        default=None,
        help="本集目标时长（至少4秒，无单集上限）；可从剧本总时长读取，否则必须显式提供",
    )
    create_episode.add_argument(
        "--aspect-ratio",
        default=None,
        help="画幅比例；未指定且剧本未写明时，创建后等待用户选择，不使用默认画幅",
    )
    create_episode.add_argument("--development-brief", type=Path)
    create_episode.add_argument(
        "--production-mode",
        choices=sorted(PRODUCTION_MODES),
        default=None,
        help="显式指定模式；未指定时默认 dialogue_direct（不需要故事板）",
    )
    create_episode.add_argument(
        "--storyboard-decision",
        choices=sorted(STORYBOARD_DECISIONS),
        default=None,
        help="故事板决定：pending、yes 或 no；默认 no",
    )
    create_episode.add_argument(
        "--video-model",
        choices=["pending", "fast", "mini"],
        default="pending",
        help="视频模型；默认在剧集开始前询问",
    )
    create_episode.add_argument(
        "--video-session-id",
        default=None,
        help="可选；不提供时在创建剧集时自动分配本集唯一数字 sessionId",
    )

    delete_episode = subparsers.add_parser(
        "delete-episode",
        help="删除指定剧集及其全部本地产物（需要显式确认）",
    )
    delete_episode.add_argument("--project", required=True)
    delete_episode.add_argument("--episode", required=True)
    delete_episode.add_argument("--confirm-delete", action="store_true")
    delete_episode.add_argument(
        "--purge-assets",
        action="store_true",
        help="允许删除已登记资产；与 --confirm-delete 一起使用",
    )

    storyboard_decision = subparsers.add_parser(
        "set-storyboard-decision",
        help="在开始制作前记录是否需要故事板",
    )
    storyboard_decision.add_argument("--project", required=True)
    storyboard_decision.add_argument("--episode", required=True)
    storyboard_decision.add_argument("--decision", choices=["yes", "no"], required=True)

    asset_reuse = subparsers.add_parser(
        "confirm-asset-reuse",
        help="确认资产规划后的角色、场景和道具复用清单",
    )
    asset_reuse.add_argument("--project", required=True)
    asset_reuse.add_argument("--episode", required=True)
    asset_reuse_choice = asset_reuse.add_mutually_exclusive_group(required=True)
    asset_reuse_choice.add_argument(
        "--reuse-all", action="store_true", help="接受全部已验证复用建议"
    )
    asset_reuse_choice.add_argument(
        "--generate-all", action="store_true", help="不复用任何候选，全部重新生成"
    )
    asset_reuse_choice.add_argument(
        "--reuse-ids",
        help="逗号分隔的确认复用资产 ID；其他候选将重新生成",
    )
    asset_reuse.add_argument(
        "--revise-confirmed",
        action="store_true",
        help="制作尚未开始时，修订已确认的复用清单",
    )

    shot_storyboard_decision = subparsers.add_parser(
        "set-shot-storyboard-decision",
        help="为单个视频单元切换故事板或直通视频 Prompt 路由",
    )
    shot_storyboard_decision.add_argument("--project", required=True)
    shot_storyboard_decision.add_argument("--episode", required=True)
    shot_storyboard_decision.add_argument("--shot", type=int, required=True)
    shot_storyboard_decision.add_argument("--decision", choices=["yes", "no"], required=True)

    accept_storyboards = subparsers.add_parser(
        "accept-storyboards",
        help="用户确认保留当前故事板并跳过审查阻断",
    )
    accept_storyboards.add_argument("--project", required=True)
    accept_storyboards.add_argument("--episode", required=True)

    style_decision = subparsers.add_parser(
        "set-style",
        help="在开始制作前记录本集视觉风格；不会使用隐式默认风格",
    )
    style_decision.add_argument("--project", required=True)
    style_decision.add_argument("--episode", required=True)
    style_decision.add_argument("--style", required=True)
    style_decision.add_argument("--extra-constraints", default="")

    aspect_ratio_decision = subparsers.add_parser(
        "set-aspect-ratio",
        help="在开始制作前记录本集画幅比例；不会使用隐式默认画幅",
    )
    aspect_ratio_decision.add_argument("--project", required=True)
    aspect_ratio_decision.add_argument("--episode", required=True)
    aspect_ratio_decision.add_argument("--aspect-ratio", required=True)

    video_model = subparsers.add_parser(
        "set-video-model",
        help="在剧集开始前记录本集使用 fast 或 mini 视频模型",
    )
    video_model.add_argument("--project", required=True)
    video_model.add_argument("--episode", required=True)
    video_model.add_argument("--model", choices=["fast", "mini"], required=True)

    video_session = subparsers.add_parser(
        "set-video-session",
        help="记录本集视频生成复用的 sessionId；生成首个镜头后锁定",
    )
    video_session.add_argument("--project", required=True)
    video_session.add_argument("--episode", required=True)
    video_session.add_argument("--session-id", required=True)

    remove_video = subparsers.add_parser(
        "remove-video-shot",
        help="删除指定视频镜头产物并解除该镜头的已生成锁定",
    )
    remove_video.add_argument("--project", required=True)
    remove_video.add_argument("--episode", required=True)
    remove_video.add_argument("--shot", type=int, required=True)

    adopt_video = subparsers.add_parser(
        "adopt-video-shot",
        help="登记已生成但尚未完成流程登记的视频文件，不重复调用 Provider",
    )
    adopt_video.add_argument("--project", required=True)
    adopt_video.add_argument("--episode", required=True)
    adopt_video.add_argument("--shot", type=int, required=True)

    import_video = subparsers.add_parser(
        "import-video-shot",
        help="导入用户提供的已生成视频并登记到指定镜头",
    )
    import_video.add_argument("--project", required=True)
    import_video.add_argument("--episode", required=True)
    import_video.add_argument("--shot", type=int, required=True)
    import_video.add_argument("--source", type=Path, required=True)

    finalize_video = subparsers.add_parser(
        "finalize-video-stage",
        help="修复已完成全部镜头但阶段回写失败的视频阶段状态",
    )
    finalize_video.add_argument("--project", required=True)
    finalize_video.add_argument("--episode", required=True)

    rough_cut = subparsers.add_parser(
        "local-rough-cut",
        help="使用已生成视频在本地按镜头顺序拼接粗剪，不调用视频 Provider",
    )
    rough_cut.add_argument("--project", required=True)
    rough_cut.add_argument("--episode", required=True)

    duration_exception = subparsers.add_parser(
        "allow-shot-duration-exception",
        help="Record a user-approved 3-second production-shot exception for one episode",
    )
    duration_exception.add_argument("--project", required=True)
    duration_exception.add_argument("--episode", required=True)
    duration_exception.add_argument("--min-seconds", type=int, choices=[3], default=3)
    duration_exception.add_argument("--reason", required=True)

    set_duration = subparsers.add_parser(
        "set-duration",
        help="Change the duration of a blocked episode before story design starts",
    )
    set_duration.add_argument("--project", required=True)
    set_duration.add_argument("--episode", required=True)
    set_duration.add_argument("--duration", type=int, required=True)
    set_duration.add_argument(
        "--preserve-assets",
        action="store_true",
        help="允许在已保留定妆资产的剧本重做边界更新总时长",
    )

    revise_script = subparsers.add_parser(
        "revise-script",
        help="Replace a locked source script and restart story design",
    )
    revise_script.add_argument("--project", required=True)
    revise_script.add_argument("--episode", required=True)
    revise_script.add_argument("--script-file", type=Path, required=True)
    revise_script.add_argument(
        "--preserve-stages",
        help="Comma-separated completed downstream stages to preserve while rebuilding story prompts",
    )

    restore_preserved = subparsers.add_parser(
        "restore-preserved-stages",
        help="Register existing character/scene/prop assets as preserved without regeneration",
    )
    restore_preserved.add_argument("--project", required=True)
    restore_preserved.add_argument("--episode", required=True)
    restore_preserved.add_argument("--stages", required=True)
    create_episode.add_argument(
        "--script-file",
        type=Path,
        help="UTF-8 file containing the user's script; script.md will be locked to its exact content",
    )

    advance = subparsers.add_parser(
        "advance",
        help="Advance until a native Agent handoff, block, completion, or video confirmation",
    )
    advance.add_argument("--project", required=True)
    advance.add_argument("--episode", required=True)

    pause = subparsers.add_parser("pause", help="Pause at a stable stage boundary")
    pause.add_argument("--project", required=True)
    pause.add_argument("--episode", required=True)
    pause.add_argument(
        "--recover-interrupted",
        action="store_true",
        help="Recover a process interrupted during a provider call, then pause",
    )

    remove_character_image = subparsers.add_parser(
        "remove-character-image",
        help="Remove one character design image while preserving its text lock and sibling assets",
    )
    remove_character_image.add_argument("--project", required=True)
    remove_character_image.add_argument("--episode", required=True)
    remove_character_image.add_argument(
        "--character",
        required=True,
        help="Character key, e.g. crowd_archetype or char_crowd_archetype",
    )

    remove_prop_image = subparsers.add_parser(
        "remove-prop-image",
        help="Remove one prop design image while preserving its textual lock",
    )
    remove_prop_image.add_argument("--project", required=True)
    remove_prop_image.add_argument("--episode", required=True)
    remove_prop_image.add_argument("--prop", required=True, help="Prop key, e.g. delivery_bag or knife")

    regenerate = subparsers.add_parser("regenerate")
    regenerate.add_argument("--project", required=True)
    regenerate.add_argument("--episode", required=True)
    regenerate.add_argument("--stage", required=True)
    regenerate.add_argument(
        "--shots",
        help="Comma-separated storyboard shots to regenerate without replacing siblings",
    )
    regenerate.add_argument(
        "--annotations",
        help="Comma-separated annotation ids that this repair must resolve after review",
    )
    regenerate.add_argument(
        "--repair-plan",
        help="Execute an already persisted repair plan",
    )
    regenerate.add_argument(
        "--repair-notes",
        type=Path,
        help="JSON object mapping selected storyboard numbers to review corrections",
    )
    regenerate.add_argument(
        "--characters",
        help="Comma-separated character keys for selective character board regeneration",
    )
    regenerate.add_argument(
        "--character-notes",
        help="JSON object mapping character keys to visual revision notes",
    )
    regenerate.add_argument(
        "--scene-revision",
        action="store_true",
        help="Reopen scene design for coordinated lighting and sky revision",
    )
    regenerate.add_argument(
        "--scene-images",
        help="Comma-separated scene keys for image-only regeneration",
    )
    regenerate.add_argument(
        "--scene-image-notes",
        help="JSON object mapping scene keys to image revision notes",
    )
    regenerate.add_argument(
        "--prop-images",
        help="Comma-separated prop keys for image-only regeneration",
    )
    regenerate.add_argument(
        "--prop-image-notes",
        help="JSON object mapping prop keys to image revision notes",
    )

    restart_style = subparsers.add_parser(
        "restart-style",
        help="Change the global visual style and restart the episode from story design",
    )
    restart_style.add_argument("--project", required=True)
    restart_style.add_argument("--episode", required=True)
    restart_style.add_argument("--style", required=True)
    restart_style.add_argument("--extra-constraints", default="")

    restart_stage = subparsers.add_parser(
        "restart-from-stage",
        help="Restart from a completed stage while preserving upstream assets",
    )
    restart_stage.add_argument("--project", required=True)
    restart_stage.add_argument("--episode", required=True)
    restart_stage.add_argument("--stage", required=True)
    restart_stage.add_argument(
        "--delete-old",
        action="store_true",
        help="Delete old outputs in the restarted stage and its downstream stages",
    )
    restart_stage.add_argument(
        "--design-notes",
        default="",
        help="Additional creative constraints for the restarted stages",
    )

    restart_preserving = subparsers.add_parser(
        "restart-preserving-stage",
        help="Restart all stages except one explicitly preserved completed stage",
    )
    restart_preserving.add_argument("--project", required=True)
    restart_preserving.add_argument("--episode", required=True)
    restart_preserving.add_argument("--preserve-stage", required=True)

    rollback = subparsers.add_parser(
        "rollback",
        help="快速回退；未明确选择时只输出确认问题，不修改资产",
    )
    rollback.add_argument("--project", required=True)
    rollback.add_argument("--episode", required=True)
    rollback.add_argument("--from-stage", required=True)
    rollback_group = rollback.add_mutually_exclusive_group()
    rollback_group.add_argument(
        "--keep-stages",
        help="逗号分隔的已完成阶段；只回退其余相关阶段",
    )
    rollback_group.add_argument(
        "--reset-all",
        action="store_true",
        help="不保留回退点及下游阶段的活动资产，旧版本进入历史",
    )

    confirm = subparsers.add_parser(
        "confirm-video",
        help="审批并只生成一个指定镜头",
    )
    confirm.add_argument("--project", required=True)
    confirm.add_argument("--episode", required=True)
    confirm.add_argument("--shot", type=int, required=True)

    supplement_confirm = subparsers.add_parser(
        "confirm-video-supplement",
        help="审批并生成一个显式新增的字母补充视频单元，例如 U08B",
    )
    supplement_confirm.add_argument("--project", required=True)
    supplement_confirm.add_argument("--episode", required=True)
    supplement_confirm.add_argument("--unit", required=True)
    supplement_confirm.add_argument("--duration", type=float, required=True)
    supplement_confirm.add_argument("--fingerprint", required=True)

    shot_aspect = subparsers.add_parser(
        "set-video-shot-aspect-ratio",
        help="为单个已生成视频单元设置重提画幅并撤下旧视频",
    )
    shot_aspect.add_argument("--project", required=True)
    shot_aspect.add_argument("--episode", required=True)
    shot_aspect.add_argument("--shot", type=int, required=True)
    shot_aspect.add_argument("--aspect-ratio", required=True)

    batch_confirm = subparsers.add_parser(
        "confirm-video-batch",
        help="并发审批并生成多个指定视频镜头（单集最多 2 个并发单元）",
    )
    batch_confirm.add_argument("--project", required=True)
    batch_confirm.add_argument("--episode", required=True)
    batch_confirm.add_argument("--shots", required=True)
    batch_confirm.add_argument(
        "--max-concurrency",
        type=int,
        default=2,
        help="单集同时提交的视频生成单元数，范围 1–2（默认 2）",
    )

    incremental = subparsers.add_parser(
        "incremental-shot",
        help="视频阶段准备单镜增量制作；默认只返回确认问题，不改已有资产",
    )
    incremental.add_argument("--project", required=True)
    incremental.add_argument("--episode", required=True)
    incremental.add_argument("--shot", type=int, required=True)
    incremental.add_argument("--notes", required=True)
    incremental.add_argument("--characters", default="")
    incremental.add_argument(
        "--scope",
        help="确认要重做的范围：character,storyboard，可多选；视频 Prompt 已在第一阶段锁定",
    )
    incremental.add_argument(
        "--confirm",
        action="store_true",
        help="确认本次增量范围；不会自动调用视频生成",
    )

    model = subparsers.add_parser("set-model")
    model.add_argument("--project", required=True)
    model.add_argument("--episode", required=True)
    model.add_argument("--model", required=True)

    image = subparsers.add_parser("set-image-provider")
    image.add_argument("--project", required=True)
    image.add_argument("--episode", required=True)
    image.add_argument("--provider", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_root = args.workspace.resolve()
    workspace = Workspace(workspace_root)
    annotations = AnnotationStore(workspace_root)
    repairs = RepairPlanStore(workspace_root)

    if args.command == "list":
        projects = workspace.list_projects()
        _print(
            {
                "projects": [
                    dict(
                        project,
                        episodes=[
                            _slim_episode(episode)
                            for episode in workspace.list_episodes(project["project_id"])
                        ],
                    )
                    for project in projects
                ]
            }
        )
        return 0

    if args.command == "delete-episode":
        if not args.confirm_delete:
            raise ValueError("episode deletion requires --confirm-delete")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status_path = workspace.statuses.path_for(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            if not status_path.is_file() or not episode_dir.is_dir():
                raise FileNotFoundError(
                    "episode does not exist: %s/%s" % (args.project, args.episode)
                )
            status = workspace.statuses.load(args.project, args.episode)
            if status.get("assets") and not args.purge_assets:
                raise RuntimeError(
                    "refusing to delete an episode with registered assets; pass --purge-assets explicitly"
                )
            shutil.rmtree(episode_dir)
            status_path.unlink()
            for sidecar_root, suffix in (
                (workspace_root / "events" / args.project, ".jsonl"),
                (workspace_root / "usage" / args.project, ".jsonl"),
                (workspace_root / "agent_timing" / args.project, ".jsonl"),
            ):
                sidecar_path = sidecar_root / (args.episode + suffix)
                if sidecar_path.is_file():
                    sidecar_path.unlink()
        _print(
            {
                "deleted": True,
                "project_id": args.project,
                "episode_id": args.episode,
                "recoverable": False,
            }
        )
        return 0

    if args.command == "status":
        status = workspace.statuses.load(args.project, args.episode)
        _print(status if args.full else _slim_status(status))
        return 0

    if args.command == "next-action":
        status = workspace.statuses.load(args.project, args.episode)
        request = status["agent_request"]
        incremental = status.get("incremental_request", {"state": "clear", "request": None})
        if request["state"] != "waiting" and incremental.get("state") != "waiting":
            _print({"state": "idle", "request": None, "incremental_request": None})
        elif request["state"] == "waiting":
            _print({"state": "waiting", "request": request["request"], "incremental_request": None})
        else:
            _print({"state": "waiting_user_confirmation", "request": None, "incremental_request": incremental["request"]})
        return 0

    if args.command == "annotations":
        _print(
            {
                "summary": workspace.statuses.load(args.project, args.episode)[
                    "annotations"
                ],
                "annotations": annotations.list(
                    args.project, args.episode, pending_only=True
                ),
            }
        )
        return 0

    if args.command == "repairs":
        _print({"repairs": repairs.list(args.project, args.episode)})
        return 0

    if args.command == "supersede-repair":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["run"].get("state") != "blocked":
                raise ValueError("only a blocked episode can retire an active repair plan")
            if status.get("agent_request", {}).get("state") != "clear":
                raise ValueError("cannot retire a repair plan while an Agent request is waiting")
            active_plan_id = status.get("repairs", {}).get("active_plan_id")
            if not active_plan_id:
                raise ValueError("episode has no active repair plan")
            plan = repairs.load(args.project, args.episode, active_plan_id)
            repairs.supersede_locked(
                status,
                plan,
                "跨镜头审查阻塞后，用户继续新的精确镜头修复；旧计划已安全收口",
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "已安全收口旧修复计划 %s，准备新的精确镜头修复" % active_plan_id,
                stage=plan["stage"],
                runner="codex",
                meta={"repair_plan_id": active_plan_id},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "plan-repair":
        if args.stage != "storyboard_generation":
            raise ValueError("V1 plan-repair supports storyboard_generation only")
        selected = _parse_shot_numbers(args.shots)
        notes = _load_repair_notes(args.repair_notes)
        unknown_notes = set(notes) - set(selected)
        if unknown_notes:
            raise ValueError("repair notes contain unselected shots")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            active_shots = {
                shot["shot_number"]
                for shot in resolved_storyboard_shots(status["creative_brief"])
            }
            unknown = set(selected) - active_shots
            if unknown:
                raise ValueError(
                    "repair shots are outside the active storyboard plan: %s"
                    % ", ".join(map(str, sorted(unknown)))
                )
            active_by_path = {
                asset["path"]: asset_id
                for asset_id, asset in status["assets"].items()
                if asset["stage"] == args.stage
            }
            targets = []
            for number in selected:
                task_id = "shot%02d" % number
                ensure_task(status, args.stage, task_id)
                asset_id = active_by_path.get("%s_storyboard.png" % task_id)
                # A prior failed generation may have no active canonical asset.
                # The persisted repair plan can still create that shot from its
                # prompt and reference bundle; ``None`` is an intentional base.
                if asset_id is not None:
                    status["assets"][asset_id]["task_id"] = task_id
                targets.append(
                    {
                        "task_id": task_id,
                        "asset_id": asset_id,
                        "instruction": notes.get(number, ""),
                    }
                )
            plan = repairs.create_locked(
                status,
                args.stage,
                targets,
                annotation_ids=_parse_ids(args.annotations),
                source="annotation" if args.annotations else "manual",
            )
            workspace.statuses.save(status)
        _print(plan)
        return 0

    if args.command == "handle-annotations":
        _print(annotations.mark_handled(args.project, args.episode, args.through))
        return 0

    if args.command == "create-project":
        _print(workspace.create_project(args.project, args.name))
        return 0

    if args.command == "create-episode":
        storyboard_decision = args.storyboard_decision
        if storyboard_decision is None:
            storyboard_decision = {
                "pending": "pending",
                "dialogue_direct": "no",
                "storyboard": "yes",
            }.get(args.production_mode, "no")
        production_mode = args.production_mode or {
            "no": "dialogue_direct",
            "yes": "storyboard",
            "pending": "pending",
        }[storyboard_decision]
        if args.production_mode in {"dialogue_direct", "storyboard"} and storyboard_decision == "pending":
            storyboard_decision = "no" if args.production_mode == "dialogue_direct" else "yes"
        if args.production_mode == "dialogue_direct" and storyboard_decision == "yes":
            raise ValueError("dialogue_direct conflicts with storyboard_decision=yes")
        if args.production_mode == "storyboard" and storyboard_decision == "no":
            raise ValueError("storyboard conflicts with storyboard_decision=no")
        provided_script = _load_script(args.script_file)
        brief = make_creative_brief(
            args.topic,
            args.hook,
            style=args.style,
            target_duration_sec=args.duration,
            aspect_ratio=args.aspect_ratio,
            development_brief=_load_development_brief(args.development_brief),
            provided_script=provided_script,
            production_mode=production_mode,
            storyboard_decision=storyboard_decision,
            video_model=args.video_model,
            video_session_id=args.video_session_id,
        )
        _print(
            workspace.create_episode(
                args.project, args.episode, brief, episode_name=args.name
            )
        )
        return 0

    if args.command == "set-style":
        if not args.style.strip():
            raise ValueError("style is required")
        style_constraints = expand_style(args.style, args.extra_constraints)
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["stages"]["story_design"]["state"] != "pending":
                raise RuntimeError("style can only be selected before story_design starts")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot set style while an Agent request is waiting")
            status["creative_brief"]["style"] = args.style.strip()
            status["creative_brief"]["style_constraints"] = style_constraints
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": "story_design",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户已选择本集视觉风格：%s" % args.style.strip(),
                stage="story_design",
                runner="codex",
                meta={"style": args.style.strip()},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "set-aspect-ratio":
        aspect_ratio = normalize_aspect_ratio(args.aspect_ratio)
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["stages"]["story_design"]["state"] != "pending":
                raise RuntimeError("aspect ratio can only be selected before story_design starts")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot set aspect ratio while an Agent request is waiting")
            status["creative_brief"]["aspect_ratio"] = aspect_ratio
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": "story_design",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户已选择本集画幅比例：%s" % aspect_ratio,
                stage="story_design",
                runner="codex",
                meta={"aspect_ratio": aspect_ratio},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "set-storyboard-decision":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            story_stage = status["stages"]["story_design"]
            agent_request = status.get("agent_request", {})
            if story_stage["state"] != "pending":
                if not (
                    story_stage["state"] == "running"
                    and status.get("run", {}).get("current_stage") == "story_design"
                    and agent_request.get("state") == "waiting"
                ):
                    raise RuntimeError("storyboard decision can only be set before story_design starts")
                story_stage.update(
                    {
                        "state": "pending",
                        "started_at": None,
                        "completed_at": None,
                        "outputs": [],
                        "handoff": None,
                        "error": None,
                        "review": {"state": "pending", "reason": ""},
                    }
                )
                task = status.get("tasks", {}).get("story_design:story_design")
                if task is not None:
                    task.update(
                        {
                            "state": "pending",
                            "execution_id": None,
                            "execution_history": [],
                            "asset_ids": [],
                            "error": None,
                            "completed_at": None,
                        }
                    )
                status["agent_request"] = {"state": "clear", "request": None}
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot set storyboard decision while an Agent request is waiting")
            status["creative_brief"]["storyboard_decision"] = args.decision
            status["creative_brief"]["production_mode"] = (
                "dialogue_direct" if args.decision == "no" else "storyboard"
            )
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": "story_design",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户已确认%s故事板，准备开始制作"
                % ("需要" if args.decision == "yes" else "不需要"),
                stage="story_design",
                runner="codex",
                meta={"storyboard_decision": args.decision},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "confirm-asset-reuse":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["stages"]["asset_planning"]["state"] not in {"done", "preserved"}:
                raise RuntimeError("asset planning must finish before reuse confirmation")
            if status["stages"]["character_design"]["state"] not in {"pending", "blocked"}:
                raise RuntimeError("asset reuse can only be confirmed before character design")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot confirm asset reuse while an Agent request is waiting")
            reuse_state = status.get("asset_reuse", {}).get("state")
            if reuse_state == "confirmed" and not args.revise_confirmed:
                raise RuntimeError(
                    "asset reuse is already confirmed; use --revise-confirmed before design starts"
                )
            if args.revise_confirmed:
                downstream_assets = [
                    asset_id
                    for asset_id, asset in status.get("assets", {}).items()
                    if asset.get("stage") in {"character_design", "visual_design"}
                    and asset.get("status", "active") == "active"
                ]
                if reuse_state != "confirmed":
                    raise RuntimeError(
                        "--revise-confirmed requires an existing confirmed reuse decision"
                    )
                if status["stages"]["visual_design"]["state"] not in {"pending", "blocked"}:
                    raise RuntimeError("asset reuse can only be revised before visual design")
                if downstream_assets:
                    raise RuntimeError(
                        "cannot revise asset reuse after design assets exist: %s"
                        % ", ".join(sorted(downstream_assets))
                    )
            proposal = status.get("asset_reuse", {})
            plan_path = workspace.episode_dir(args.project, args.episode) / "asset_plan.json"
            if not plan_path.is_file() or sha256_file(plan_path) != proposal.get(
                "asset_plan_sha256"
            ):
                raise RuntimeError("asset reuse proposal is stale; run advance to refresh it")
            if args.reuse_all:
                reuse_ids = [
                    item["asset_id"]
                    for item in proposal.get("items", [])
                    if item.get("source") is not None
                ]
            elif args.generate_all:
                reuse_ids = []
            else:
                reuse_ids = _parse_ids(args.reuse_ids)
            status["asset_reuse"] = confirm_asset_reuse(
                proposal,
                reuse_ids,
                allow_revision=args.revise_confirmed,
            )
            generated_ids = [
                item["asset_id"]
                for item in status["asset_reuse"]["items"]
                if item["decision"] == "generate"
            ]
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": "character_design",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户已确认定妆复用清单：复用 %d 项，新生成 %d 项"
                % (len(reuse_ids), len(generated_ids)),
                stage="asset_planning",
                runner="codex",
                meta={
                    "reuse_asset_ids": sorted(reuse_ids),
                    "generate_asset_ids": sorted(generated_ids),
                    "asset_plan_sha256": status["asset_reuse"]["asset_plan_sha256"],
                },
            )
        _print(_slim_status(status))
        return 0

    if args.command == "set-shot-storyboard-decision":
        status = _set_shot_storyboard_decision(
            workspace,
            args.project,
            args.episode,
            args.shot,
            args.decision,
        )
        _print(_slim_status(status))
        return 0

    if args.command == "accept-storyboards":
        status = _accept_current_storyboards(
            workspace,
            args.project,
            args.episode,
        )
        _print(_slim_status(status))
        return 0

    if args.command == "set-video-model":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["stages"]["story_design"]["state"] != "pending":
                raise RuntimeError("video model can only be selected before story_design starts")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot set video model while an Agent request is waiting")
            status["creative_brief"]["video_model"] = args.model
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": "story_design",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户已选择本集视频模型：%s" % args.model,
                stage="story_design",
                runner="codex",
                meta={"video_model": args.model},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "local-rough-cut":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            shot_count = resolved_shot_count(status["creative_brief"])
            video_assets = {}
            for asset in status.get("assets", {}).values():
                if asset.get("stage") != "video_generation" or asset.get("status") != "active":
                    continue
                match = __import__("re").fullmatch(r"shot(\d+)_video_jimeng_v\d+\.mp4", asset.get("path", ""))
                if match:
                    video_assets[int(match.group(1))] = asset
            if set(video_assets) != set(range(1, shot_count + 1)):
                raise ValueError("all generated shot videos are required before local rough cut")
            list_path = episode_dir / "intermediate" / "rough_cut_concat.txt"
            list_path.parent.mkdir(parents=True, exist_ok=True)
            list_path.write_text(
                "".join("file '%s'\n" % str((episode_dir / video_assets[n]["path"]).resolve()).replace("'", "'\\''")
                        for n in range(1, shot_count + 1)),
                encoding="utf-8",
            )
            output_path = episode_dir / "rough_cut.mp4"
            command = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                fallback = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
                            "-c:v", "libx264", "-c:a", "aac", str(output_path)]
                subprocess.run(fallback, check=True, capture_output=True, text=True)
            plan_path = episode_dir / "edit_post.md"
            plan_path.write_text(
                "# 本地粗剪交付\n\n"
                "## 剪映执行摘要\n\n"
                "当前文件是按视频生成单元 1 至 %d 顺序拼接的粗剪底片。请在剪映中按下方检查项补齐声音、字幕和人物信息；本文件不虚构未提供的音乐或音效素材。\n\n"
                "## 时间线与剪切\n\n"
                "| 顺序 | 来源 | 建议处理 |\n| --- | --- | --- |\n"
                % shot_count
                + "".join(
                    "| %d | 视频单元 %02d | 先保留完整可用画面；如需压节奏，只从本单元首尾无效帧开始裁切，保持对白和动作因果。 |\n"
                    % (number, number)
                    for number in range(1, shot_count + 1)
                )
                + "\n## 音乐进入/退出\n\n"
                "- 片头：先听原声和对白，确认情绪后再决定是否从 0 秒淡入音乐。\n"
                "- 对白段：音乐压到对白下方，避免盖住台词；笑点、反转和动作落点可短暂抽低或留白。\n"
                "- 片尾：最后一个动作或台词结束后再淡出，不要提前切断结果音。\n"
                "\n## 音效与环境声\n\n"
                "保留每个视频单元的有效环境声；只在画面确实出现接触、冲击、移动或喜剧落点时补音效，避免整段铺满。\n\n"
                "## 人物出场名\n\n"
                "首次出现且观众尚未明确身份的角色，在其第一次清晰露脸后加 1.5–2 秒出场名；已在前文建立身份的角色不重复添加。\n\n"
                "## 对白与字幕\n\n"
                "字幕跟随实际对白起止，单条不超过两行；人物名、重点词和笑点可单独断行，但不要用字幕替代画面内文字。\n\n"
                "## 导出设置\n\n"
                "沿用本集画幅比例和生成视频的帧率；导出前检查音画同步、字幕安全区、首尾黑帧、爆音、素材缺失和最终总时长。\n",
                encoding="utf-8",
            )
            stage = status["stages"]["edit_post"]
            stage["state"] = "running"
            set_task_state(status, "edit_post", "edit_post", "running")
            register_asset(status, episode_dir, "edit_post.rough_cut", "edit_post", "video",
                           "rough_cut", "本地粗剪视频", "rough_cut.mp4",
                           metadata={"method": "local_ffmpeg_concat", "shot_count": shot_count},
                           task_id="edit_post")
            register_asset(status, episode_dir, "edit_post.plan", "edit_post", "document",
                           "edit_post", "剪辑交付说明", "edit_post.md", task_id="edit_post")
            pass_review(status, "edit_post", "本地粗剪文件与剪辑说明已生成")
            complete_stage(status, "edit_post", ["edit_post.md", "rough_cut.mp4"], episode_dir)
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project, args.episode, "done", "完成：本地粗剪交付",
                stage="edit_post", runner="codex", meta={"method": "ffmpeg_concat"})
        _print(_slim_status(status))
        return 0
    if args.command == "finalize-video-stage":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            if status["stages"]["video_generation"].get("state") == "done":
                status["stages"]["video_generation"]["error"] = None
                status["run"]["last_error"] = None
                workspace.statuses.save(status)
                _print(_slim_status(status))
                return 0
            tasks = [
                task for task in status.get("tasks", {}).values()
                if task.get("stage") == "video_generation"
            ]
            shot_count = resolved_shot_count(status["creative_brief"])
            if len(tasks) < shot_count or any(task.get("state") != "passed" for task in tasks):
                raise ValueError("not all video shot tasks have passed")
            status["stages"]["video_generation"]["state"] = "reviewing"
            pass_review(status, "video_generation", "全部镜头均已生成并通过视频文件与元数据合同")
            outputs = [
                asset["path"] for asset in status["assets"].values()
                if asset.get("stage") == "video_generation" and asset.get("status") == "active"
            ]
            complete_stage(status, "video_generation", outputs, episode_dir)
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project, args.episode, "done", "修复完成：视频生成阶段状态回写",
                stage="video_generation", runner="codex")
        _print(_slim_status(status))
        return 0
    if args.command == "import-video-shot":
        if args.shot < 1:
            raise ValueError("shot must be positive")
        if not args.source.is_file():
            raise FileNotFoundError(str(args.source))
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            task_id = "shot%02d" % args.shot
            task_key = "video_generation:%s" % task_id
            task = status.get("tasks", {}).get(task_key)
            if task is None:
                # Recovery path: a provider may have completed a file before
                # the coordinator persisted the corresponding task record.
                # Importing an explicitly supplied local file may safely
                # recreate that bookkeeping record without calling the video
                # provider or resubmitting the shot.
                task = ensure_task(status, "video_generation", task_id)
            target_name = "shot%02d_video_jimeng_v%d.mp4" % (
                args.shot, status["stages"]["video_generation"]["revision"]
            )
            target = episode_dir / target_name
            target.write_bytes(args.source.read_bytes())
            asset_id = "video_generation.%s" % hashlib.sha256(
                ("video_generation" + target_name).encode("utf-8")
            ).hexdigest()[:16]
            if asset_id in status["assets"]:
                if status["assets"][asset_id].get("status") == "deleted":
                    status["assets"].pop(asset_id, None)
                else:
                    raise ValueError("asset_id already registered: %s" % asset_id)
            for existing_id, existing in list(status["assets"].items()):
                if existing.get("path") == target_name and existing.get("status") == "deleted":
                    status["assets"].pop(existing_id, None)
            record = register_asset(
                status, episode_dir, asset_id, "video_generation", "video",
                "shot_video", "镜头 %02d" % args.shot, target_name,
                metadata={"provider": "user_import", "imported_from": str(args.source)},
                task_id=task_id,
            )
            task["asset_ids"] = [asset_id]
            set_task_state(status, "video_generation", task_id, "passed",
                           review_reason="用户提供的真实视频文件已通过时长与媒体合同校验")
            stage = status["stages"]["video_generation"]
            stage.update({"state": "waiting_confirmation", "outputs":
                          list(dict.fromkeys(stage.get("outputs", []) + [target_name])),
                          "error": None})
            consume_video_shot_approval(status, args.shot)
            status["metrics"]["video_generations"] = int(
                status.get("metrics", {}).get("video_generations", 0)
            ) + 1
            status["run"].update({"state": "waiting_confirmation",
                                  "current_stage": "video_generation",
                                  "last_error": None,
                                  "waiting_for": "video_shot_approval"})
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project, args.episode, "asset",
                "已导入用户提供的镜头 %s 视频" % task_id,
                stage="video_generation", runner="codex",
                meta={"shot": args.shot, "path": target_name})
        _print(_slim_status(status))
        return 0
    if args.command == "adopt-video-shot":
        if args.shot < 1:
            raise ValueError("shot must be positive")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            task_id = "shot%02d" % args.shot
            task_key = "video_generation:%s" % task_id
            task = status.get("tasks", {}).get(task_key)
            if task is None:
                raise ValueError("video task does not exist: %s" % task_id)
            deleted = [
                (asset_id, asset)
                for asset_id, asset in status.get("assets", {}).items()
                if asset.get("stage") == "video_generation"
                and asset.get("task_id") == task_id
                and asset.get("status") == "deleted"
            ]
            if len(deleted) != 1:
                raise ValueError("expected one deleted video asset for %s" % task_id)
            old_id, old = deleted[0]
            relative_path = old["path"]
            source = resolve_asset_path(episode_dir, relative_path)
            if not source.is_file():
                raise FileNotFoundError(relative_path)
            status["assets"].pop(old_id, None)
            task["asset_ids"] = []
            record = register_asset(
                status,
                episode_dir,
                old_id,
                "video_generation",
                "video",
                "shot_video",
                "镜头 %02d" % args.shot,
                relative_path,
                metadata=old.get("metadata") or {},
                task_id=task_id,
            )
            task["asset_ids"] = [old_id]
            set_task_state(
                status,
                "video_generation",
                task_id,
                "passed",
                review_reason="已生成视频文件补登记并通过合同校验",
            )
            stage = status["stages"]["video_generation"]
            stage.update({
                "state": "waiting_confirmation",
                "outputs": [relative_path],
                "error": None,
            })
            consume_video_shot_approval(status, args.shot)
            status["metrics"]["video_generations"] = int(
                status.get("metrics", {}).get("video_generations", 0)
            ) + 1
            status["run"].update({
                "state": "waiting_confirmation",
                "current_stage": "video_generation",
                "last_error": None,
                "waiting_for": "video_shot_approval",
            })
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "asset",
                "已补登记镜头 %s 的视频生成结果" % task_id,
                stage="video_generation",
                runner="codex",
                meta={"shot": args.shot, "path": relative_path},
            )
        _print(_slim_status(status))
        return 0
    if args.command == "remove-video-shot":
        if args.shot < 1:
            raise ValueError("shot must be positive")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            task_id = "shot%02d" % args.shot
            task_key = "video_generation:%s" % task_id
            task = status.get("tasks", {}).get(task_key)
            if task is None:
                raise ValueError("video task does not exist: %s" % task_id)
            active_assets = [
                (asset_id, asset)
                for asset_id, asset in status.get("assets", {}).items()
                if asset.get("stage") == "video_generation"
                and asset.get("task_id") == task_id
                and asset.get("status") == "active"
            ]
            if not active_assets:
                raise ValueError("no active generated video exists for %s" % task_id)
            removed_paths = []
            for asset_id, asset in active_assets:
                source = resolve_asset_path(episode_dir, asset["path"])
                if source.is_file():
                    source.unlink()
                asset["status"] = "deleted"
                asset["deleted_at"] = utc_now()
                removed_paths.append(asset["path"])
                task["asset_ids"] = [
                    value for value in task.get("asset_ids", []) if value != asset_id
                ]
            set_task_state(status, "video_generation", task_id, "pending")
            stage = status["stages"]["video_generation"]
            stage["outputs"] = [
                value for value in stage.get("outputs", [])
                if value not in removed_paths
            ]
            status["metrics"]["video_generations"] = max(
                0, int(status.get("metrics", {}).get("video_generations", 0)) - len(active_assets)
            )
            status["run"].update(
                {
                    "state": "waiting_confirmation",
                    "current_stage": "video_generation",
                    "last_error": None,
                    "waiting_for": "video_shot_approval",
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "asset",
                "用户删除视频生成镜头 %s，允许重新配置 sessionId" % task_id,
                stage="video_generation",
                runner="codex",
                meta={"shot": args.shot, "paths": removed_paths},
            )
        _print(_slim_status(status))
        return 0
    if args.command == "set-video-session":
        if not args.session_id.isdigit():
            raise ValueError("video sessionId must contain only digits")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            current = status["creative_brief"].get("video_session_id")
            generated = any(
                asset.get("stage") == "video_generation"
                and asset.get("status") == "active"
                for asset in status.get("assets", {}).values()
            )
            if generated:
                raise RuntimeError(
                    "cannot set video sessionId after a video shot has been generated"
                )
            if current is not None and current != args.session_id:
                stage_state = status["stages"]["video_generation"].get("state")
                # Before the first video shot is generated, the user may
                # still replace the session at the approval boundary.
                if stage_state not in {"blocked", "waiting_confirmation"}:
                    raise RuntimeError(
                        "video sessionId is episode-locked; existing value is %s" % current
                    )
            status["creative_brief"]["video_session_id"] = args.session_id
            status["run"].update(
                {
                    "state": "idle",
                    "current_stage": status["run"].get("current_stage") or "video_generation",
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "已记录本集视频 sessionId；本集所有镜头将复用该值",
                stage="video_generation",
                runner="codex",
                meta={"video_session_id": args.session_id},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "incremental-shot":
        if not args.notes.strip() or len(args.notes.strip()) > 3000:
            raise ValueError("incremental shot notes must be 1-3000 characters")
        allowed_scope = {"character", "storyboard"}
        scope = [item.strip() for item in (args.scope or "").split(",") if item.strip()]
        if len(scope) != len(set(scope)) or any(item not in allowed_scope for item in scope):
            raise ValueError("scope must contain only character,storyboard; locked video prompts cannot be edited downstream")
        status = workspace.statuses.load(args.project, args.episode)
        shot_count = resolved_shot_count(status["creative_brief"])
        if not 1 <= args.shot <= shot_count:
            raise ValueError("shot must be between 1 and %d" % shot_count)
        if status["stages"]["video_binding"]["state"] not in {"done", "skipped"}:
            raise RuntimeError("incremental shot production requires video reference binding to be complete")
        if not args.confirm or not scope:
            _print(
                {
                    "state": "confirmation_required",
                    "shot": args.shot,
                    "question": "本次只改哪些执行资产？请明确角色定妆或故事板范围；第一阶段锁定的视频 Prompt 不在下游修改。",
                    "options": sorted(allowed_scope),
                    "preservation": "未选中的镜头、角色、场景、道具、故事板和视频 Prompt 全部保留；本次不自动调用视频生成。",
                }
            )
            return 0
        characters = _parse_ids(args.characters)
        request = {
            "shot": args.shot,
            "notes": args.notes.strip(),
            "scope": scope,
            "characters": characters,
            "preserve_unselected_assets": True,
            "video_generation_requires_separate_confirmation": True,
        }
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status.get("incremental_request", {}).get("state") == "waiting":
                raise RuntimeError("another incremental shot request is waiting for confirmation")
            status["incremental_request"] = {"state": "waiting", "request": request}
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "已登记单镜增量制作范围：镜头%02d；未选资产保持不变，视频生成仍需单独确认" % args.shot,
                stage="video_generation",
                runner="codex",
                meta=request,
            )
        _print({"state": "waiting_user_confirmation", "incremental_request": request})
        return 0

    if args.command == "allow-shot-duration-exception":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["stages"]["story_design"]["state"] != "blocked":
                raise RuntimeError("shot-duration exception requires a blocked story_design stage")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot set shot-duration exception while an Agent request is waiting")
            status["creative_brief"]["shot_duration_exception"] = {
                "min_sec": args.min_seconds,
                "reason": args.reason.strip(),
            }
            affected = invalidate_from(status, "story_design")
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户批准本集保留3秒镜头；下游将按例外时长重新生成",
                stage="story_design",
                runner="codex",
                meta={"min_shot_duration_sec": args.min_seconds, "affected_stages": list(affected)},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "set-duration":
        if args.duration < MIN_VIDEO_SHOT_DURATION_SEC:
            raise ValueError(
                "episode duration must be at least %d seconds"
                % MIN_VIDEO_SHOT_DURATION_SEC
            )
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["run"]["state"] not in {"blocked", "idle", "paused"}:
                raise RuntimeError("duration can only change at a stable episode boundary")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot change duration while an Agent request is waiting")
            story_state = status["stages"]["story_design"]["state"]
            if story_state != "blocked" and not (
                args.preserve_assets
                and story_state == "pending"
                and status["run"].get("current_stage") == "story_design"
            ):
                raise RuntimeError("duration change requires a blocked story_design stage or a preserved-assets story_design boundary")
            if status["assets"] and not args.preserve_assets:
                raise RuntimeError("duration change is only allowed before assets are generated")
            if args.preserve_assets:
                preservable_stages = [
                    stage_id
                    for stage_id in ("asset_planning", "character_design", "visual_design")
                    if status["stages"][stage_id]["state"]
                    in {"done", "skipped", "preserved"}
                ]
                affected = list(
                    rollback_with_preserved_stages(
                        status,
                        workspace.episode_dir(args.project, args.episode),
                        "story_design",
                        preservable_stages,
                    )
                )
            else:
                affected = invalidate_from(status, "story_design")
            status["creative_brief"]["target_duration_sec"] = args.duration
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户将单集时长调整为%d秒，重新开始剧本与分镜设计"
                % args.duration,
                stage="story_design",
                runner="codex",
                meta={"target_duration_sec": args.duration, "affected_stages": list(affected)},
            )
        _print(_slim_status(status))
        return 0

    if args.command == "revise-script":
        revised_script = _load_script(args.script_file)
        preserved_stage_ids = []
        if args.preserve_stages:
            preserved_stage_ids = [item.strip() for item in args.preserve_stages.split(",") if item.strip()]
            allowed = set(STAGE_IDS[1:])
            unknown = set(preserved_stage_ids) - allowed
            if unknown:
                raise ValueError("unknown preserved stages: %s" % ", ".join(sorted(unknown)))
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["run"]["state"] not in {"idle", "paused", "blocked"}:
                raise RuntimeError("script revision requires a stable episode boundary")
            if status.get("agent_request", {}).get("state") != "clear":
                raise RuntimeError("cannot revise the script while an Agent request is waiting")
            existing_locked_script = status["creative_brief"].get("provided_script")
            if existing_locked_script and revised_script == existing_locked_script:
                raise ValueError("revised script is identical to the current locked script")
            episode_dir = workspace.episode_dir(args.project, args.episode)
            if preserved_stage_ids:
                affected = list(
                    rollback_with_preserved_stages(
                        status, episode_dir, "story_design", preserved_stage_ids
                    )
                )
            else:
                affected = invalidate_from(status, "story_design")
            script_path = workspace.episode_dir(args.project, args.episode) / "inputs" / "original_script.md"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(revised_script, encoding="utf-8")
            status["creative_brief"]["provided_script"] = revised_script
            status["script_lock"] = {
                "state": "locked",
                "source_path": "inputs/original_script.md",
                "sha256": hashlib.sha256(revised_script.encode("utf-8")).hexdigest(),
            }
            status["run"].update(
                {"state": "idle", "current_stage": "story_design", "last_error": None}
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "用户修订并锁定剧本原文件；仅重做剧本与分镜设计及下游阶段",
                stage="story_design",
                runner="codex",
                meta={
                    "affected_stages": list(affected),
                    "preserved_stages": preserved_stage_ids,
                    "adopted_generated_script": not bool(existing_locked_script),
                },
            )
        _print(_slim_status(status))
        return 0

    if args.command == "restore-preserved-stages":
        requested = [item.strip() for item in args.stages.split(",") if item.strip()]
        allowed = {"character_design", "visual_design"}
        if not requested or any(item not in allowed for item in requested):
            raise ValueError("stages must contain only character_design,visual_design")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            for stage_id in requested:
                stage = status["stages"][stage_id]
                if stage["state"] not in {"pending", "preserved", "done"}:
                    raise RuntimeError("stage is not at a stable preservation boundary: %s" % stage_id)
                patterns = ("char_*",) if stage_id == "character_design" else ("scene_*", "prop_*")
                names = sorted(
                    path.name
                    for pattern in patterns
                    for path in episode_dir.glob(pattern)
                    if path.is_file() and path.suffix in {".txt", ".md", ".png"}
                )
                if not names:
                    raise FileNotFoundError("no existing assets found for %s" % stage_id)
                for name in names:
                    stem = Path(name).stem
                    asset_id = ("character_" if stage_id == "character_design" else "visual_") + re.sub(r"[^A-Za-z0-9_-]", "_", stem)
                    kind = "image" if name.endswith(".png") else ("prompt" if name.endswith(".txt") else "document")
                    # A preserved stage can contain the prompt/markdown and the
                    # actual PNG for one design.  They share a stem, but both
                    # must be registered so downstream reference binding can
                    # resolve the image without regenerating the design.
                    if asset_id in status["assets"]:
                        existing = status["assets"][asset_id]
                        if existing.get("path") == name:
                            continue
                        asset_id = asset_id + "_" + kind
                        if asset_id in status["assets"]:
                            continue
                    metadata = {"provider": "recovered_existing", "preserved": True}
                    if name.endswith("_sheet.png"):
                        metadata.update({"planned_asset_id": stem[:-len("_sheet")], "asset_type": "character" if stage_id == "character_design" else "scene_or_prop"})
                        catalog_manifest = (
                            workspace_root
                            / "projects"
                            / args.project
                            / "makeup"
                            / "manifest.json"
                        )
                        try:
                            catalog = json.loads(
                                catalog_manifest.read_text(encoding="utf-8")
                            )
                            catalog_entries = list(catalog.get("characters", [])) + list(
                                catalog.get("props", [])
                            )
                            catalog_entry = next(
                                (
                                    entry
                                    for entry in catalog_entries
                                    if entry.get("id") == metadata["planned_asset_id"]
                                ),
                                None,
                            )
                            if catalog_entry is not None:
                                metadata.update(
                                    {
                                        "source_catalog_path": (
                                            "workspace/projects/%s/makeup/%s"
                                            % (args.project, name)
                                        ),
                                        "source_catalog_id": catalog_entry["id"],
                                        "source_catalog_sha256": catalog_entry.get(
                                            "sha256"
                                        ),
                                    }
                                )
                        except (OSError, json.JSONDecodeError, TypeError):
                            pass
                    existing_by_path = next(
                        (asset for asset in status["assets"].values() if asset.get("path") == name),
                        None,
                    )
                    if existing_by_path is not None:
                        existing_by_path["stage"] = stage_id
                        existing_by_path["role"] = "preserved_asset"
                        existing_by_path["status"] = "active"
                        existing_by_path["metadata"] = {
                            **dict(existing_by_path.get("metadata") or {}),
                            **metadata,
                        }
                        continue
                    register_asset(status, episode_dir, asset_id, stage_id, kind, "preserved_asset", stem, name, metadata=metadata, task_id=stem)
                stage["state"] = "preserved"
                stage["outputs"] = names
                stage["review"] = {"state": "passed", "reason": "用户指定保留现有定妆资产；已校验并复用原文件，不调用Provider"}
                stage["handoff"] = {"summary": "现有定妆资产已保留，流程到达本阶段时跳过，不重做。", "outputs": names}
                for task in status.get("tasks", {}).values():
                    if task.get("stage") == stage_id:
                        set_task_state(status, stage_id, task["id"], "passed", review_reason=stage["review"]["reason"])
            status["run"].update({"state": "idle", "current_stage": "asset_planning", "last_error": None, "waiting_for": None})
            workspace.statuses.save(status)
        _print(_slim_status(status))
        return 0

    if args.command == "restart-style":
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            affected = restart_for_global_style(
                status,
                workspace.episode_dir(args.project, args.episode),
                args.style,
                args.extra_constraints,
            )
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "全局风格已切换为%s，重新开始剧本与分镜设计；下游失效：%s"
                % (args.style, ", ".join(affected[1:])),
                stage="story_design",
                runner="codex",
                meta={"style": args.style, "affected_stages": list(affected)},
            )
        _print(_slim_status(ChainRunner(PROJECT_ROOT, workspace_root).advance(
            args.project, args.episode, authorized=True
        )))
        return 0

    if args.command == "restart-from-stage":
        runner = ChainRunner(PROJECT_ROOT, workspace_root)
        _print(
            _slim_status(
                runner.restart_from_stage(
                    args.project,
                    args.episode,
                    args.stage,
                    delete_old=args.delete_old,
                    design_notes=args.design_notes,
                )
            )
        )
        return 0

    if args.command == "restart-preserving-stage":
        runner = ChainRunner(PROJECT_ROOT, workspace_root)
        _print(
            _slim_status(
                runner.restart_preserving_stage(
                    args.project, args.episode, args.preserve_stage
                )
            )
        )
        return 0

    if args.command == "rollback":
        if not args.keep_stages and not args.reset_all:
            status = workspace.statuses.load(args.project, args.episode)
            current = status["run"].get("current_stage")
            affected = []
            if args.from_stage in status["stages"]:
                start = STAGE_IDS.index(args.from_stage)
                affected = list(STAGE_IDS[start:])
            _print(
                {
                    "state": "confirmation_required",
                    "question": "回退时保留哪些阶段已有资产？选择 keep_stages，或选择 reset_all 全部重新制作。",
                    "from_stage": args.from_stage,
                    "current_stage": current,
                    "affected_stages": affected,
                    "options": {
                        "keep_stages": "例如 character_design,visual_design",
                        "reset_all": True,
                    },
                }
            )
            return 0
        preserved = _parse_ids(args.keep_stages)
        runner = ChainRunner(PROJECT_ROOT, workspace_root)
        _print(
            _slim_status(
                runner.rollback(
                    args.project,
                    args.episode,
                    args.from_stage,
                    preserved,
                    reset_all=args.reset_all,
                )
            )
        )
        return 0

    if args.command == "set-model":
        validate_model(args.model)
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["run"]["state"] == "running":
                raise RuntimeError("cannot change model while an episode is running")
            status["providers"]["model"] = args.model
            workspace.statuses.save(status)
        _print(_slim_status(status))
        return 0

    if args.command == "set-image-provider":
        if args.provider not in IMAGE_PROVIDERS:
            raise ValueError("unsupported image provider: %s" % args.provider)
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if status["run"]["state"] == "running":
                raise RuntimeError("cannot change provider while an episode is running")
            status["providers"]["image"] = args.provider
            workspace.statuses.save(status)
        _print(_slim_status(status))
        return 0

    runner = ChainRunner(PROJECT_ROOT, workspace_root)
    if args.command == "submit-agent-result":
        with args.result_file.open("r", encoding="utf-8") as handle:
            result = json.load(handle)
        if not isinstance(result, dict):
            raise ValueError("native Agent result must be a JSON object")
        _print(
            _slim_status(
                runner.submit_agent_result(
                    args.project, args.episode, args.request, result
                )
            )
        )
        return 0
    if args.command == "advance":
        _print(_slim_status(runner.advance(args.project, args.episode, authorized=True)))
        return 0
    if args.command == "pause":
        status = (
            runner.recover_interrupted_and_pause(args.project, args.episode)
            if args.recover_interrupted
            else runner.pause(args.project, args.episode)
        )
        _print(_slim_status(status))
        return 0
    if args.command == "remove-character-image":
        _print(
            _slim_status(
                runner.remove_character_image(
                    args.project,
                    args.episode,
                    args.character,
                    authorized=True,
                )
            )
        )
        return 0
    if args.command == "remove-prop-image":
        _print(
            _slim_status(
                runner.remove_prop_image(
                    args.project,
                    args.episode,
                    args.prop,
                    authorized=True,
                )
            )
        )
        return 0
    if args.command == "confirm-video":
        _print(
            _slim_status(
                runner.confirm_video_shot(
                    args.project,
                    args.episode,
                    args.shot,
                    authorized=True,
                )
            )
        )
        return 0
    if args.command == "confirm-video-supplement":
        _print(
            _slim_status(
                runner.confirm_video_supplement(
                    args.project,
                    args.episode,
                    args.unit,
                    args.duration,
                    args.fingerprint,
                    authorized=True,
                )
            )
        )
        return 0
    if args.command == "set-video-shot-aspect-ratio":
        aspect_ratio = normalize_aspect_ratio(args.aspect_ratio)
        if args.shot < 1:
            raise ValueError("shot must be positive")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            episode_dir = workspace.episode_dir(args.project, args.episode)
            shot_count = resolved_shot_count(status["creative_brief"])
            if args.shot > shot_count:
                raise ValueError("shot must be between 1 and %d" % shot_count)
            task_id = "shot%02d" % args.shot
            task_key = "video_generation:%s" % task_id
            task = status.get("tasks", {}).get(task_key)
            if task is None:
                raise ValueError("video task does not exist: %s" % task_id)
            active_assets = [
                (asset_id, asset)
                for asset_id, asset in status.get("assets", {}).items()
                if asset.get("stage") == "video_generation"
                and asset.get("task_id") == task_id
                and asset.get("status") == "active"
            ]
            if not active_assets:
                raise ValueError("no active generated video exists for %s" % task_id)
            removed_paths = []
            for asset_id, asset in active_assets:
                source = resolve_asset_path(episode_dir, asset["path"])
                if source.is_file():
                    source.unlink()
                asset["status"] = "deleted"
                asset["deleted_at"] = utc_now()
                removed_paths.append(asset["path"])
                task["asset_ids"] = [
                    value for value in task.get("asset_ids", []) if value != asset_id
                ]
            set_task_state(status, "video_generation", task_id, "pending")
            brief = status["creative_brief"]
            brief.setdefault("video_shot_aspect_ratios", {})[task_id] = aspect_ratio
            stage = status["stages"]["video_generation"]
            stage["outputs"] = [
                value for value in stage.get("outputs", []) if value not in removed_paths
            ]
            stage["state"] = "waiting_confirmation"
            status["run"].update(
                {
                    "state": "waiting_confirmation",
                    "current_stage": "video_generation",
                    "last_error": None,
                    "waiting_for": "video_shot_approval",
                }
            )
            status.setdefault("confirmations", {}).setdefault("video_generation", {}).setdefault("shots", {})[task_id] = {
                "state": "required",
                "revision_fingerprint": None,
                "approved_at": None,
                "generated_at": None,
            }
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "已将视频单元 %s 定向重提画幅设为 %s；旧视频已归档" % (task_id, aspect_ratio),
                stage="video_generation",
                runner="codex",
                meta={"shot": args.shot, "aspect_ratio": aspect_ratio, "paths": removed_paths},
            )
        _print(_slim_status(status))
        return 0
    if args.command == "confirm-video-batch":
        shots = [int(item.strip()) for item in args.shots.split(",") if item.strip()]
        _print(
            _slim_status(
                runner.confirm_video_shots_batch(
                    args.project,
                    args.episode,
                    shots,
                    max_concurrency=args.max_concurrency,
                    authorized=True,
                )
            )
        )
        return 0
    if args.command == "regenerate":
        if args.prop_images:
            if args.stage != "visual_design":
                raise ValueError("--prop-images is only supported for visual_design")
            if args.shots or args.repair_plan or args.repair_notes or args.annotations or args.characters or args.scene_revision or args.scene_images:
                raise ValueError("prop image regeneration cannot mix other regeneration arguments")
            try:
                notes = json.loads(args.prop_image_notes or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("--prop-image-notes must be a JSON object") from error
            if not isinstance(notes, dict):
                raise ValueError("--prop-image-notes must be a JSON object")
            _print(
                _slim_status(
                    runner.regenerate_prop_images(
                        args.project,
                        args.episode,
                        args.prop_images.split(","),
                        revision_notes=notes,
                        authorized=True,
                    )
                )
            )
            return 0
        if args.scene_images:
            if args.stage != "visual_design":
                raise ValueError("--scene-images is only supported for visual_design")
            if args.shots or args.repair_plan or args.repair_notes or args.annotations or args.characters or args.scene_revision:
                raise ValueError("scene image regeneration cannot mix other regeneration arguments")
            try:
                notes = json.loads(args.scene_image_notes or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("--scene-image-notes must be a JSON object") from error
            if not isinstance(notes, dict):
                raise ValueError("--scene-image-notes must be a JSON object")
            _print(
                _slim_status(
                    runner.regenerate_scene_images(
                        args.project,
                        args.episode,
                        args.scene_images.split(","),
                        revision_notes=notes,
                        authorized=True,
                    )
                )
            )
            return 0
        if args.scene_revision:
            if args.stage != "visual_design":
                raise ValueError("--scene-revision is only supported for visual_design")
            if args.shots or args.repair_plan or args.repair_notes or args.annotations or args.characters:
                raise ValueError("scene revision cannot mix character or storyboard repair arguments")
            _print(
                _slim_status(
                    runner.regenerate_visual_design(
                        args.project,
                        args.episode,
                        brighten=True,
                        add_sky=True,
                        authorized=True,
                    )
                )
            )
            return 0
        if args.characters:
            if args.stage != "character_design":
                raise ValueError("--characters is only supported for character_design")
            if args.shots or args.repair_plan or args.repair_notes or args.annotations:
                raise ValueError("character regeneration cannot mix storyboard repair arguments")
            try:
                notes = json.loads(args.character_notes or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("--character-notes must be a JSON object") from error
            if not isinstance(notes, dict):
                raise ValueError("--character-notes must be a JSON object")
            _print(
                _slim_status(
                    runner.regenerate_character_assets(
                        args.project,
                        args.episode,
                        args.characters.split(","),
                        revision_notes=notes,
                        authorized=True,
                    )
                )
            )
            return 0
        if args.shots or args.repair_plan:
            if args.stage != "storyboard_generation":
                raise ValueError("--shots is only supported for storyboard_generation")
            existing_plan = (
                repairs.load(args.project, args.episode, args.repair_plan)
                if args.repair_plan
                else None
            )
            selected = (
                [int(target["task_id"][-2:]) for target in existing_plan["targets"]]
                if existing_plan is not None
                else _parse_shot_numbers(args.shots)
            )
            notes = _load_repair_notes(args.repair_notes)
            status = runner.regenerate_storyboards(
                args.project,
                args.episode,
                selected,
                repair_notes=notes,
                annotation_ids=_parse_ids(args.annotations),
                repair_plan_id=args.repair_plan,
                authorized=True,
            )
            if status["run"]["state"] == "idle":
                status = runner.advance(args.project, args.episode, authorized=True)
            _print(_slim_status(status))
            return 0
        if args.repair_notes:
            raise ValueError("--repair-notes requires --shots")
        if args.annotations or args.repair_plan:
            raise ValueError("--annotations and --repair-plan require selective targets")
        with EpisodeLock(workspace_root, args.project, args.episode):
            status = workspace.statuses.load(args.project, args.episode)
            if args.stage not in status["stages"]:
                raise ValueError("unknown stage: %s" % args.stage)
            stage = status["stages"][args.stage]
            if stage["state"] == "pending" and not stage["outputs"]:
                raise ValueError("stage has no active result to regenerate")
            if stage["state"] not in {"blocked", "running", "reviewing", "repairing", "done"}:
                raise ValueError("stage is not ready for full regeneration: %s" % stage["state"])
            current_stage = status["run"].get("current_stage")
            if current_stage != args.stage:
                current_index = (
                    STAGE_IDS.index(current_stage)
                    if current_stage in STAGE_IDS
                    else None
                )
                requested_index = STAGE_IDS.index(args.stage)
                can_reopen_blocked_downstream = (
                    current_index is not None
                    and requested_index < current_index
                    and status["run"].get("state") == "blocked"
                    and status["stages"][current_stage]["state"] == "blocked"
                    and status.get("agent_request", {}).get("state") == "clear"
                )
                can_reopen_video_confirmation = (
                    current_index is not None
                    and requested_index < current_index
                    and current_stage == "video_generation"
                    and status["run"].get("state") == "waiting_confirmation"
                    and status["stages"][current_stage]["state"] == "waiting_confirmation"
                    and args.stage == "video_binding"
                )
                if not (can_reopen_blocked_downstream or can_reopen_video_confirmation):
                    raise ValueError("only the current stage can be fully regenerated")
            active_plan_id = status["repairs"].get("active_plan_id")
            if active_plan_id:
                plan = repairs.load(args.project, args.episode, active_plan_id)
                repairs.supersede_locked(
                    status,
                    plan,
                    "用户确认新的创作方向，旧修复计划已被更大范围重做替代",
                )
            affected = invalidate_from(status, args.stage)
            workspace.statuses.save(status)
            EventLog(workspace_root).append(
                args.project,
                args.episode,
                "decision",
                "Codex 请求重做 %s；下游失效：%s"
                % (args.stage, ", ".join(affected[1:]) or "无"),
                stage=args.stage,
                runner="codex",
                meta={"affected_stages": list(affected)},
            )
        _print(_slim_status(runner.advance(args.project, args.episode, authorized=True)))
        return 0

    raise RuntimeError("unsupported command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
