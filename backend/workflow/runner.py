import hashlib
import json
import os
import shutil
import tempfile
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from backend.core.assets import (
    archive_asset,
    register_asset,
    resolve_asset_path,
    validate_image,
    validate_text,
)
from backend.core.agent_actions import (
    clear_agent_request,
    request_agent,
    require_agent_request,
)
from backend.core.authorization import ProviderCallNotAuthorized
from backend.core.events import EventLog
from backend.core.repairs import RepairPlanStore
from backend.core.flow import STAGE_IDS
from backend.core.lock import EpisodeLock
from backend.core.models import DEFAULT_MODEL, validate_model
from backend.core.status import (
    StatusStore,
    normalize_shot_plan,
    normalize_storyboard_plan,
    min_video_shot_duration,
    resolved_shot_count,
    resolved_storyboard_shots,
    storyboard_required_shots,
    utc_now,
)
from backend.core.storage import restore_intermediate_storage
from backend.core.tasks import ensure_task, set_task_state
from backend.core.usage import UsageLedger, estimate_tokens
from backend.runners.image import (
    CodexImagegenRunner,
    ImageRequest,
    JimengImageRunner,
)
from backend.runners.video import (
    MAX_VIDEO_REFERENCES,
    JimengVideoRunner,
    VideoRequest,
)
from .chain import (
    approve_video,
    approve_video_shot,
    begin_stage,
    block_stage,
    cancel_waiting_agent_and_pause,
    complete_stage,
    flow_next,
    invalidate_downstream,
    invalidate_from,
    rollback_with_preserved_stages,
    remove_character_image_asset,
    pass_review,
    record_stage_handoff,
    pause_run,
    recover_interrupted_run_and_pause,
    restart_preserving_stage,
    restart_from_stage,
    remove_prop_image_asset,
    resume_stage_from_checkpoint,
    skip_stage,
    wait_for_storyboard_decision,
    wait_for_asset_reuse_confirmation,
    wait_for_style,
    wait_for_aspect_ratio,
    wait_for_video_model,
    wait_for_video_session,
    wait_for_video_confirmation,
    consume_video_shot_approval,
    consume_video_supplement_approval,
    approve_video_supplement,
    compute_video_supplement_fingerprint,
    require_video_shot_approval,
)
from .contracts import validate_text_output_names
from .context import ContextBuilder
from .asset_reuse import (
    build_asset_reuse_proposal,
    decision_for_asset,
    resolve_reuse_source_path,
    sha256_file,
)
from .output import (
    materialize_inline_stage_files,
    persist_staged_text_outputs,
    validate_stage_payload,
)


MAX_VIDEO_CONCURRENCY = 2


CODEX_TEXT_STAGES = {
    "story_design",
    "asset_planning",
    "character_design",
    "visual_design",
    "edit_post",
}

class AgentActionPending(RuntimeError):
    """Control-flow signal: the state machine is waiting for a native child."""

    pass


def _stable_asset_id(stage_id: str, path: str) -> str:
    return "%s.%s" % (
        stage_id,
        hashlib.sha256(path.encode("utf-8")).hexdigest()[:16],
    )


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent)
    )
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary_name)
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_asset_plan(episode_dir: Path) -> Dict[str, Any]:
    path = Path(episode_dir) / "asset_plan.json"
    if not path.is_file():
        raise FileNotFoundError("asset_plan.json is required")
    plan = json.loads(path.read_text(encoding="utf-8"))
    return plan


def _asset_plan_entry(episode_dir: Path, asset_id: str) -> Dict[str, Any]:
    for item in _load_asset_plan(episode_dir).get("assets", []):
        if item.get("id") == asset_id:
            return item
    raise ValueError("asset is not declared in asset_plan.json: %s" % asset_id)


def _video_reference_subject_prefix(
    item: Mapping[str, Any], planned_asset_names: Mapping[str, str]
) -> str:
    """Give every submitted reference a model-readable subject identity."""
    asset_id = str(item.get("asset_id") or "").strip()
    role = str(item.get("role") or "").strip()
    subject_name = str(planned_asset_names.get(asset_id) or "").strip()
    if role == "character" and not subject_name:
        raise ValueError(
            "character video reference has no semantic subject name: %s"
            % (asset_id or "<missing asset_id>")
        )
    if subject_name:
        role_label = {
            "character": "角色",
            "scene": "场景",
            "prop": "道具",
        }.get(role, "参考资产")
        return "%s：%s；" % (role_label, subject_name)
    if role == "storyboard":
        return "故事板：本视频单元；"
    return ("参考资产：%s；" % asset_id) if asset_id else ""


def _simplify_video_prompt_for_binding(prompt: str) -> str:
    """Compress only the reusable style header at video-reference binding time.

    The locked prompt file remains unchanged; shot content, dialogue, action notes,
    and negative constraints pass through verbatim.
    """
    replacements = {
        r"^\*\*默认风格：\*\*.*$": "**默认风格：**86版西游真人实拍神话剧质感，实景感强，真人比例，荒诞喜剧节奏",
        r"^\*\*场景质感：\*\*.*$": "**场景质感：**真实西游实景与传统神话剧搭景质感",
        r"^\*\*人物质感：\*\*.*$": "**人物质感：**真人比例、经典西游妆造，不复制具体演员面孔",
        r"^\*\*表演方式：\*\*.*$": "**表演方式：**认真演绎，喜剧来自荒诞剧情",
        r"^\*\*动作：\*\*.*$": "**动作：**清晰夸张，符合真人武打逻辑",
        r"^\*\*表情：\*\*.*$": "**表情：**强烈，不要卡通幼态",
        r"^\*\*特效：\*\*.*$": "**特效：**只在关键爆点使用，避免过度炫技",
        r"^\*\*现代元素：\*\*.*$": "**现代元素：**仅作为荒诞梗点嵌入西游世界",
        r"^\*\*画幅：\*\*.*$": "**画幅：**9:16竖屏",
        r"^\*\*转场：\*\*.*$": "**转场：**全部硬切",
        r"^\*\*生成原则：\*\*.*$": "**生成原则：**每个主题作为一个完整生成单元，内部自行完成运镜与镜头变化；天庭售后拆为5S+5S",
    }
    simplified = prompt
    for pattern, replacement in replacements.items():
        simplified = re.sub(pattern, replacement, simplified, count=1, flags=re.MULTILINE)
    return simplified


_VISIBLE_CHARACTER_LINE = re.compile(
    r"(?m)^\[\u672c\u5355\u5143\u53ef\u89c1\u89d2\u8272\]\s*(.+?)\s*$"
)
_ROLE_LOCK_VISIBLE_CHARACTERS = re.compile(
    r"(?m)^\u89d2\u8272\u4e0e\u7ad9\u4f4d\u9501\uff1a(?:\u672c\u5355\u5143)?\u53ef\u89c1\u89d2\u8272\u4e3a([^\uff1b\n]+)"
)
_LEGACY_NEGATIVE_ROLE_MARKERS = (
    "\u4e0d\u5f97",
    "\u4e0d\u51fa\u73b0",
    "\u4e0d\u5165\u955c",
    "\u4e0d\u53c2\u4e0e",
    "\u4e0d\u65b0\u589e",
    "\u5982\u5165\u955c",
    "\u4ec5\u4fdd\u6301\u65e2\u5b9a\u8fde\u7eed\u6027",
    "\u7981\u6b62",
    "\u65e0\u4ed6\u4eba",
)


def _character_alias_index(
    character_assets: Sequence[Mapping[str, Any]],
) -> Dict[str, set]:
    """Map character-name fragments to the ids that contain them."""
    aliases: Dict[str, set] = {}
    for asset in character_assets:
        asset_id = str(asset["id"])
        name = re.sub(r"\s+", "", str(asset["name"]))
        for start in range(len(name)):
            for end in range(start + 2, len(name) + 1):
                aliases.setdefault(name[start:end], set()).add(asset_id)
    return aliases


def _match_character_roster_text(
    text: str,
    alias_index: Mapping[str, set],
    *,
    allow_groups: bool,
) -> set:
    matched = set()
    for alias in sorted(alias_index, key=len, reverse=True):
        asset_ids = alias_index[alias]
        if alias not in text:
            continue
        if len(asset_ids) == 1:
            matched.update(asset_ids)
            continue
        if allow_groups and re.search(
            r"(?:[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\u540d|\u5168\u90e8|\u6240\u6709|\u4f17)"
            + re.escape(alias),
            text,
        ):
            matched.update(asset_ids)
    return matched


def _video_visible_character_ids(
    prompt: str,
    character_assets: Sequence[Mapping[str, Any]],
) -> tuple:
    """Return the locked visible-character roster and extraction mode.

    New prompts declare ``[\u672c\u5355\u5143\u53ef\u89c1\u89d2\u8272]`` explicitly. Existing locked
    prompts receive a compatibility pass over structured internal shots plus
    positive clauses in ``\u89d2\u8272\u4e0e\u7ad9\u4f4d\u9501``. Negative clauses are excluded.
    """
    alias_index = _character_alias_index(character_assets)
    explicit = _VISIBLE_CHARACTER_LINE.search(prompt)
    if explicit is None:
        explicit = _ROLE_LOCK_VISIBLE_CHARACTERS.search(prompt)
    if explicit is not None:
        value = explicit.group(1).strip().rstrip("\u3002")
        if value in {"\u65e0", "\u65e0\u89d2\u8272", "\u4ec5\u73af\u5883"}:
            return set(), "explicit", []
        tokens = [
            token.strip()
            for token in re.split(r"[\u3001,\uff0c/\uff1b;]|\u4e0e|\u548c", value)
            if token.strip()
        ]
        visible = set()
        unresolved = []
        for token in tokens:
            matched = _match_character_roster_text(
                token, alias_index, allow_groups=True
            )
            if matched:
                visible.update(matched)
            else:
                unresolved.append(token)
        return visible, "explicit", unresolved

    internal_lines = [
        line.strip()
        for line in prompt.splitlines()
        if re.match(r"^U\d{2}-\d{2}\uff5c", line.strip())
    ]
    role_line = next(
        (
            line.strip().removeprefix("\u89d2\u8272\u4e0e\u7ad9\u4f4d\u9501\uff1a")
            for line in prompt.splitlines()
            if line.strip().startswith("\u89d2\u8272\u4e0e\u7ad9\u4f4d\u9501\uff1a")
        ),
        "",
    )
    positive_role_clauses = [
        clause
        for clause in role_line.split("\uff1b")
        if clause.strip()
        and not any(marker in clause for marker in _LEGACY_NEGATIVE_ROLE_MARKERS)
    ]
    legacy_source = "\n".join(internal_lines + positive_role_clauses)
    visible = _match_character_roster_text(
        legacy_source, alias_index, allow_groups=True
    )
    exclusive = (
        "纯" in role_line
        or "无他人入镜" in role_line
        or re.search(r"(?:^|；)\s*仅(?:有|由)?", role_line) is not None
    )
    return (
        visible,
        "legacy_exclusive" if exclusive else "legacy_unverified",
        [],
    )


def _validate_video_character_reference_closure(
    plan: Mapping[str, Any],
    shot: Mapping[str, Any],
    prompt: str,
) -> Dict[str, Any]:
    """Report visible/planned character differences without blocking binding."""
    assets_by_id = {item["id"]: item for item in plan["assets"]}
    character_assets = [
        item for item in plan["assets"] if item.get("type") == "character"
    ]
    visible_ids, mode, unresolved = _video_visible_character_ids(
        prompt, character_assets
    )
    planned_ids = {
        reference["asset_id"]
        for reference in shot["references"]
        if assets_by_id[reference["asset_id"]]["type"] == "character"
    }
    missing = visible_ids - planned_ids
    stale = planned_ids - visible_ids
    verified = mode != "legacy_unverified"
    confidence = "high" if verified else "low"

    def labels(asset_ids: set) -> str:
        return ", ".join(
            "%s(%s)" % (asset_id, assets_by_id[asset_id]["name"])
            for asset_id in sorted(asset_ids)
        )

    issues = []
    if unresolved:
        issues.append(
            {
                "type": "visible_character_not_in_asset_plan",
                "confidence": confidence,
                "message": "\u53ef\u89c1\u89d2\u8272\u672a\u5728\u8d44\u4ea7\u89c4\u5212\u4e2d\u627e\u5230: %s"
                % ", ".join(unresolved),
            }
        )
    if missing:
        issues.append(
            {
                "type": "visible_character_missing_reference",
                "confidence": confidence,
                "message": "\u53ef\u89c1\u89d2\u8272\u7591\u4f3c\u7f3a\u5c11\u7ed1\u5b9a: %s" % labels(missing),
            }
        )
    if stale:
        issues.append(
            {
                "type": "possibly_stale_character_reference",
                "confidence": confidence,
                "message": "\u7591\u4f3c\u5b58\u5728\u975e\u53ef\u89c1\u89d2\u8272\u65e7\u7ed1\u5b9a: %s" % labels(stale),
            }
        )
    return {
        "mode": mode,
        "verified": verified,
        "visible_character_ids": sorted(visible_ids),
        "planned_character_reference_ids": sorted(planned_ids),
        "issues": issues,
    }


def _validate_planned_design_outputs(
    episode_dir: Path,
    stage_id: str,
    output_names: Sequence[str],
    reuse_asset_ids: Sequence[str] = (),
) -> None:
    plan = _load_asset_plan(episode_dir)
    allowed_types = {"character"} if stage_id == "character_design" else {"scene", "prop"}
    planned = {
        item["id"]: item for item in plan["assets"] if item["type"] in allowed_types
    }
    returned = {
        name[: -len("_prompt.txt")]
        for name in output_names
        if name.endswith("_prompt.txt")
    }
    unknown = returned - set(planned)
    if unknown:
        raise ValueError("design stage returned unplanned assets: %s" % ", ".join(sorted(unknown)))
    reused = set(reuse_asset_ids)
    returned_reuse = returned & reused
    if returned_reuse:
        raise ValueError(
            "design stage returned assets already locked for reuse: %s"
            % ", ".join(sorted(returned_reuse))
        )
    missing_required = {
        asset_id for asset_id, item in planned.items()
        if item["required"] and asset_id not in reused and asset_id not in returned
    }
    if missing_required:
        raise ValueError(
            "design stage omitted required planned assets: %s"
            % ", ".join(sorted(missing_required))
        )


class ChainRunner:
    """Advance the deterministic flow until confirmation, a block, or completion."""

    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        image_runners: Optional[Mapping[str, Any]] = None,
        video_runner: Optional[JimengVideoRunner] = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.statuses = StatusStore(self.workspace_root)
        self.events = EventLog(self.workspace_root)
        self.repairs = RepairPlanStore(self.workspace_root)
        self.usage = UsageLedger(self.workspace_root)
        self.contexts = ContextBuilder(self.project_root, self.workspace_root)
        self.image_runners = dict(
            image_runners
            or {
                "codex_imagegen": CodexImagegenRunner(),
                "jimeng_image_cli": JimengImageRunner(),
            }
        )
        self.video = video_runner or JimengVideoRunner()

    def _ensure_asset_reuse_proposal(
        self, status: Dict[str, Any], episode_dir: Path
    ) -> bool:
        """Create or refresh the user-facing reuse proposal before design starts."""
        if status["stages"]["asset_planning"]["state"] not in {"done", "preserved"}:
            return False
        if status["stages"]["character_design"]["state"] not in {"pending", "blocked"}:
            return False
        plan_path = episode_dir / "asset_plan.json"
        if not plan_path.is_file():
            return False
        plan_sha = sha256_file(plan_path)
        reuse = status.get("asset_reuse", {})
        if (
            reuse.get("state") in {"waiting_confirmation", "confirmed"}
            and reuse.get("asset_plan_sha256") == plan_sha
        ):
            return False
        status["asset_reuse"] = build_asset_reuse_proposal(
            self.workspace_root, status, episode_dir
        )
        reusable = [
            item["asset_id"]
            for item in status["asset_reuse"]["items"]
            if item["recommended"] == "reuse"
        ]
        self._emit(
            status,
            "decision",
            "定妆复用候选已识别，等待用户确认：%s"
            % (", ".join(reusable) if reusable else "无可直接复用资产"),
            "asset_planning",
            meta={
                "reusable_asset_ids": reusable,
                "asset_plan_sha256": plan_sha,
            },
        )
        return True

    def _materialize_confirmed_reuse_assets(
        self,
        status: Dict[str, Any],
        stage_id: str,
        episode_dir: Path,
        *,
        force_generate_ids: Sequence[str] = (),
    ) -> List[str]:
        """Copy the exact confirmed source triplet into the episode and register it."""
        reuse = status.get("asset_reuse", {})
        if reuse.get("state") != "confirmed":
            raise RuntimeError("asset reuse must be confirmed before design starts")
        plan_path = episode_dir / "asset_plan.json"
        if sha256_file(plan_path) != reuse.get("asset_plan_sha256"):
            raise RuntimeError("asset plan changed after reuse confirmation")
        allowed_types = {"character"} if stage_id == "character_design" else {"scene", "prop"}
        forced = set(force_generate_ids)
        outputs: List[str] = []
        for item in reuse.get("items", []):
            task_id = item["asset_id"]
            if (
                item["type"] not in allowed_types
                or item["decision"] != "reuse"
                or task_id in forced
            ):
                continue
            source = item["source"]
            sources = {
                "%s_sheet.md" % task_id: resolve_reuse_source_path(
                    self.workspace_root, source["spec_path"]
                ),
                "%s_prompt.txt" % task_id: resolve_reuse_source_path(
                    self.workspace_root, source["prompt_path"]
                ),
                "%s_sheet.png" % task_id: resolve_reuse_source_path(
                    self.workspace_root, source["image_path"]
                ),
            }
            if validate_image(sources["%s_sheet.png" % task_id]) != "image/png":
                raise ValueError("confirmed reuse image is invalid: %s" % task_id)
            if sha256_file(sources["%s_sheet.png" % task_id]) != source["image_sha256"]:
                raise ValueError("confirmed reuse image changed after confirmation: %s" % task_id)
            validate_text(sources["%s_sheet.md" % task_id])
            validate_text(sources["%s_prompt.txt" % task_id])
            ensure_task(status, stage_id, task_id)
            for target_name, source_path in sources.items():
                target_path = episode_dir / target_name
                source_sha = sha256_file(source_path)
                existing = next(
                    (
                        (asset_id, asset)
                        for asset_id, asset in status["assets"].items()
                        if asset.get("stage") == stage_id
                        and asset.get("path") == target_name
                    ),
                    None,
                )
                if existing is not None:
                    existing_id, existing_asset = existing
                    existing_matches = False
                    try:
                        existing_matches = (
                            target_path.is_file()
                            and target_path.stat().st_size == existing_asset.get("size")
                            and sha256_file(target_path) == source_sha
                        )
                    except OSError:
                        existing_matches = False
                    if existing_matches:
                        existing_asset.setdefault("metadata", {}).update(
                            {
                                "provider": "asset_reuse",
                                "planned_asset_id": task_id,
                                "reused": True,
                                "reuse_source_kind": source["kind"],
                                "reuse_source_episode": source["episode_id"],
                                "reuse_source_path": source["image_path"],
                                "reuse_source_sha256": source["image_sha256"],
                            }
                        )
                        outputs.append(target_name)
                        continue
                    if target_path.is_file():
                        archive_asset(status, episode_dir, existing_id)
                    status["assets"].pop(existing_id, None)

                _atomic_copy(source_path, target_path)
                is_prompt = target_name.endswith("_prompt.txt")
                is_image = target_name.endswith("_sheet.png")
                kind = "image" if is_image else ("prompt" if is_prompt else "document")
                role = (
                    {"character": "character_sheet", "scene": "scene_sheet", "prop": "prop_sheet"}[item["type"]]
                    if not is_prompt
                    else "image_prompt"
                )
                register_asset(
                    status,
                    episode_dir,
                    _stable_asset_id(stage_id, target_name),
                    stage_id,
                    kind,
                    role,
                    Path(target_name).stem,
                    target_name,
                    metadata={
                        "provider": "asset_reuse",
                        "asset_type": (
                            {"character": "character_sheet", "scene": "scene_sheet", "prop": "prop_sheet"}[item["type"]]
                            if is_image else role
                        ),
                        "planned_asset_id": task_id,
                        "required": bool(_asset_plan_entry(episode_dir, task_id)["required"]),
                        "reused": True,
                        "reuse_source_kind": source["kind"],
                        "reuse_source_episode": source["episode_id"],
                        "reuse_source_path": source["image_path"],
                        "reuse_source_sha256": source["image_sha256"],
                    },
                    task_id=task_id,
                )
                outputs.append(target_name)
            set_task_state(
                status,
                stage_id,
                task_id,
                "passed",
                review_reason="用户确认复用；源文件指纹与三件套合同通过",
            )
            self.usage.record(
                status["project_id"],
                status["episode_id"],
                kind="reuse",
                status="reused",
                stage_id=stage_id,
                task_id=task_id,
                provider="asset_reuse",
            )
            self._emit(
                status,
                "asset",
                "已按用户确认复用正式定妆：%s" % task_id,
                stage_id,
                runner="workbench",
                meta={
                    "asset_id": task_id,
                    "source_kind": source["kind"],
                    "source_episode": source["episode_id"],
                    "source_path": source["image_path"],
                },
            )
        return sorted(set(outputs))

    def _configure_model(self, status: Mapping[str, Any]) -> None:
        model = validate_model(status.get("providers", {}).get("model", DEFAULT_MODEL))
        for image_runner in self.image_runners.values():
            if hasattr(image_runner, "model"):
                image_runner.model = model

    def _video_submission_label(
        self, project_id: str, episode_id: str, shot_number: Any
    ) -> str:
        """Build a provider-facing label without changing the locked prompt."""
        project_name = project_id
        episode_name = episode_id
        project_meta = self.workspace_root / "projects" / project_id / "project.json"
        episode_meta = (
            self.workspace_root
            / "projects"
            / project_id
            / "episodes"
            / episode_id
            / "episode.json"
        )
        for path, key in ((project_meta, "project_name"), (episode_meta, "episode_name")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                value = payload.get("name")
                if isinstance(value, str) and value.strip():
                    if key == "project_name":
                        project_name = value.strip()
                    else:
                        episode_name = value.strip()
            except (OSError, json.JSONDecodeError):
                pass
        shot_label = (
            "%02d" % shot_number
            if isinstance(shot_number, int)
            else str(shot_number).upper()
        )
        return "【剧名：%s｜剧集：%s（%s）｜镜头：%s】" % (
            project_name,
            episode_name,
            episode_id,
            shot_label,
        )

    @staticmethod
    def _character_reference_paths(
        status: Mapping[str, Any], prompt_name: str
    ) -> tuple[Path, ...]:
        """Attach the declared visual reference to the matching character pass."""
        if prompt_name != "char_male_donkey_prompt.txt":
            return ()
        brief = status.get("creative_brief", {})
        development = brief.get("development_brief", {})
        anchor = development.get("visual_anchor", "")
        if not isinstance(anchor, str):
            return ()
        candidates = re.findall(
            r"/[^\s`'\"，。；：]+?\.(?:png|jpg|jpeg)", anchor, re.IGNORECASE
        )
        return tuple(
            path for path in (Path(item) for item in candidates) if path.is_file()
        )

    def episode_dir(self, project_id: str, episode_id: str) -> Path:
        return (
            self.workspace_root / "projects" / project_id / "episodes" / episode_id
        ).resolve()

    def restart_from_stage(
        self,
        project_id: str,
        episode_id: str,
        stage_id: str,
        *,
        delete_old: bool = False,
        design_notes: str = "",
    ) -> Dict[str, Any]:
        """Reopen a completed stage while preserving all upstream stages."""
        if stage_id not in STAGE_IDS:
            raise ValueError("unknown stage: %s" % stage_id)
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["run"].get("state") in {"waiting_agent", "waiting_user_decision"}:
                raise RuntimeError("cannot restart while an Agent handoff is waiting")
            requested_index = STAGE_IDS.index(stage_id)
            direct_mode = status.get("creative_brief", {}).get("production_mode") == "dialogue_direct"
            allowed_upstream_states = {"done", "skipped", "preserved"} if direct_mode else {"done", "preserved"}
            for upstream_id in STAGE_IDS[:requested_index]:
                if status["stages"][upstream_id]["state"] not in allowed_upstream_states:
                    raise RuntimeError("upstream stage is incomplete: %s" % upstream_id)
            if status["stages"][stage_id]["state"] not in {
                "done", "blocked", "reviewing", "repairing", "running", "waiting_confirmation"
            }:
                raise RuntimeError(
                    "stage is not ready for restart: %s" % status["stages"][stage_id]["state"]
                )
            if design_notes:
                constraints = status["creative_brief"].get("style_constraints", "")
                status["creative_brief"]["style_constraints"] = (
                    constraints.rstrip()
                    + "\n\n[本轮场景与道具重做约束]\n"
                    + design_notes.strip()
                )
            affected = restart_from_stage(
                status, episode_dir, stage_id, delete_old=delete_old
            )
            self._emit(
                status,
                "decision",
                "从 %s 重新开始；保留上游阶段，下游失效：%s%s"
                % (
                    status["stages"][stage_id]["label"],
                    ", ".join(affected[1:]) or "无",
                    "（旧产物已删除）" if delete_old else "",
                ),
                stage_id,
                runner="codex",
                meta={
                    "restart_stage": stage_id,
                    "affected_stages": list(affected),
                    "delete_old": delete_old,
                    "design_notes": design_notes,
                },
            )
            self.statuses.save(status)
        return self.advance(project_id, episode_id, authorized=True)

    def rollback(
        self,
        project_id: str,
        episode_id: str,
        from_stage_id: str,
        preserved_stage_ids: Sequence[str],
        *,
        reset_all: bool = False,
    ) -> Dict[str, Any]:
        """Perform an explicitly confirmed rollback without touching other episodes."""
        if from_stage_id not in STAGE_IDS:
            raise ValueError("unknown rollback stage: %s" % from_stage_id)
        if reset_all and preserved_stage_ids:
            raise ValueError("reset_all cannot be combined with preserved stages")
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["run"].get("state") in {"waiting_agent", "waiting_user_decision"}:
                raise RuntimeError("cannot rollback while an Agent or user decision is waiting")
            if reset_all:
                affected = restart_from_stage(status, episode_dir, from_stage_id)
                preserved = []
            else:
                affected = rollback_with_preserved_stages(
                    status, episode_dir, from_stage_id, preserved_stage_ids
                )
                preserved = list(preserved_stage_ids)
            self._emit(
                status,
                "decision",
                "快速回退：从 %s 重新开始；%s"
                % (
                    status["stages"][from_stage_id]["label"],
                    "全部相关阶段重新制作"
                    if reset_all
                    else "保留阶段：" + (", ".join(preserved) or "无"),
                ),
                from_stage_id,
                runner="codex",
                meta={
                    "rollback_from_stage": from_stage_id,
                    "reset_all": reset_all,
                    "preserved_stages": preserved,
                    "reset_stages": list(affected),
                },
            )
            self.statuses.save(status)
        return self.advance(project_id, episode_id, authorized=True)

    def restart_preserving_stage(
        self, project_id: str, episode_id: str, preserved_stage_id: str
    ) -> Dict[str, Any]:
        """Reset the episode while keeping one completed stage authoritative."""
        if preserved_stage_id not in STAGE_IDS:
            raise ValueError("unknown stage: %s" % preserved_stage_id)
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["run"].get("state") in {"waiting_agent", "waiting_user_decision"}:
                raise RuntimeError("cannot restart while an Agent handoff is waiting")
            if status["stages"][preserved_stage_id]["state"] != "done":
                raise RuntimeError(
                    "preserved stage must be complete: %s" % preserved_stage_id
                )
            affected = restart_preserving_stage(
                status, episode_dir, preserved_stage_id
            )
            self._emit(
                status,
                "decision",
                "重做除 %s 外的全部阶段；保留该阶段现有资产：%s"
                % (
                    status["stages"][preserved_stage_id]["label"],
                    ", ".join(affected),
                ),
                "story_design",
                runner="codex",
                meta={
                    "preserved_stage": preserved_stage_id,
                    "affected_stages": list(affected),
                },
            )
            self.statuses.save(status)
        return self.advance(project_id, episode_id, authorized=True)

    def _emit(
        self,
        status: Dict[str, Any],
        event_type: str,
        content: str,
        stage_id: Optional[str],
        runner: str = "workbench",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.events.append(
            status["project_id"],
            status["episode_id"],
            event_type,
            content,
            stage=stage_id,
            runner=runner,
            meta=meta,
        )

    def _codex_event_callback(self, status: Dict[str, Any], stage_id: str):
        def callback(event: Dict[str, Any]) -> None:
            event_type = event.get("type", "unknown")
            message = {
                "turn.started": "模型正在生成结构化内容",
                "turn.completed": "模型输出已接收，正在校验",
            }.get(event_type)
            # Codex may emit recoverable telemetry named `error` and still finish
            # successfully. Fatal failures are surfaced by the runner exception and
            # the stage block event, so raw protocol events should not alarm users.
            if message is None:
                return
            item_type = event.get("item", {}).get("type")
            self._emit(
                status,
                "progress",
                message,
                stage_id,
                runner="codex",
                meta={"event_type": event_type, "item_type": item_type},
            )

        return callback

    def _run_codex_stage(
        self, status: Dict[str, Any], stage_id: str, episode_dir: Path, authorized: bool
    ) -> List[str]:
        """Request native generation instead of launching ``codex exec``.

        The previous implementation synchronously launched a hidden Codex CLI
        process and then another hidden reviewer process.  The output now stops
        at a durable handoff; the main Codex conversation performs the visible
        delegation and returns through ``submit-agent-result``.
        """
        context = self.contexts.build(stage_id, status, episode_dir)
        stage = status["stages"][stage_id]
        stage["inputs"] = [path.name for path in context.loaded_text_paths] + [
            path.name for path in context.image_paths
        ]
        request = request_agent(
            status,
            kind="stage_generation",
            stage_id=stage_id,
            payload={
                "agent_profile": "manga-stage-producer",
                "context_mode": "fresh_node_only",
                "output_schema": str(
                    self.project_root / "backend" / "schemas" / "stage_output.schema.json"
                ),
                "creative_brief": status["creative_brief"],
                "instructions": context.prompt,
                "attached_images": [str(path.resolve()) for path in context.image_paths],
                "authority_limits": [
                    "只返回文件 path/content、review、summary 和方案字段；review 可带 issues 字符串数组；不要依赖共享文件系统",
                    "状态机负责安全写入、计算 size/SHA-256、校验并原子落盘；不得修改 status.json",
                    "不得调用视频生成；文字阶段不调用图片生成",
                ],
            },
        )
        output_dir = (
            episode_dir
            / "runs"
            / request["task_id"]
            / request["execution_id"]
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        request["payload"]["output_dir"] = str(output_dir.resolve())
        request["payload"]["instructions"] += (
            "\n\n## 输出约束\n"
            "请在最终 JSON 的 files 中返回每个文件的 path 和完整 content；顶层只允许 files、review、summary、shot_plan、storyboard_plan，review 可选 issues 字符串数组；不要依赖或假设可写入 output_dir；状态机将负责安全写入并计算 size、sha256。"
        )
        exception = status["creative_brief"].get("shot_duration_exception")
        if exception is not None:
            request["payload"]["instructions"] += (
                "\n\n## 用户已批准的单集时长例外\n"
                "本集保留原剧本中的3秒镜头；shot_plan 可使用3–15秒，必须保持总时长、原剧本镜头边界和 review 中的连续性说明一致。"
            )
        self.usage.record(
            status["project_id"],
            status["episode_id"],
            kind="agent_request",
            status="started",
            stage_id=stage_id,
            task_id=request.get("task_id"),
            execution_id=request.get("execution_id"),
            attempt=stage.get("attempt"),
            provider="codex",
            estimated_input_tokens=estimate_tokens(request["payload"]),
        )
        self._emit(
            status,
            "progress",
            "子 Agent 启动：%s｜读取已锁定输入并返回结构化结果"
            % status["stages"][stage_id]["label"],
            stage_id,
            runner="workbench",
            meta={
                "display_prompt": "读取本阶段已锁定输入，返回结构化结果；不改状态、不调用 Provider。"
            },
        )
        raise AgentActionPending("waiting for native stage generation")

    def _commit_text(
        self,
        status: Dict[str, Any],
        stage_id: str,
        payload: Mapping[str, Any],
        episode_dir: Path,
        output_dir: Path,
    ) -> List[str]:
        resolved_plan = None
        resolved_board_plan = None
        if stage_id == "story_design":
            resolved_plan = normalize_shot_plan(
                payload["shot_plan"], status["creative_brief"]["target_duration_sec"],
                require_resolved=True,
                enforce_video_duration_limits=True,
                min_duration_sec=min_video_shot_duration(status["creative_brief"]),
            )
            resolved_board_plan = normalize_storyboard_plan(
                payload["storyboard_plan"], resolved_plan,
                require_resolved=True,
                min_duration_sec=min_video_shot_duration(status["creative_brief"]),
            ) if payload.get("storyboard_plan") is not None else None
        if stage_id in {"character_design", "visual_design"}:
            reuse_asset_ids = [
                item["asset_id"]
                for item in status.get("asset_reuse", {}).get("items", [])
                if item.get("decision") == "reuse"
            ]
            _validate_planned_design_outputs(
                episode_dir,
                stage_id,
                [item["path"] for item in payload["files"]],
                reuse_asset_ids,
            )
        outputs = persist_staged_text_outputs(
            status,
            stage_id,
            payload,
            episode_dir,
            output_dir,
        )
        record_stage_handoff(status, stage_id, payload["summary"], outputs)
        status["stages"][stage_id]["review"] = {
            "state": "passed", "reason": payload["review"]["reason"]
        }
        if resolved_plan is not None:
            status["creative_brief"]["shot_plan"] = resolved_plan
        if resolved_board_plan is not None:
            status["creative_brief"]["storyboard_plan"] = resolved_board_plan
        return outputs

    @staticmethod
    def _validate_storyboard_review_result(
        request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> tuple:
        if set(result) != {"task_id", "decision", "issues", "reason"}:
            raise ValueError("storyboard review result has unexpected fields")
        task_id = request.get("task_id")
        if result.get("task_id") != task_id:
            raise ValueError("storyboard reviewer changed task_id")
        decision = result.get("decision")
        if decision not in {"pass", "warn", "block"}:
            raise ValueError("storyboard review decision is invalid")
        if not isinstance(result.get("issues"), list) or not isinstance(result.get("reason"), str):
            raise ValueError("storyboard review issue contract is invalid")
        for issue in result["issues"]:
            if not isinstance(issue, Mapping) or set(issue) != {
                "task_id",
                "reason",
                "repair_instruction",
            }:
                raise ValueError("storyboard review issue contract is invalid")
            if not str(issue["task_id"]).startswith("shot"):
                raise ValueError("storyboard review issue must target a shot")
        return decision, result["issues"], result["reason"].strip()

    def submit_agent_result(
        self, project_id: str, episode_id: str, request_id: str, result: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Validate a native child response and make the only state transition."""
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            request = require_agent_request(status, request_id)
            kind = request["kind"]
            stage_id = request["stage_id"]
            self._emit(
                status,
                "progress",
                "子 Agent 结束：%s｜结果已回传，主控开始校验" % status["stages"][stage_id]["label"],
                stage_id,
                runner="subagent",
                meta={
                    "display_prompt": "结果已接收；主控只校验合同、资产和阶段依赖。"
                },
            )
            result_estimate = estimate_tokens(result)
            if kind == "stage_generation":
                validate_stage_payload(stage_id, result, status["creative_brief"])
                clear_agent_request(status)
                review = result["review"]
                reason = review["reason"].strip()
                if not review["passed"]:
                    self.usage.record(
                        project_id, episode_id, kind="agent_result", status="failed",
                        stage_id=stage_id, task_id=request.get("task_id"),
                        execution_id=request.get("execution_id"), provider="codex",
                        estimated_output_tokens=result_estimate,
                        error=reason,
                    )
                    status["stages"][stage_id]["review"] = {"state": "failed", "reason": reason}
                    block_stage(status, stage_id, "主 Agent 自检未通过：%s" % reason)
                    self._emit(status, "block", "主 Agent 自检未通过：%s" % reason, stage_id, runner="subagent")
                else:
                    self.usage.record(
                        project_id, episode_id, kind="agent_result", status="success",
                        stage_id=stage_id, task_id=request.get("task_id"),
                        execution_id=request.get("execution_id"), provider="codex",
                        estimated_output_tokens=result_estimate,
                    )
                    output_value = request["payload"].get("output_dir")
                    if not isinstance(output_value, str) or not output_value.strip():
                        raise ValueError("stage generation request has no output_dir")
                    output_dir = Path(output_value)
                    result = materialize_inline_stage_files(result, output_dir)
                    outputs = self._commit_text(
                        status, stage_id, result, episode_dir, output_dir
                    )
                    self._emit(
                        status,
                        "decision",
                        "主 Agent 已完成自检并通过确定性合同：%s" % reason,
                        stage_id,
                        runner="subagent",
                    )
                    if stage_id in {"character_design", "visual_design"}:
                        status["stages"][stage_id]["state"] = "pending"
                        status["run"].update({"state": "idle", "current_stage": stage_id})
                    else:
                        complete_stage(status, stage_id, outputs, episode_dir)
                        if stage_id == "asset_planning":
                            self._ensure_asset_reuse_proposal(status, episode_dir)
                        self._emit(status, "done", "完成：%s" % status["stages"][stage_id]["label"], stage_id)
            elif kind == "storyboard_sequence_review":
                decision, issues, reason = self._validate_storyboard_review_result(request, result)
                clear_agent_request(status)
                if decision == "block":
                    self.usage.record(
                        project_id, episode_id, kind="review_round", status="failed",
                        stage_id=stage_id, task_id=request.get("task_id"),
                        execution_id=request.get("execution_id"), provider="codex",
                        estimated_input_tokens=estimate_tokens(request.get("payload", {})),
                        estimated_output_tokens=result_estimate,
                        error=reason,
                    )
                    targets = sorted(
                        {
                            issue.get("task_id")
                            for issue in issues
                            if isinstance(issue, Mapping) and str(issue.get("task_id", "")).startswith("shot")
                        }
                    )
                    detail = "跨镜头审查未通过；repair targets=%s；%s" % (", ".join(targets) or "未定位", reason)
                    active_plan_id = status["repairs"].get("active_plan_id")
                    if active_plan_id:
                        plan = self.repairs.load(project_id, episode_id, active_plan_id)
                        if plan["stage"] == stage_id and plan["state"] in {"planned", "processing", "blocked"}:
                            self.repairs.block_locked(status, plan, detail)
                    block_stage(status, stage_id, detail)
                    self._emit(status, "block", detail, stage_id, runner="subagent", meta={"issues": issues})
                else:
                    self.usage.record(
                        project_id, episode_id, kind="review_round", status="success",
                        stage_id=stage_id, task_id=request.get("task_id"),
                        execution_id=request.get("execution_id"), provider="codex",
                        estimated_input_tokens=estimate_tokens(request.get("payload", {})),
                        estimated_output_tokens=result_estimate,
                    )
                    outputs = sorted(
                        asset["path"] for asset in status["assets"].values()
                        if asset["stage"] == stage_id and asset["role"] == "storyboard_sheet"
                    )
                    pass_review(status, stage_id, reason)
                    complete_stage(status, stage_id, outputs, episode_dir)
                    active_plan_id = status["repairs"].get("active_plan_id")
                    if active_plan_id:
                        plan = self.repairs.load(project_id, episode_id, active_plan_id)
                        if plan["stage"] == stage_id:
                            for target in plan["targets"]:
                                self.repairs.set_target_state_locked(
                                    status,
                                    plan,
                                    target["task_id"],
                                    "passed",
                                    result_asset_id=target.get("result_asset_id"),
                                )
                            self.repairs.complete_locked(
                                status, plan,
                                reason="指定镜头已通过整组视觉终审",
                            )
                    self._emit(status, "done", "完成：%s" % status["stages"][stage_id]["label"], stage_id, runner="subagent")
            else:
                raise RuntimeError("native Agent result kind is not implemented: %s" % kind)
            self.statuses.save(status)
            return status

    def _image_runner(self, status: Dict[str, Any]) -> Any:
        provider = status["providers"]["image"]
        try:
            return self.image_runners[provider]
        except KeyError:
            raise RuntimeError("image provider is unavailable: %s" % provider)

    @staticmethod
    def _asset_file_matches_record(
        asset: Mapping[str, Any],
        episode_dir: Path,
        expected_kind: str,
        expected_revision: int,
    ) -> bool:
        if (
            asset.get("kind") != expected_kind
            or asset.get("revision") != expected_revision
        ):
            return False
        try:
            path = resolve_asset_path(episode_dir, asset["path"])
            if path.stat().st_size != asset.get("size"):
                return False
            if expected_kind == "image":
                return validate_image(path) == "image/png"
            validate_text(path)
            return True
        except (KeyError, OSError, ValueError):
            return False

    def _character_design_resume_plan(
        self, status: Dict[str, Any], episode_dir: Path
    ) -> Optional[Dict[str, Any]]:
        """Return a safe reuse plan only when reviewed character text is intact."""
        stage_id = "character_design"
        stage = status["stages"][stage_id]
        if stage["review"].get("state") != "passed":
            return None
        revision = stage["revision"]
        stage_assets = {
            asset_id: asset
            for asset_id, asset in status["assets"].items()
            if asset.get("stage") == stage_id
        }
        text_assets = {
            asset_id: asset
            for asset_id, asset in stage_assets.items()
            if asset.get("kind") in {"document", "prompt"}
        }
        text_names = sorted(asset["path"] for asset in text_assets.values())
        try:
            validate_text_output_names(
                stage_id, text_names, status["creative_brief"]
            )
        except ValueError:
            return None
        if not all(
            self._asset_file_matches_record(
                asset,
                episode_dir,
                asset["kind"],
                revision,
            )
            for asset in text_assets.values()
        ):
            return None

        prompt_names = sorted(
            name for name in text_names if name.endswith("_prompt.txt")
        )
        expected_images = {
            name[: -len("_prompt.txt")] + "_sheet.png" for name in prompt_names
        }
        reusable_ids = set(text_assets)
        valid_registered_images = set()
        for asset_id, asset in stage_assets.items():
            path = asset.get("path")
            if (
                path in expected_images
                and asset.get("role") == "character_sheet"
                and self._asset_file_matches_record(
                    asset, episode_dir, "image", revision
                )
            ):
                reusable_ids.add(asset_id)
                valid_registered_images.add(path)

        # A provider may have atomically placed a real canonical image immediately
        # before the process died, leaving no saved asset record. It is safe to
        # recover only exact prompt-derived canonical paths; unrelated orphan names
        # are deliberately ignored.
        recoverable_images = set()
        for target_name in expected_images - valid_registered_images:
            try:
                if (
                    validate_image(resolve_asset_path(episode_dir, target_name))
                    == "image/png"
                ):
                    recoverable_images.add(target_name)
            except (OSError, ValueError):
                continue
        reusable_images = valid_registered_images | recoverable_images
        return {
            "text_outputs": text_names,
            "asset_ids": reusable_ids,
            "expected_images": expected_images,
            "reusable_images": reusable_images,
            "missing_images": expected_images - reusable_images,
        }

    def _reviewed_design_text_checkpoint(
        self, status: Dict[str, Any], stage_id: str, episode_dir: Path
    ) -> Optional[List[str]]:
        """Return self-checked text assets that are ready for their image pass."""
        stage = status["stages"][stage_id]
        if stage["review"].get("state") != "passed":
            return None
        records = [
            asset
            for asset in status["assets"].values()
            if asset.get("stage") == stage_id and asset.get("kind") in {"document", "prompt"}
        ]
        names = sorted(asset["path"] for asset in records)
        try:
            validate_text_output_names(stage_id, names, status["creative_brief"])
        except ValueError:
            return None
        if not all(
            self._asset_file_matches_record(
                asset, episode_dir, asset["kind"], stage["revision"]
            )
            for asset in records
        ):
            return None
        return names

    def _storyboard_resume_plan(
        self, status: Dict[str, Any], episode_dir: Path
    ) -> Optional[Dict[str, Any]]:
        stage_id = "storyboard_generation"
        stage = status["stages"][stage_id]
        try:
            shots = resolved_storyboard_shots(status["creative_brief"])
        except ValueError:
            return None
        revision = stage["revision"]
        expected = {
            "shot%02d_storyboard.png" % shot["shot_number"]
            for shot in storyboard_required_shots(status["creative_brief"])
        }
        reusable_ids = set()
        valid_paths = set()
        for asset_id, asset in status["assets"].items():
            if (
                asset.get("stage") == stage_id
                and asset.get("path") in expected
                and asset.get("role") == "storyboard_sheet"
                and self._asset_file_matches_record(
                    asset, episode_dir, "image", revision
                )
            ):
                reusable_ids.add(asset_id)
                valid_paths.add(asset["path"])
        recoverable = set()
        for target_name in expected - valid_paths:
            try:
                if validate_image(episode_dir / target_name) == "image/png":
                    recoverable.add(target_name)
            except (OSError, ValueError):
                continue
        if not reusable_ids and not recoverable:
            return None
        return {
            "asset_ids": reusable_ids,
            "expected_images": expected,
            "reusable_images": valid_paths | recoverable,
        }

    def resume_incomplete_stage_from_checkpoint(
        self, status: Dict[str, Any], stage_id: str, episode_dir: Path
    ) -> bool:
        """Prepare a blocked/interrupted design node for a continuation attempt."""
        if stage_id == "character_design":
            plan = self._character_design_resume_plan(status, episode_dir)
        elif stage_id == "storyboard_generation":
            plan = self._storyboard_resume_plan(status, episode_dir)
        else:
            return False
        if plan is None:
            return False
        previous_state = status["stages"][stage_id]["state"]
        resume_stage_from_checkpoint(status, stage_id, plan["asset_ids"])
        if stage_id == "character_design":
            content = (
                "恢复角色定妆检查点：复用已通过文字设定及 %d/%d 张有效角色图，"
                "仅补 %d 张缺失或无效图片"
            ) % (
                len(plan["reusable_images"]),
                len(plan["expected_images"]),
                len(plan["missing_images"]),
            )
            missing_images = sorted(plan["missing_images"])
        else:
            # A provider call may finish and register an image just before the
            # coordinator is paused. Re-link those valid checkpoint assets to
            # passed shot tasks so the next attempt reuses them instead of
            # generating the same boards again.
            for asset_id in plan["asset_ids"]:
                asset = status["assets"][asset_id]
                task_id = asset.get("task_id")
                if task_id:
                    set_task_state(
                        status,
                        stage_id,
                        task_id,
                        "passed",
                        review_reason="恢复有效故事板检查点；等待整组视觉终审",
                    )
            content = (
                "恢复故事板 Task 检查点：保留 %d/%d 张真实图片，"
                "未通过新视觉审查前不会推进 Stage"
            ) % (len(plan["reusable_images"]), len(plan["expected_images"]))
            missing_images = sorted(plan["expected_images"] - plan["reusable_images"])
        self._emit(
            status,
            "decision",
            content,
            stage_id,
            meta={
                "previous_state": previous_state,
                "reused_images": sorted(plan["reusable_images"]),
                "missing_images": missing_images,
            },
        )
        return True

    def _render_design_assets(
        self,
        status: Dict[str, Any],
        stage_id: str,
        episode_dir: Path,
        text_outputs: List[str],
        authorized: bool,
        recover_existing: bool = False,
        prompt_overrides: Optional[Mapping[str, str]] = None,
    ) -> List[str]:
        outputs = list(text_outputs)
        if stage_id == "character_design":
            prompt_jobs = [
                (name, "character_sheet") for name in text_outputs
                if name.startswith("char_") and name.endswith("_prompt.txt")
            ]
        elif stage_id == "visual_design":
            prompt_jobs = [
                (name, "scene_sheet" if name.startswith("scene_") else "prop_sheet")
                for name in text_outputs
                if name.endswith("_prompt.txt")
                and (name.startswith("scene_") or name.startswith("prop_"))
            ]
        else:
            raise ValueError("unsupported design stage: %s" % stage_id)
        prompt_jobs.sort(key=lambda item: item[0])
        force_generate_ids = {
            name[: -len("_prompt.txt")]
            for name in (prompt_overrides or {})
            if name.endswith("_prompt.txt")
        }
        outputs = sorted(
            set(outputs)
            | set(
                self._materialize_confirmed_reuse_assets(
                    status,
                    stage_id,
                    episode_dir,
                    force_generate_ids=tuple(force_generate_ids),
                )
            )
        )
        image_runner = self._image_runner(status)
        for image_index, (prompt_name, asset_type) in enumerate(prompt_jobs, 1):
            target_name = prompt_name[: -len("_prompt.txt")] + "_sheet.png"
            task_id = target_name[: -len("_sheet.png")]
            planned_asset = _asset_plan_entry(episode_dir, task_id)
            required_asset = bool(planned_asset["required"])
            ensure_task(status, stage_id, task_id)
            target_path = episode_dir / target_name
            reuse_decision = decision_for_asset(status, task_id)
            if (
                reuse_decision is not None
                and reuse_decision.get("decision") == "reuse"
                and task_id not in force_generate_ids
            ):
                if target_name not in outputs:
                    raise RuntimeError(
                        "confirmed reuse asset was not materialized: %s" % task_id
                    )
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="用户确认复用；未调用图片 Provider",
                )
                continue
            existing = next(
                (
                    (asset_id, asset)
                    for asset_id, asset in status["assets"].items()
                    if asset.get("stage") == stage_id
                    and asset.get("path") == target_name
                ),
                None,
            )
            if existing is not None:
                asset_id, asset = existing
                if (
                    asset.get("role") == asset_type
                    and self._asset_file_matches_record(
                        asset,
                        episode_dir,
                        "image",
                        status["stages"][stage_id]["revision"],
                    )
                ):
                    outputs.append(target_name)
                    set_task_state(
                        status,
                        stage_id,
                        task_id,
                        "passed",
                        review_reason="复用的真实图片已通过字节和登记校验",
                    )
                    self._emit(
                        status,
                        "progress",
                        "复用已验证真实图片 %d/%d：%s"
                        % (image_index, len(prompt_jobs), target_name),
                        stage_id,
                        runner="workbench",
                    )
                    self.usage.record(
                        status["project_id"], status["episode_id"],
                        kind="reuse", status="reused", stage_id=stage_id,
                        task_id=task_id, provider=asset.get("metadata", {}).get("provider"),
                    )
                    continue
                status["assets"].pop(asset_id)

            existing_file_is_valid = False
            if recover_existing:
                try:
                    existing_file_is_valid = (
                        validate_image(target_path) == "image/png"
                    )
                except (OSError, ValueError):
                    existing_file_is_valid = False
            if existing_file_is_valid:
                register_asset(
                    status,
                    episode_dir,
                    _stable_asset_id(stage_id, target_name),
                    stage_id,
                    "image",
                    asset_type,
                    Path(target_name).stem,
                    target_name,
                    metadata={
                        "provider": status["providers"]["image"],
                        "asset_type": asset_type,
                        "recovered_existing": True,
                    },
                    task_id=task_id,
                )
                outputs.append(target_name)
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="恢复的真实图片已通过字节和登记校验",
                )
                self.statuses.save(status)
                self._emit(
                    status,
                    "asset",
                    "已校验并恢复现有真实图片登记：%s" % target_name,
                    stage_id,
                    runner="workbench",
                )
                continue

            self._emit(
                status,
                "progress",
                "正在生成真实图片 %d/%d：%s"
                % (image_index, len(prompt_jobs), target_name),
                stage_id,
                runner=status["providers"]["image"],
            )
            request = ImageRequest(
                prompt=(episode_dir / prompt_name).read_text(encoding="utf-8")
                + (("\n\n[用户定妆修订]\n" + prompt_overrides[prompt_name])
                   if prompt_overrides and prompt_name in prompt_overrides
                   else ""),
                target_path=target_path,
                asset_type=asset_type,
                reference_paths=self._character_reference_paths(status, prompt_name)
                if stage_id == "character_design"
                else (),
            )
            set_task_state(status, stage_id, task_id, "running")
            repair_plan_id = None
            active_plan_id = status["repairs"].get("active_plan_id")
            if active_plan_id:
                plan = self.repairs.load(status["project_id"], status["episode_id"], active_plan_id)
                if plan["stage"] == stage_id and any(
                    item["task_id"] == task_id for item in plan["targets"]
                ):
                    self.repairs.set_target_state_locked(status, plan, task_id, "running")
                    repair_plan_id = plan["id"]
            self.statuses.save(status)
            started_at = time.monotonic()
            try:
                result = image_runner.run(request, authorized=authorized)
            except Exception as error:
                self.usage.record(
                    status["project_id"], status["episode_id"],
                    kind="provider_call", status="failed", stage_id=stage_id,
                    task_id=task_id, execution_id=status["tasks"]["%s:%s" % (stage_id, task_id)].get("execution_id"),
                    attempt=status["tasks"]["%s:%s" % (stage_id, task_id)].get("attempt"),
                    provider=status["providers"]["image"],
                    estimated_input_tokens=estimate_tokens(request.prompt),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error=str(error),
                )
                if required_asset:
                    raise
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="可选定妆生成失败，按资源规划省略且后续不绑定",
                )
                omitted_texts = {
                    prompt_name,
                    prompt_name[: -len("_prompt.txt")] + "_sheet.md",
                }
                outputs = [name for name in outputs if name not in omitted_texts]
                for asset_id, asset in list(status["assets"].items()):
                    if asset.get("stage") == stage_id and asset.get("path") in omitted_texts:
                        status["assets"].pop(asset_id, None)
                for name in omitted_texts:
                    path = episode_dir / name
                    if path.is_file():
                        path.unlink()
                handoff = status["stages"][stage_id].get("handoff")
                if handoff:
                    handoff["outputs"] = [
                        name for name in handoff.get("outputs", [])
                        if name not in omitted_texts
                    ]
                    handoff["summary"] = (
                        handoff.get("summary", "")
                        + " 可选资产 %s 生成失败，已省略且不会进入后续绑定。"
                        % task_id
                    ).strip()
                self._emit(
                    status,
                    "decision",
                    "省略可选定妆资产：%s" % task_id,
                    stage_id,
                    runner="workbench",
                    meta={"asset_id": task_id, "required": False, "error": str(error)},
                )
                self.statuses.save(status)
                continue
            self.usage.record(
                status["project_id"], status["episode_id"],
                kind="provider_call", status="success", stage_id=stage_id,
                task_id=task_id, execution_id=status["tasks"]["%s:%s" % (stage_id, task_id)].get("execution_id"),
                attempt=status["tasks"]["%s:%s" % (stage_id, task_id)].get("attempt"),
                provider=result.get("provider", status["providers"]["image"]),
                estimated_input_tokens=estimate_tokens(request.prompt),
                image_count=1,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            register_asset(
                status,
                episode_dir,
                _stable_asset_id(stage_id, target_name),
                stage_id,
                "image",
                asset_type,
                Path(target_name).stem,
                target_name,
                metadata={
                    "provider": result["provider"],
                    "asset_type": asset_type,
                    "planned_asset_id": task_id,
                    "required": required_asset,
                },
                task_id=task_id,
            )
            set_task_state(status, stage_id, task_id, "reviewing")
            set_task_state(
                status,
                stage_id,
                task_id,
                "passed",
                review_reason="真实图片字节和角色/场景/道具资产合同通过",
            )
            status["metrics"]["image_generations"] += 1
            outputs.append(target_name)
            # Keep each successful provider result durable so a later image failure
            # or process interruption can continue at the first real gap.
            self.statuses.save(status)
            self._emit(
                status,
                "asset",
                "已生成真实图片：%s" % target_name,
                stage_id,
                runner=result["provider"],
            )
        return outputs

    def regenerate_character_assets(
        self,
        project_id: str,
        episode_id: str,
        character_names: Sequence[str],
        *,
        revision_notes: Optional[Mapping[str, str]] = None,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Rebuild selected character boards while preserving valid siblings."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "character regeneration requires an explicit current user action"
            )
        selected = tuple(sorted(set(name.strip() for name in character_names if name.strip())))
        if not selected:
            raise ValueError("at least one character is required")
        notes = {str(key).strip(): str(value).strip() for key, value in (revision_notes or {}).items()}
        if any(not value or len(value) > 3000 for value in notes.values()):
            raise ValueError("character revision notes must be non-empty and <= 3000 characters")

        stage_id = "character_design"
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            self._configure_model(status)
            if status["run"].get("state") == "waiting_agent":
                raise RuntimeError("cannot regenerate characters while an Agent handoff is waiting")
            stage = status["stages"][stage_id]
            if stage["state"] != "done":
                raise RuntimeError("character_design must be complete before selective regeneration")

            prompt_assets = {
                Path(asset["path"]).name[: -len("_prompt.txt")]: (asset_id, asset)
                for asset_id, asset in status["assets"].items()
                if asset.get("stage") == stage_id
                and asset.get("kind") == "prompt"
                and asset.get("path", "").endswith("_prompt.txt")
            }
            unknown = [name for name in selected if "char_%s" % name not in prompt_assets]
            if unknown:
                raise ValueError("unknown characters: %s" % ", ".join(unknown))

            downstream = invalidate_downstream(status, stage_id)
            prompt_overrides: Dict[str, str] = {}
            reuse_items = status.get("asset_reuse", {}).get("items", [])
            for item in reuse_items:
                if item.get("asset_id") in {"char_%s" % name for name in selected}:
                    # An explicit user-requested redesign supersedes the earlier
                    # reuse choice for only the selected stable asset ids.
                    item["decision"] = "generate"
            for name in selected:
                key = "char_%s" % name
                prompt_id, prompt_asset = prompt_assets[key]
                prompt_overrides[prompt_asset["path"]] = notes.get(name, "")
                target_name = key + "_sheet.png"
                image_record = next(
                    (
                        (asset_id, asset)
                        for asset_id, asset in status["assets"].items()
                        if asset.get("stage") == stage_id
                        and asset.get("path") == target_name
                    ),
                    None,
                )
                if image_record is None:
                    raise ValueError("character image asset is missing: %s" % target_name)
                image_id, _image_asset = image_record
                archive_asset(status, episode_dir, image_id)
                resolve_asset_path(episode_dir, target_name).unlink()
                status["assets"].pop(image_id)
                set_task_state(status, stage_id, key, "pending")

            stage.update(
                {
                    "state": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "outputs": [],
                    "error": None,
                }
            )
            status["run"].update({"state": "idle", "current_stage": stage_id, "last_error": None})
            self._emit(
                status,
                "decision",
                "局部重做角色定妆：%s；保留其余角色并使下游节点失效"
                % ", ".join("char_%s" % name for name in selected),
                stage_id,
                runner="codex",
                meta={"selected_characters": list(selected), "invalidated_stages": list(downstream)},
            )
            self.statuses.save(status)

            begin_stage(status, episode_dir)
            text_outputs = self._reviewed_design_text_checkpoint(status, stage_id, episode_dir)
            if text_outputs is None:
                raise RuntimeError("character text assets are not a valid checkpoint")
            outputs = self._render_design_assets(
                status,
                stage_id,
                episode_dir,
                text_outputs,
                authorized,
                recover_existing=True,
                prompt_overrides=prompt_overrides,
            )
            pass_review(status, stage_id, "局部角色定妆重做通过；未选中角色保留原有效资产")
            complete_stage(status, stage_id, outputs, episode_dir)
            self.statuses.save(status)
            return status

    def remove_character_image(
        self,
        project_id: str,
        episode_id: str,
        character_name: str,
        *,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Remove one character board image while preserving the text lock and siblings."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "character image removal requires an explicit current user action"
            )

        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            run_state = status["run"].get("state")
            if run_state not in {"idle", "paused"}:
                raise RuntimeError(
                    "character image removal requires an idle or paused episode"
                )

            image_name, image_id, affected = remove_character_image_asset(
                status, episode_dir, character_name
            )
            next_stage = next(
                (
                    stage_id
                    for stage_id in STAGE_IDS
                    if status["stages"][stage_id]["state"] not in {"done", "skipped", "preserved"}
                ),
                None,
            )
            status["run"].update(
                {
                    "state": run_state,
                    "current_stage": next_stage,
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            self._emit(
                status,
                "decision",
                "按用户请求移除角色定妆图：%s；保留其余角色资产" % image_name,
                "character_design",
                runner="codex",
                meta={
                    "removed_asset_id": image_id,
                    "removed_image": image_name,
                    "invalidated_stages": list(affected),
                },
            )
            self.statuses.save(status)
            return status

    def remove_prop_image(
        self,
        project_id: str,
        episode_id: str,
        prop_name: str,
        *,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Remove one prop board image while preserving textual locks."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "prop image removal requires an explicit current user action"
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            run_state = status["run"].get("state")
            if run_state not in {"idle", "paused"}:
                raise RuntimeError("prop image removal requires an idle or paused episode")
            image_name, image_id, affected = remove_prop_image_asset(
                status, episode_dir, prop_name
            )
            next_stage = next(
                (
                    stage_id
                    for stage_id in STAGE_IDS
                    if status["stages"][stage_id]["state"] not in {"done", "skipped", "preserved"}
                ),
                None,
            )
            status["run"].update(
                {
                    "state": run_state,
                    "current_stage": next_stage,
                    "last_error": None,
                    "waiting_for": None,
                }
            )
            self._emit(
                status,
                "decision",
                "按用户请求移除道具定妆图：%s；保留文字锁和其余道具资产" % image_name,
                "visual_design",
                runner="codex",
                meta={
                    "removed_asset_id": image_id,
                    "removed_image": image_name,
                    "invalidated_stages": list(affected),
                },
            )
            self.statuses.save(status)
            return status

    def regenerate_visual_design(
        self,
        project_id: str,
        episode_id: str,
        *,
        brighten: bool = True,
        add_sky: bool = True,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Reopen scene design for a coordinated lighting/sky revision."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "scene regeneration requires an explicit current user action"
            )
        scene_note = (
            "本轮场景统一修订：整体明度提高约一档，采用明亮银蓝天光作为主光，"
            "金色天宫反射作为副光；保持危机感但避免压黑、脏灰和夜景死黑，"
            "让建筑体量、云海层次、空间轴线和远景城市更清楚、更大气。"
            if brighten
            else ""
        )
        if add_sky:
            scene_note += (
                "新增一个公共天空环境定妆：九重天明亮云海天空底板，"
                "与月宫、坠落轴、南天门共用银蓝裂光从左上向右下的光向、"
                "墨绿云海色板和右上月宫/左上南天门的空间定位；不得加入角色或剧情动作。"
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            if status["run"].get("state") == "waiting_agent":
                raise RuntimeError("cannot regenerate scenes while an Agent handoff is waiting")
            stage = status["stages"]["visual_design"]
            if stage["state"] != "done":
                raise RuntimeError("visual_design must be complete before coordinated regeneration")
            current_stage = status["run"].get("current_stage")
            if current_stage in STAGE_IDS and STAGE_IDS.index(current_stage) < STAGE_IDS.index("visual_design"):
                raise RuntimeError("visual_design cannot be reopened before its upstream stage")
            if scene_note:
                constraints = status["creative_brief"].get("style_constraints", "")
                marker = "[场景统一修订]"
                if marker not in constraints:
                    status["creative_brief"]["style_constraints"] = (
                        constraints.rstrip() + "\n\n" + marker + "\n" + scene_note
                    )
            invalidate_from(status, "visual_design")
            self._emit(
                status,
                "decision",
                "重做场景定妆：提亮基础光线并新增公共天空底板",
                "visual_design",
                runner="codex",
                meta={"brighten": brighten, "add_sky": add_sky},
            )
            self.statuses.save(status)
        return self.advance(project_id, episode_id, authorized=True)

    def regenerate_scene_images(
        self,
        project_id: str,
        episode_id: str,
        scene_names: Sequence[str],
        *,
        revision_notes: Optional[Mapping[str, str]] = None,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Replace selected scene images and invalidate downstream consumers."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "scene image regeneration requires an explicit current user action"
            )
        selected = tuple(sorted(set(name.strip() for name in scene_names if name.strip())))
        if not selected:
            raise ValueError("at least one scene is required")
        notes = {str(key).strip(): str(value).strip() for key, value in (revision_notes or {}).items()}
        if any(not value or len(value) > 3000 for value in notes.values()):
            raise ValueError("scene revision notes must be non-empty and <= 3000 characters")

        stage_id = "visual_design"
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            if status["run"].get("state") == "waiting_agent":
                raise RuntimeError("cannot regenerate scene images while an Agent handoff is waiting")
            if status["stages"][stage_id]["state"] not in {"done", "preserved"}:
                raise RuntimeError("visual_design must be complete before image-only regeneration")
            prompt_assets = {
                Path(asset["path"]).name[: -len("_prompt.txt")]: (asset_id, asset)
                for asset_id, asset in status["assets"].items()
                if asset.get("stage") == stage_id
                and asset.get("kind") == "prompt"
                and asset.get("path", "").endswith("_prompt.txt")
            }
            unknown = [name for name in selected if "scene_%s" % name not in prompt_assets]
            if unknown:
                raise ValueError("unknown scenes: %s" % ", ".join(unknown))

            invalidate_downstream(status, stage_id)
            image_runner = self._image_runner(status)
            for name in selected:
                key = "scene_%s" % name
                _prompt_id, prompt_asset = prompt_assets[key]
                target_name = key + "_sheet.png"
                image_record = next(
                    (
                        (asset_id, asset)
                        for asset_id, asset in status["assets"].items()
                        if asset.get("stage") == stage_id
                        and asset.get("path") == target_name
                    ),
                    None,
                )
                if image_record is None:
                    raise ValueError("scene image asset is missing: %s" % target_name)
                image_id, _image_asset = image_record
                archive_asset(status, episode_dir, image_id)
                target_path = resolve_asset_path(episode_dir, target_name)
                target_path.unlink()
                status["assets"].pop(image_id)
                task_id = key
                set_task_state(status, stage_id, task_id, "running")
                self.statuses.save(status)
                prompt = resolve_asset_path(episode_dir, prompt_asset["path"]).read_text(encoding="utf-8")
                prompt += "\n\n[用户场景亮度修订]\n" + notes.get(name, "")
                result = image_runner.run(
                    ImageRequest(
                        prompt=prompt,
                        target_path=target_path,
                        asset_type="scene_sheet",
                    ),
                    authorized=authorized,
                )
                register_asset(
                    status,
                    episode_dir,
                    _stable_asset_id(stage_id, target_name),
                    stage_id,
                    "image",
                    "scene_sheet",
                    Path(target_name).stem,
                    target_name,
                    metadata={"provider": result["provider"], "asset_type": "scene_sheet"},
                    task_id=task_id,
                )
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="定向场景亮度修订后的真实图片通过资产合同",
                )
                status["metrics"]["image_generations"] += 1
                self.statuses.save(status)
            status["stages"][stage_id]["handoff"]["summary"] += " 已按用户反馈定向提亮：" + ", ".join(selected) + "。"
            self.statuses.save(status)
            return status

    def regenerate_prop_images(
        self,
        project_id: str,
        episode_id: str,
        prop_names: Sequence[str],
        *,
        revision_notes: Optional[Mapping[str, str]] = None,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Replace selected prop images while preserving reviewed prop text."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "prop image regeneration requires an explicit current user action"
            )
        selected = tuple(sorted(set(name.strip() for name in prop_names if name.strip())))
        if not selected:
            raise ValueError("at least one prop is required")
        notes = {
            str(key).strip(): str(value).strip()
            for key, value in (revision_notes or {}).items()
        }
        if any(not value or len(value) > 3000 for value in notes.values()):
            raise ValueError("prop revision notes must be non-empty and <= 3000 characters")

        stage_id = "visual_design"
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            if status["run"].get("state") == "waiting_agent":
                raise RuntimeError("cannot regenerate prop images while an Agent handoff is waiting")
            if status["stages"][stage_id]["state"] not in {"done", "preserved"}:
                raise RuntimeError("visual_design must be complete before image-only regeneration")

            prompt_assets = {
                Path(asset["path"]).name[: -len("_prompt.txt")]: (asset_id, asset)
                for asset_id, asset in status["assets"].items()
                if asset.get("stage") == stage_id
                and asset.get("kind") == "prompt"
                and asset.get("path", "").endswith("_prompt.txt")
            }
            unknown = [name for name in selected if "prop_%s" % name not in prompt_assets]
            if unknown:
                raise ValueError("unknown props: %s" % ", ".join(unknown))

            invalidate_downstream(status, stage_id)
            image_runner = self._image_runner(status)
            for name in selected:
                key = "prop_%s" % name
                _prompt_id, prompt_asset = prompt_assets[key]
                target_name = key + "_sheet.png"
                image_record = next(
                    (
                        (asset_id, asset)
                        for asset_id, asset in status["assets"].items()
                        if asset.get("stage") == stage_id
                        and asset.get("path") == target_name
                    ),
                    None,
                )
                if image_record is None:
                    raise ValueError("prop image asset is missing: %s" % target_name)
                image_id, _image_asset = image_record
                target_path = resolve_asset_path(episode_dir, target_name)
                if target_path.exists():
                    target_path.unlink()
                # The user explicitly rejected this image and asked not to
                # retain it as a historical revision.
                status["assets"].pop(image_id)
                task_id = key
                set_task_state(status, stage_id, task_id, "running")
                self.statuses.save(status)
                prompt = resolve_asset_path(
                    episode_dir, prompt_asset["path"]
                ).read_text(encoding="utf-8")
                prompt += "\n\n[用户道具视觉修订]\n" + notes.get(name, "")
                result = image_runner.run(
                    ImageRequest(
                        prompt=prompt,
                        target_path=target_path,
                        asset_type="prop_sheet",
                    ),
                    authorized=authorized,
                )
                register_asset(
                    status,
                    episode_dir,
                    _stable_asset_id(stage_id, target_name),
                    stage_id,
                    "image",
                    "prop_sheet",
                    Path(target_name).stem,
                    target_name,
                    metadata={"provider": result["provider"], "asset_type": "prop_sheet"},
                    task_id=task_id,
                )
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="定向道具视觉修订后的真实图片通过资产合同",
                )
                status["metrics"]["image_generations"] += 1
                self.statuses.save(status)
            status["stages"][stage_id]["handoff"]["summary"] += (
                " 已按用户反馈定向重做图片：" + ", ".join(selected) + "。"
            )
            self.statuses.save(status)
            return status

    def _render_storyboards(
        self, status: Dict[str, Any], episode_dir: Path, authorized: bool
    ) -> List[str]:
        stage_id = "storyboard_generation"
        outputs: List[str] = []
        storyboard_shots = storyboard_required_shots(status["creative_brief"])
        shot_count = len(storyboard_shots)
        pending = []
        for shot in storyboard_shots:
            shot_number = shot["shot_number"]
            task_id = "shot%02d" % shot_number
            task = ensure_task(status, stage_id, task_id)
            target_name = "%s_storyboard.png" % task_id
            existing = next(
                (
                    (asset_id, asset)
                    for asset_id, asset in status["assets"].items()
                    if asset.get("stage") == stage_id
                    and asset.get("path") == target_name
                    and self._asset_file_matches_record(
                        asset, episode_dir, "image", status["stages"][stage_id]["revision"]
                    )
                ),
                None,
            )
            if existing is not None and task["state"] in {"passed", "pending"}:
                if task["state"] != "passed":
                    set_task_state(
                        status,
                        stage_id,
                        task_id,
                        "passed",
                        review_reason="复用已落盘的有效故事板；等待整组视觉终审",
                    )
                outputs.append(target_name)
                self._emit(status, "progress", "复用已通过视觉审查的故事板 Task：%s" % task_id, stage_id, runner="workbench")
                self.usage.record(
                    status["project_id"], status["episode_id"],
                    kind="reuse", status="reused", stage_id=stage_id,
                    task_id=task_id, provider="workbench",
                )
                continue
            pending.append((shot, task, existing))

        if pending:
            image_runner = self._image_runner(status)
            jobs = []
            for shot, task, existing in pending:
                shot_number = shot["shot_number"]
                task_id = "shot%02d" % shot_number
                target_name = "%s_storyboard.png" % task_id
                target_path = episode_dir / target_name
                prompt = (episode_dir / ("%s_prompt_storyboard.txt" % task_id)).read_text(encoding="utf-8")
                binding_path = episode_dir / (
                    "shot%02d_storyboard_references.json" % shot_number
                )
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
                expected_hash = hashlib.sha256(
                    (episode_dir / ("shot%02d_prompt_storyboard.txt" % shot_number)).read_bytes()
                ).hexdigest()
                if binding.get("prompt_sha256") != expected_hash:
                    raise ValueError(
                        "storyboard binding no longer matches locked prompt for shot %02d"
                        % shot_number
                    )
                reference_bundle = {
                    "version": binding["version"],
                    "selection_mode": "locked_asset_plan",
                    "declared_roster": [
                        item["asset_id"] for item in binding["references"]
                    ],
                    "manifest": binding["references"],
                    "references": tuple(
                        Path(item["path"]) for item in binding["references"]
                    ),
                }
                feedback = task.get("review_feedback", "")
                if feedback:
                    prompt += "\n[上一轮可见 Reviewer 要求修复]\n%s\n严格修复这些问题，不得改变既定镜头、网格、时点和未被指出的锁定内容。" % feedback
                if existing is not None:
                    existing_id, _ = existing
                    archive_asset(status, episode_dir, existing_id)
                    status["assets"].pop(existing_id)
                set_task_state(status, stage_id, task_id, "running")
                jobs.append((shot, task_id, target_name, target_path, prompt, reference_bundle, task.get("execution_id"), task.get("attempt")))
                self._emit(status, "progress", "并行生成 %d×%d 独立故事板 %d/%d：%s" % (shot["columns"], shot["rows"], shot_number, shot_count, target_name), stage_id, runner=status["providers"]["image"])
            self.statuses.save(status)

            def run_job(job):
                shot, task_id, target_name, target_path, prompt, reference_bundle, execution_id, attempt = job
                started_at = time.monotonic()
                try:
                    result = image_runner.run(
                        ImageRequest(
                            prompt=prompt,
                            target_path=target_path,
                            asset_type="storyboard",
                            reference_paths=reference_bundle["references"],
                        ),
                        authorized=authorized,
                    )
                except Exception as error:
                    self.usage.record(
                        status["project_id"], status["episode_id"],
                        kind="provider_call", status="failed", stage_id=stage_id,
                        task_id=task_id, execution_id=execution_id, attempt=attempt,
                        provider=status["providers"]["image"],
                        estimated_input_tokens=estimate_tokens(prompt),
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        error=str(error),
                    )
                    raise
                self.usage.record(
                    status["project_id"], status["episode_id"],
                    kind="provider_call", status="success", stage_id=stage_id,
                    task_id=task_id, execution_id=execution_id, attempt=attempt,
                    provider=result.get("provider", status["providers"]["image"]),
                    estimated_input_tokens=estimate_tokens(prompt), image_count=1,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
                return shot, task_id, target_name, target_path, prompt, reference_bundle, result

            results = []
            if jobs:
                with ThreadPoolExecutor(max_workers=min(4, len(jobs)), thread_name_prefix="storyboard") as executor:
                    futures = [executor.submit(run_job, job) for job in jobs]
                    for future in as_completed(futures):
                        results.append(future.result())
            results.sort(key=lambda item: item[1])
            active_plan_id = status["repairs"].get("active_plan_id")
            repair_plan = (
                self.repairs.load(status["project_id"], status["episode_id"], active_plan_id)
                if active_plan_id
                else None
            )
            for shot, task_id, target_name, _target_path, _prompt, reference_bundle, result in results:
                status["metrics"]["image_generations"] += 1
                asset_id = _stable_asset_id(stage_id, target_name)
                register_asset(
                    status,
                    episode_dir,
                    asset_id,
                    stage_id,
                    "image",
                    "storyboard_sheet",
                    "镜头 %02d · %d×%d · %d格"
                    % (shot["shot_number"], shot["columns"], shot["rows"], shot["panel_count"]),
                    target_name,
                    metadata={
                        "provider": result["provider"],
                        "asset_type": "storyboard",
                        "columns": shot["columns"],
                        "rows": shot["rows"],
                        "panel_count": shot["panel_count"],
                        "timepoints_sec": shot["timepoints_sec"],
                        "generation_attempts": ensure_task(status, stage_id, task_id)["attempt"],
                        "reference_manifest_version": reference_bundle["version"],
                        "reference_selection_mode": reference_bundle["selection_mode"],
                        "declared_roster": reference_bundle["declared_roster"],
                        "reference_manifest": reference_bundle["manifest"],
                    },
                    task_id=task_id,
                )
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="真实图片字节、镜头网格和资产合同通过；等待整组视觉终审",
                )
                if repair_plan and repair_plan["stage"] == stage_id and any(
                    item["task_id"] == task_id for item in repair_plan["targets"]
                ):
                    self.repairs.set_target_state_locked(
                        status, repair_plan, task_id, "verifying", result_asset_id=asset_id
                    )
                outputs.append(target_name)
                self._emit(status, "asset", "已生成真实故事板：%s" % target_name, stage_id, runner=result["provider"])
            self.statuses.save(status)
            self._emit(status, "progress", "%d 张独立故事板已并行生成，进入一次整组视觉终审" % len(results), stage_id, runner="workbench")
        request_agent(
            status,
            kind="storyboard_sequence_review",
            stage_id=stage_id,
            task_id="sequence_review",
            payload={
                "agent_profile": "manga-storyboard-reviewer",
                "output_schema": str(
                    self.project_root / "backend" / "schemas" / "storyboard_review.schema.json"
                ),
                "creative_brief": status["creative_brief"],
                "objective": "审查整组故事板及其逐镜 Prompt。先以锁定剧本、storyboard.md 和上游动作脊柱为事实来源，禁止把后续规划新增的视觉奇观当成剧本内容。逐镜核对闭合角色/场景/道具 roster、首帧与末帧、网格阅读顺序、角色身份、空间轴线、屏幕方向、手脚与道具接触、受力方向、动作先后，以及是否出现未列角色/道具。只审查每个镜头内部的可执行性，不要求相邻镜头首尾姿态或道具状态硬匹配；镜头间变化交给剪辑和转场。只有以下情况才 block：新增/删减/改序剧本动作，核心人物或道具重复/消失，空间轴线或绕后关系不可执行，物理接触与受力明显错误，或故事板与锁定 Prompt 直接矛盾。角色定妆中未被真正锁定的细节（例如未出现在已通过定妆图的角、发饰或小纹样）在故事板中缺失只能 warn，不得反向阻断故事板；这类问题应归因到角色定妆阶段。技术正确但审美回报不足也先 warn，除非核心动作本身不可读。每个 block 必须给出可执行修复，不要用泛泛的‘加强连续性’。通过时 issues 返回空数组。",
                "attached_images": [
                    str((episode_dir / name).resolve()) for name in outputs
                ],
                "attached_prompts": [
                    str((episode_dir / (Path(name).stem.replace("_storyboard", "_prompt_storyboard") + ".txt")).resolve())
                    for name in outputs
                ],
            },
        )
        self._emit(
            status,
            "progress",
            "子 Agent 启动：%s｜读取故事板组并执行固定连续性审查"
            % status["stages"][stage_id]["label"],
            stage_id,
            runner="workbench",
            meta={
                "display_prompt": "核对空间轴、动作因果、道具状态和故事板可执行性；只返回结构化审查结果。"
            },
        )
        raise AgentActionPending("waiting for storyboard sequence review")
        return outputs

    def _bind_references(
        self,
        status: Dict[str, Any],
        episode_dir: Path,
        *,
        for_video: bool,
    ) -> List[str]:
        """Write deterministic reference manifests without touching locked prompts."""
        stage_id = "video_binding" if for_video else "storyboard_binding"
        plan = _load_asset_plan(episode_dir)
        active_images: Dict[str, str] = {}
        for asset in status["assets"].values():
            if asset.get("kind") != "image" or asset.get("stage") not in {
                "character_design",
                "visual_design",
            }:
                continue
            planned_id = asset.get("metadata", {}).get("planned_asset_id")
            if not planned_id:
                name = Path(asset["path"]).name
                if name.endswith("_sheet.png"):
                    planned_id = name[: -len("_sheet.png")]
            if planned_id:
                active_images[str(planned_id)] = str(
                    resolve_asset_path(episode_dir, asset["path"]).resolve()
                )

        outputs: List[str] = []
        character_warning_messages: List[str] = []
        for shot in plan["shots"]:
            shot_number = int(shot["shot_number"])
            if not for_video and not shot["storyboard_required"]:
                continue
            character_closure = None
            if for_video:
                prompt_name = "shot%02d_prompt_video.txt" % shot_number
                prompt_path = episode_dir / prompt_name
                if not prompt_path.is_file():
                    raise FileNotFoundError(
                        "locked video prompt is missing: %s" % prompt_name
                    )
                character_closure = _validate_video_character_reference_closure(
                    plan,
                    shot,
                    prompt_path.read_text(encoding="utf-8"),
                )
                character_warning_messages.extend(
                    "U%02d %s" % (shot_number, issue["message"])
                    for issue in character_closure["issues"]
                )
            references = []
            reference_necessities = []
            omitted_optional = []
            omitted_reference_assets = []
            omitted_reference_reasons = {}
            if for_video and shot["storyboard_required"]:
                board_name = "shot%02d_storyboard.png" % shot_number
                board_path = episode_dir / board_name
                if not board_path.is_file():
                    raise FileNotFoundError(
                        "required storyboard is missing for video unit %02d" % shot_number
                    )
                references.append(
                    {
                        "role": "storyboard",
                        "asset_id": "shot%02d_storyboard" % shot_number,
                        "path": str(board_path.resolve()),
                    }
                )
                reference_necessities.append("required")
            for reference in shot["references"]:
                asset_id = reference["asset_id"]
                path = active_images.get(asset_id)
                if path is None:
                    if reference["necessity"] == "required":
                        raise FileNotFoundError(
                            "required planned asset is unavailable: %s" % asset_id
                        )
                    omitted_optional.append(asset_id)
                    continue
                references.append(
                    {
                        "role": next(
                            item["type"] for item in plan["assets"]
                            if item["id"] == asset_id
                        ),
                        "asset_id": asset_id,
                        "path": path,
                        # Keep the binding-to-prompt relationship explicit
                        # without mutating the locked video prompt text.
                        "prompt_usage": reference.get("purpose", ""),
                    }
                )
                reference_necessities.append(reference["necessity"])
            limit = MAX_VIDEO_REFERENCES if for_video else 5
            if len(references) > limit:
                required_count = sum(
                    necessity == "required" for necessity in reference_necessities
                )
                if required_count > limit:
                    # Storyboard providers have a five-image cap. A required
                    # asset can still be fully specified by the locked prompt;
                    # when the cap is exceeded, preserve identity/space/action
                    # anchors and omit a static parked delivery bag reference.
                    # The bag remains a required episode asset and is still
                    # bound wherever its handoff or pickup is visually active.
                    if not for_video and "prop_kfc_delivery_bag" in [
                        item["asset_id"] for item in references
                    ]:
                        bag_index = next(
                            index
                            for index, item in enumerate(references)
                            if item["asset_id"] == "prop_kfc_delivery_bag"
                        )
                        omitted_reference_assets.append("prop_kfc_delivery_bag")
                        references.pop(bag_index)
                        reference_necessities.pop(bag_index)
                        required_count -= 1
                    if required_count > limit:
                        raise ValueError(
                            "video unit %02d has %d required references; limit is %d"
                            % (shot_number, required_count, limit)
                        )
                kept_references = []
                kept_necessities = []
                for item, necessity in zip(references, reference_necessities):
                    if necessity == "required" or len(kept_references) < limit:
                        kept_references.append(item)
                        kept_necessities.append(necessity)
                    else:
                        omitted_optional.append(item["asset_id"])
                references = kept_references
                reference_necessities = kept_necessities
            prompt_name = "shot%02d_prompt_video.txt" % shot_number
            prompt_path = episode_dir / prompt_name
            if for_video and not prompt_path.is_file():
                raise FileNotFoundError("locked video prompt is missing: %s" % prompt_name)
            manifest = {
                "version": "1.0",
                "shot_number": shot_number,
                "prompt_path": prompt_name if for_video else "shot%02d_prompt_storyboard.txt" % shot_number,
                "prompt_sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest()
                if for_video else hashlib.sha256(
                    (episode_dir / ("shot%02d_prompt_storyboard.txt" % shot_number)).read_bytes()
                ).hexdigest(),
                "references": references,
                "omitted_optional_assets": omitted_optional,
                "omitted_reference_assets": omitted_reference_assets,
                "omitted_reference_reasons": omitted_reference_reasons,
            }
            if for_video:
                manifest["character_reference_closure"] = character_closure
                manifest["reference_constraints"] = [
                    "每张定妆参考图只用于其 asset_id 对应角色或资产，不得把该参考图的脸型、妆造、服装或身份特征迁移给同镜头其他角色。",
                    "同一视频单元出现多个角色时，所有绑定角色必须保持彼此独立的脸型、妆造、服装、发型和身份特征，不得串形象或合并为同一主体。",
                    "角色变身或身份转换只按视频 Prompt 明确的变身对象执行；参考图仅在该对象成为目标身份后生效，其他角色始终保持各自绑定身份。",
                ]
                manifest["prompt_reference_instructions"] = [
                    "参考图按 references 数组顺序编号为参考图1、参考图2……；视频模型必须将每张图只用于其 asset_id 对应的角色或资产。",
                    "每张参考图的 prompt_usage 是该图在本单元 Prompt 中的唯一使用范围，不得将图中身份特征迁移给其他角色。",
                ]
            name = (
                "shot%02d_video_references.json" % shot_number
                if for_video
                else "shot%02d_storyboard_references.json" % shot_number
            )
            target = episode_dir / name
            target.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            register_asset(
                status,
                episode_dir,
                _stable_asset_id(stage_id, name),
                stage_id,
                "document",
                "reference_binding",
                "视频单元 %02d 参考绑定" % shot_number,
                name,
                metadata={
                    "prompt_sha256": manifest["prompt_sha256"],
                    "reference_count": len(references),
                    "omitted_optional_assets": omitted_optional,
                    "omitted_reference_assets": omitted_reference_assets,
                    "character_warning_count": len(
                        character_closure["issues"]
                    ) if character_closure else 0,
                },
                task_id="shot%02d" % shot_number,
            )
            outputs.append(name)
        handoff_summary = "仅绑定已规划且通过的参考图；锁定 Prompt 正文未修改。"
        if character_warning_messages:
            prefix = " 角色绑定疑似问题（不阻塞，共%d项）：" % len(
                character_warning_messages
            )
            available = 1200 - len(handoff_summary) - len(prefix)
            details = "；".join(character_warning_messages)
            if len(details) > available:
                details = details[: max(0, available - 12)].rstrip("；") + "；其余见绑定清单"
            handoff_summary += prefix + details
            self._emit(
                status,
                "decision",
                handoff_summary,
                stage_id,
                runner="workbench",
                meta={
                    "character_binding_warning_count": len(
                        character_warning_messages
                    )
                },
            )
        record_stage_handoff(status, stage_id, handoff_summary, outputs)
        return outputs

    def regenerate_storyboards(
        self,
        project_id: str,
        episode_id: str,
        shot_numbers: Sequence[int],
        *,
        repair_notes: Optional[Mapping[int, str]] = None,
        annotation_ids: Sequence[str] = (),
        repair_plan_id: Optional[str] = None,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Execute a persisted Stage + Shot repair plan with per-shot checkpoints."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "storyboard regeneration requires an explicit current user action"
            )
        if not shot_numbers:
            raise ValueError("at least one storyboard shot is required")
        if any(isinstance(number, bool) or not isinstance(number, int) for number in shot_numbers):
            raise ValueError("storyboard shot numbers must be integers")
        selected = tuple(sorted(set(shot_numbers)))
        notes = dict(repair_notes or {})
        for number, note in notes.items():
            if number not in selected:
                raise ValueError("repair note targets an unselected shot: %s" % number)
            if not isinstance(note, str) or not note.strip() or len(note.strip()) > 3000:
                raise ValueError("storyboard repair note is invalid for shot %s" % number)
            notes[number] = note.strip()

        stage_id = "storyboard_generation"
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            restore_intermediate_storage(status, episode_dir)
            self._configure_model(status)
            if status["stages"]["storyboard_binding"]["state"] != "done":
                raise RuntimeError("storyboard reference binding is not complete")
            storyboard_shots = storyboard_required_shots(status["creative_brief"])
            shots_by_number = {shot["shot_number"]: shot for shot in storyboard_shots}
            unknown = [number for number in selected if number not in shots_by_number]
            if unknown:
                raise ValueError(
                    "storyboard shots are outside the active plan: %s"
                    % ", ".join(map(str, unknown))
                )

            active_by_path = {
                asset["path"]: asset_id
                for asset_id, asset in status["assets"].items()
                if asset["stage"] == stage_id
            }
            for number in shots_by_number:
                task_id = "shot%02d" % number
                ensure_task(status, stage_id, task_id)
                target_name = "%s_storyboard.png" % task_id
                asset_id = active_by_path.get(target_name)
                if asset_id is not None:
                    status["assets"][asset_id]["task_id"] = task_id

            if repair_plan_id is not None:
                plan = self.repairs.load(project_id, episode_id, repair_plan_id)
                if plan["stage"] != stage_id:
                    raise ValueError("repair plan belongs to another stage")
                planned_numbers = tuple(
                    sorted(int(target["task_id"][-2:]) for target in plan["targets"])
                )
                if planned_numbers != selected:
                    raise ValueError("selected shots do not match the repair plan")
                for target in plan["targets"]:
                    number = int(target["task_id"][-2:])
                    if target.get("instruction") and number not in notes:
                        notes[number] = target["instruction"]
            else:
                plan = self.repairs.create_locked(
                    status,
                    stage_id,
                    [
                        {
                            "task_id": "shot%02d" % number,
                            "asset_id": active_by_path.get(
                                "shot%02d_storyboard.png" % number
                            ),
                            "instruction": notes.get(number, ""),
                        }
                        for number in selected
                    ],
                    annotation_ids=annotation_ids,
                    source="annotation" if annotation_ids else "manual",
                )

            self.repairs.begin_locked(status, plan)

            # Every unselected sibling must be a valid canonical image. This lets
            # interrupted full-stage invalidations recover without regenerating
            # known-good boards or trusting arbitrary orphan files.
            for number, shot in shots_by_number.items():
                if number in selected:
                    continue
                target = episode_dir / ("shot%02d_storyboard.png" % number)
                if validate_image(target) != "image/png":
                    raise ValueError(
                        "unselected storyboard is missing or invalid; include shot %02d"
                        % number
                    )

            stage = status["stages"][stage_id]
            if stage["state"] == "done":
                stage["revision"] += 1
            elif stage["state"] not in {
                "pending",
                "running",
                "reviewing",
                "repairing",
                "blocked",
            }:
                raise RuntimeError("storyboard stage cannot be selectively regenerated")

            downstream = invalidate_downstream(status, stage_id)
            for number in selected:
                target_name = "shot%02d_storyboard.png" % number
                asset_id = active_by_path.get(target_name)
                if asset_id is not None and asset_id in status["assets"]:
                    archive_asset(status, episode_dir, asset_id)
            status["assets"] = {
                asset_id: asset
                for asset_id, asset in status["assets"].items()
                if asset["stage"] != stage_id
            }
            outputs = []
            for number, shot in shots_by_number.items():
                target_name = "shot%02d_storyboard.png" % number
                target = episode_dir / target_name
                outputs.append(target_name)
                try:
                    existing_is_valid = validate_image(target) == "image/png"
                except (OSError, ValueError):
                    existing_is_valid = False
                if not existing_is_valid:
                    if number not in selected:
                        raise ValueError("storyboard is missing or invalid: %s" % target_name)
                    continue
                task_id = "shot%02d" % number
                if number in selected:
                    continue
                register_asset(
                    status,
                    episode_dir,
                    _stable_asset_id(stage_id, target_name),
                    stage_id,
                    "image",
                    "storyboard_sheet",
                    "镜头 %02d · %d×%d · %d格"
                    % (number, shot["columns"], shot["rows"], shot["panel_count"]),
                    target_name,
                    metadata={
                        "provider": "recovered_existing",
                        "asset_type": "storyboard",
                        "columns": shot["columns"],
                        "rows": shot["rows"],
                        "panel_count": shot["panel_count"],
                        "timepoints_sec": shot["timepoints_sec"],
                        "reused_during_selective_regeneration": number not in selected,
                    },
                    task_id=task_id,
                )
                set_task_state(
                    status,
                    stage_id,
                    task_id,
                    "passed",
                    review_reason="未选中故事板已通过真实字节与合同校验",
                )

            stage.update(
                {
                    "state": "pending",
                    "attempt": stage["attempt"] + 1,
                    "started_at": utc_now(),
                    "completed_at": None,
                    "inputs": [
                        "shot%02d_prompt_storyboard.txt" % number for number in selected
                    ],
                    "outputs": [],
                    "review": {"state": "pending", "reason": ""},
                    "error": None,
                }
            )
            status["run"].update(
                {"state": "idle", "current_stage": stage_id, "last_error": None}
            )
            for number in selected:
                task = ensure_task(status, stage_id, "shot%02d" % number)
                if number in notes:
                    task["review_feedback"] = notes[number]
            self._emit(
                status,
                "decision",
                "仅重做故事板镜头：%s；保留其余 %d 张已验证故事板"
                % (
                    ", ".join("S%02d" % number for number in selected),
                    len(storyboard_shots) - len(selected),
                ),
                stage_id,
                runner="codex",
                meta={
                    "selected_shots": list(selected),
                    "repair_notes": notes,
                    "invalidated_stages": list(downstream),
                    "repair_plan_id": plan["id"],
                    "annotation_watermark": plan["annotation_watermark"],
                },
            )

            self.statuses.save(status)
            return status

    def _generate_videos(
        self,
        status: Dict[str, Any],
        episode_dir: Path,
        authorized: bool,
        shot_numbers: Optional[Sequence[int]] = None,
        persist_status: bool = True,
    ) -> List[str]:
        stage_id = "video_generation"
        outputs = []
        revision = status["stages"][stage_id]["revision"]
        shot_count = resolved_shot_count(status["creative_brief"])
        durations = status["creative_brief"]["shot_plan"]["durations_sec"]
        aspect_ratio = status["creative_brief"]["aspect_ratio"]
        shot_aspect_ratios = status["creative_brief"].get("video_shot_aspect_ratios") or {}
        planned_asset_names = {
            str(item.get("id")): str(item.get("name"))
            for item in _load_asset_plan(episode_dir).get("assets", [])
            if item.get("id") and item.get("name")
        }
        selected_shots = list(shot_numbers or range(1, shot_count + 1))
        if not selected_shots or any(
            isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= shot_count
            for number in selected_shots
        ):
            raise ValueError("video generation shot selection is invalid")
        for shot_number in sorted(set(selected_shots)):
            aspect_ratio = shot_aspect_ratios.get(
                "shot%02d" % shot_number,
                status["creative_brief"]["aspect_ratio"],
            )
            task_id = "shot%02d" % shot_number
            ensure_task(status, stage_id, task_id)
            prompt_name = "shot%02d_prompt_video.txt" % shot_number
            binding_name = "shot%02d_video_references.json" % shot_number
            target_name = "shot%02d_video_jimeng_v%d.mp4" % (
                shot_number,
                revision,
            )
            set_task_state(status, stage_id, task_id, "running")
            if persist_status:
                self.statuses.save(status)
            video_prompt = (episode_dir / prompt_name).read_text(encoding="utf-8")
            video_prompt = _simplify_video_prompt_for_binding(video_prompt)
            binding = json.loads(
                (episode_dir / binding_name).read_text(encoding="utf-8")
            )
            prompt_hash = hashlib.sha256(
                (episode_dir / prompt_name).read_bytes()
            ).hexdigest()
            if binding.get("prompt_sha256") != prompt_hash:
                raise ValueError(
                    "video binding no longer matches locked prompt for unit %02d"
                    % shot_number
                )
            reference_paths = tuple(
                Path(item["path"]) for item in binding.get("references", [])
            )
            binding_duration = binding.get("duration_sec")
            if binding_duration is None:
                video_duration = float(durations[shot_number - 1])
            else:
                if isinstance(binding_duration, bool) or not isinstance(binding_duration, (int, float)):
                    raise ValueError("video binding duration_sec is invalid for unit %02d" % shot_number)
                if binding_duration < 3 or binding_duration > 15:
                    raise ValueError("video binding duration_sec must be between 3 and 15 seconds")
                video_duration = float(binding_duration)
            prompt_reference_lines = []
            for index, item in enumerate(binding.get("references", []), 1):
                usage = str(item.get("prompt_usage") or "保持该角色现有身份与外观一致")
                subject_prefix = _video_reference_subject_prefix(
                    item, planned_asset_names
                )
                prompt_reference_lines.append(
                    "图片%d\n%s%s；不得用于其他角色。"
                    % (index, subject_prefix, usage.removeprefix("锁定"))
                )
            if prompt_reference_lines:
                reference_block = "【参考图对应关系】\n" + "\n".join(prompt_reference_lines)
                video_prompt = reference_block + "\n\n" + video_prompt.lstrip()
            video_prompt = (
                self._video_submission_label(
                    status["project_id"], status["episode_id"], shot_number
                )
                + "\n\n"
                + video_prompt.lstrip()
            )
            started_at = time.monotonic()
            try:
                result = self.video.run(
                    VideoRequest(
                        video_prompt,
                        episode_dir / target_name,
                        video_duration,
                        aspect_ratio,
                        shot_number=shot_number,
                        reference_paths=reference_paths,
                    ),
                    status,
                    episode_dir,
                    authorized=authorized,
                )
            except Exception as error:
                self.usage.record(
                    status["project_id"], status["episode_id"],
                    kind="provider_call", status="failed", stage_id=stage_id,
                    task_id=task_id, execution_id=status["tasks"]["%s:%s" % (stage_id, task_id)].get("execution_id"),
                    attempt=status["tasks"]["%s:%s" % (stage_id, task_id)].get("attempt"),
                    provider=status["providers"]["video"],
                    estimated_input_tokens=estimate_tokens(video_prompt),
                    video_seconds=video_duration,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error=str(error),
                )
                raise
            self.usage.record(
                status["project_id"], status["episode_id"],
                kind="provider_call", status="success", stage_id=stage_id,
                task_id=task_id, execution_id=status["tasks"]["%s:%s" % (stage_id, task_id)].get("execution_id"),
                attempt=status["tasks"]["%s:%s" % (stage_id, task_id)].get("attempt"),
                provider=result.get("provider", status["providers"]["video"]),
                estimated_input_tokens=estimate_tokens(video_prompt),
                video_seconds=float(durations[shot_number - 1]),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            register_asset(
                status,
                episode_dir,
                _stable_asset_id(stage_id, target_name),
                stage_id,
                "video",
                "shot_video",
                "镜头 %02d" % shot_number,
                target_name,
                metadata={"provider": result["provider"], **result["metadata"]},
                task_id=task_id,
            )
            set_task_state(status, stage_id, task_id, "reviewing")
            set_task_state(
                status,
                stage_id,
                task_id,
                "passed",
                review_reason="真实视频文件、时长和画面元数据合同通过",
            )
            consume_video_shot_approval(status, shot_number)
            status["metrics"]["video_generations"] += 1
            outputs.append(target_name)
            if persist_status:
                self.statuses.save(status)
        return outputs

    def _execute_stage(
        self, status: Dict[str, Any], stage_id: str, episode_dir: Path, authorized: bool
    ) -> List[str]:
        if stage_id in CODEX_TEXT_STAGES:
            reused_outputs: List[str] = []
            if stage_id in {"character_design", "visual_design"}:
                reused_outputs = self._materialize_confirmed_reuse_assets(
                    status, stage_id, episode_dir
                )
                allowed_types = (
                    {"character"}
                    if stage_id == "character_design"
                    else {"scene", "prop"}
                )
                generation_items = [
                    item
                    for item in status["asset_reuse"]["items"]
                    if item["type"] in allowed_types and item["decision"] == "generate"
                ]
                if not generation_items:
                    return reused_outputs
            text_checkpoint = (
                self._reviewed_design_text_checkpoint(status, stage_id, episode_dir)
                if stage_id in {"character_design", "visual_design"}
                else None
            )
            if text_checkpoint is not None:
                outputs = text_checkpoint
                self._emit(
                    status,
                    "progress",
                    "文字设定已通过自检与合同校验，本次跳过文字生成",
                    stage_id,
                    runner="workbench",
                )
            else:
                outputs = self._run_codex_stage(
                    status, stage_id, episode_dir, authorized
                )
            if stage_id in {"character_design", "visual_design"}:
                # The text/review checkpoint must survive an image-provider failure.
                self.statuses.save(status)
                outputs = self._render_design_assets(
                    status,
                    stage_id,
                    episode_dir,
                    outputs,
                    authorized,
                    recover_existing=text_checkpoint is not None,
                )
            return sorted(set(outputs) | set(reused_outputs))
        if stage_id == "storyboard_binding":
            return self._bind_references(status, episode_dir, for_video=False)
        if stage_id == "storyboard_generation":
            return self._render_storyboards(status, episode_dir, authorized)
        if stage_id == "video_binding":
            return self._bind_references(status, episode_dir, for_video=True)
        if stage_id == "video_generation":
            return self._generate_videos(status, episode_dir, authorized)
        raise RuntimeError("no runner for stage: %s" % stage_id)

    def advance(
        self,
        project_id: str,
        episode_id: str,
        authorized: bool = False,
        should_pause: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        if not authorized:
            raise ProviderCallNotAuthorized(
                "advance requires an explicit current user action"
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["run"].get("state") != "done":
                restore_intermediate_storage(status, episode_dir)
            self._configure_model(status)
            if status["run"].get("state") == "waiting_agent":
                # Re-running advance must never convert a live visible handoff
                # into a block. The coordinator should read next-action instead.
                return status
            self._ensure_asset_reuse_proposal(status, episode_dir)
            current = status["run"].get("current_stage")
            if (
                status["run"].get("state") not in {"waiting_agent", "waiting_user_decision"}
                and current
                and status["stages"][current]["state"] in {
                "running",
                "reviewing",
                "repairing",
                "blocked",
                }
            ):
                previous_state = status["stages"][current]["state"]
                if not self.resume_incomplete_stage_from_checkpoint(
                    status, current, episode_dir
                ):
                    reason = (
                        "恢复上次中断的阶段"
                        if previous_state == "running"
                        else "重试阻塞阶段"
                    )
                    invalidate_from(status, current)
                    self._emit(status, "decision", reason, current)
                self.statuses.save(status)

            while True:
                if should_pause is not None and should_pause():
                    pause_run(status)
                    paused_stage = status["run"]["current_stage"]
                    self._emit(
                        status,
                        "decision",
                        (
                            "已在节点边界暂停；继续时将从 %s 开始"
                            % status["stages"][paused_stage]["label"]
                            if paused_stage
                            else "全部节点已完成"
                        ),
                        paused_stage,
                    )
                    self.statuses.save(status)
                    return status
                decision = flow_next(status, episode_dir)
                if decision.action == "done":
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_style":
                    wait_for_style(status)
                    self._emit(
                        status,
                        "decision",
                        "开始制作前需要用户选择视觉风格；剧本未提供风格，流程不会自动代入默认值",
                        "story_design",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_aspect_ratio":
                    wait_for_aspect_ratio(status)
                    self._emit(
                        status,
                        "decision",
                        "开始制作前需要用户选择画幅比例；剧本未提供画幅，流程不会自动代入默认值",
                        "story_design",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_video_model":
                    wait_for_video_model(status)
                    self._emit(
                        status,
                        "decision",
                        "开始制作前需要用户选择视频模型：fast 或 mini",
                        "story_design",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_video_session":
                    wait_for_video_session(status)
                    self._emit(
                        status,
                        "decision",
                        "进入视频阶段前需要配置本集 sessionId；本集所有镜头将复用该值",
                        "video_generation",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_confirmation":
                    wait_for_video_confirmation(status)
                    self._emit(
                        status,
                        "decision",
                        "最终视频提示词已就绪，等待确认当前 revision",
                        "video_generation",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_user_decision":
                    wait_for_storyboard_decision(status)
                    self._emit(
                        status,
                        "decision",
                        "开始制作前需要用户确认是否生成故事板",
                        "story_design",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "wait_asset_reuse":
                    wait_for_asset_reuse_confirmation(status)
                    self._emit(
                        status,
                        "decision",
                        "资产复用清单已就绪，等待用户确认后才会进入定妆阶段",
                        "asset_planning",
                    )
                    self.statuses.save(status)
                    return status
                if decision.action == "skip":
                    skip_stage(status, decision.stage_id, decision.reason)
                    self._emit(status, "decision", decision.reason, decision.stage_id)
                    self.statuses.save(status)
                    continue
                if decision.action != "run" or decision.stage_id is None:
                    error = decision.reason or "flow cannot advance"
                    if decision.stage_id:
                        block_stage(status, decision.stage_id, error)
                    self.statuses.save(status)
                    return status

                stage_id = begin_stage(status, episode_dir)
                self._emit(status, "progress", "开始：%s" % status["stages"][stage_id]["label"], stage_id)
                self.statuses.save(status)
                try:
                    outputs = self._execute_stage(status, stage_id, episode_dir, authorized)
                    if status["stages"][stage_id]["review"]["state"] != "passed":
                        pass_review(status, stage_id, "阶段输出合同通过")
                    complete_stage(status, stage_id, outputs, episode_dir)
                    self._emit(
                        status,
                        "done",
                        "完成：%s" % status["stages"][stage_id]["label"],
                        stage_id,
                    )
                    self.statuses.save(status)
                    if should_pause is not None and should_pause():
                        pause_run(status)
                        paused_stage = status["run"]["current_stage"]
                        self._emit(
                            status,
                            "decision",
                            (
                                "当前节点已完成，自动流程已暂停；继续时将从 %s 开始"
                                % status["stages"][paused_stage]["label"]
                                if paused_stage
                                else "当前节点已完成，全部流程结束"
                            ),
                            paused_stage,
                        )
                        self.statuses.save(status)
                        return status
                except AgentActionPending:
                    # The request itself is in status.json.  Release the episode
                    # lock before the native child runs, otherwise its result
                    # could never be submitted by the visible Codex coordinator.
                    self.statuses.save(status)
                    return status
                except Exception as error:
                    block_stage(status, stage_id, str(error))
                    self._emit(
                        status,
                        "block",
                        "阶段阻塞：%s" % error,
                        stage_id,
                    )
                    self.statuses.save(status)
                    return status

    def confirm_and_advance(
        self,
        project_id: str,
        episode_id: str,
        authorized: bool = False,
        should_pause: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        if not authorized:
            raise ProviderCallNotAuthorized(
                "video confirmation requires an explicit current user action"
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            fingerprint = approve_video(status, episode_dir)
            self._emit(
                status,
                "decision",
                "已确认视频生成 revision：%s" % fingerprint[:12],
                "video_generation",
            )
            self.statuses.save(status)
        return self.advance(
            project_id,
            episode_id,
            authorized=True,
            should_pause=should_pause,
        )

    def confirm_video_shot(
        self,
        project_id: str,
        episode_id: str,
        shot_number: int,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Approve and generate one video shot.

        This remains available for targeted retries. Use
        ``confirm_video_shots_batch`` when several approved units should be
        submitted concurrently.
        """
        if not authorized:
            raise ProviderCallNotAuthorized(
                "video shot confirmation requires an explicit current user action"
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["creative_brief"].get("video_model", "fast") == "pending":
                raise RuntimeError("video model must be selected before video approval")
            fingerprint = approve_video_shot(status, episode_dir, shot_number)
            self._emit(
                status,
                "decision",
                "已批准镜头 %02d 视频生成（模型：%s）"
                % (shot_number, status["creative_brief"].get("video_model", "fast")),
                "video_generation",
                meta={
                    "shot_number": shot_number,
                    "revision_fingerprint": fingerprint,
                    "video_model": status["creative_brief"].get("video_model", "fast"),
                },
            )
            begin_stage(status, episode_dir)
            self.statuses.save(status)
            try:
                outputs = self._generate_videos(
                    status, episode_dir, authorized, shot_numbers=[shot_number]
                )
                shot_count = resolved_shot_count(status["creative_brief"])
                all_passed = all(
                    status["tasks"].get("video_generation:shot%02d" % number, {}).get("state")
                    == "passed"
                    for number in range(1, shot_count + 1)
                )
                if all_passed:
                    pass_review(status, "video_generation", "全部镜头均通过视频文件与元数据合同")
                    complete_stage(
                        status,
                        "video_generation",
                        [
                            asset["path"]
                            for asset in status["assets"].values()
                            if asset.get("stage") == "video_generation"
                        ],
                        episode_dir,
                    )
                    self._emit(
                        status,
                        "done",
                        "完成：视频生成",
                        "video_generation",
                    )
                else:
                    status["stages"]["video_generation"]["state"] = "waiting_confirmation"
                    status["run"].update(
                        {
                            "state": "waiting_confirmation",
                            "current_stage": "video_generation",
                            "last_error": None,
                            "waiting_for": "video_shot_approval",
                        }
                    )
                    self._emit(
                        status,
                        "decision",
                        "镜头 %02d 已完成，可继续单镜确认或使用批量确认并发提交其余镜头"
                        % shot_number,
                        "video_generation",
                        meta={"completed_shot": shot_number, "outputs": outputs},
                    )
                self.statuses.save(status)
                return status
            except Exception as error:
                require_video_shot_approval(status, shot_number)
                block_stage(status, "video_generation", str(error))
                self._emit(
                    status,
                    "block",
                    "镜头 %02d 视频生成阻塞：%s" % (shot_number, error),
                    "video_generation",
                    meta={"shot_number": shot_number},
                )
                self.statuses.save(status)
                return status

    def confirm_video_supplement(
        self,
        project_id: str,
        episode_id: str,
        unit_id: str,
        duration_sec: float,
        fingerprint: str,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Generate one explicitly added alphanumeric bridge unit, such as U08B."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "supplemental video confirmation requires an explicit current user action"
            )
        unit_id = unit_id.strip().lower()
        if not re.fullmatch(r"[0-9]{2}[a-z]", unit_id):
            raise ValueError("supplemental video unit id must look like 08b")
        episode_dir = self.episode_dir(project_id, episode_id)
        prompt_name = "shot%s_prompt_video.txt" % unit_id
        binding_name = "shot%s_video_references.json" % unit_id
        target_name = "shot%s_video_jimeng_v%d.mp4" % (
            unit_id, 2
        )
        prompt_path = episode_dir / prompt_name
        binding_path = episode_dir / binding_name
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            for stage_id in STAGE_IDS[: STAGE_IDS.index("video_generation")]:
                if status["stages"][stage_id]["state"] not in {"done", "skipped", "preserved"}:
                    raise RuntimeError("upstream stage is incomplete: %s" % stage_id)
            current_fingerprint = compute_video_supplement_fingerprint(
                status, episode_dir, unit_id, prompt_path, binding_path, duration_sec
            )
            if current_fingerprint != fingerprint:
                raise ProviderCallNotAuthorized(
                    "supplemental video approval does not match the current revision fingerprint"
                )
            approve_video_supplement(
                status, episode_dir, unit_id, prompt_path, binding_path,
                duration_sec, fingerprint,
            )
            task_id = "shot%s" % unit_id
            task = ensure_task(status, "video_generation", task_id, unit="supplemental_video_unit")
            active_assets = [
                (asset_id, asset)
                for asset_id, asset in status.get("assets", {}).items()
                if asset.get("stage") == "video_generation"
                and asset.get("task_id") == task_id
                and asset.get("status") == "active"
            ]
            for asset_id, asset in active_assets:
                source = resolve_asset_path(episode_dir, asset["path"])
                if source.is_file():
                    source.unlink()
                asset["status"] = "deleted"
                asset["deleted_at"] = utc_now()
                task["asset_ids"] = [
                    value for value in task.get("asset_ids", []) if value != asset_id
                ]
            if active_assets:
                status["stages"]["video_generation"]["outputs"] = [
                    value
                    for value in status["stages"]["video_generation"].get("outputs", [])
                    if value != target_name
                ]
                status["metrics"]["video_generations"] = max(
                    0,
                    int(status["metrics"].get("video_generations", 0))
                    - len(active_assets),
                )
            set_task_state(status, "video_generation", task_id, "running")
            status["stages"]["video_generation"].update({"state": "running", "error": None})
            status["run"].update({
                "state": "running",
                "current_stage": "video_generation",
                "last_error": None,
                "waiting_for": None,
            })
            self.statuses.save(status)
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            reference_paths = tuple(Path(item["path"]) for item in binding.get("references", []))
            planned_asset_names = {
                str(item.get("id")): str(item.get("name"))
                for item in _load_asset_plan(episode_dir).get("assets", [])
                if item.get("id") and item.get("name")
            }
            reference_lines = []
            for index, item in enumerate(binding.get("references", []), 1):
                usage = str(item.get("prompt_usage") or "保持该参考资产的身份与外观一致")
                reference_lines.append(
                    "图片%d\n%s%s；不得用于其他角色或资产。"
                    % (index, _video_reference_subject_prefix(item, planned_asset_names), usage)
                )
            video_prompt = _simplify_video_prompt_for_binding(
                prompt_path.read_text(encoding="utf-8")
            )
            if reference_lines:
                video_prompt = "【参考图对应关系】\n" + "\n".join(reference_lines) + "\n\n" + video_prompt.lstrip()
            video_prompt = (
                self._video_submission_label(project_id, episode_id, unit_id)
                + "\n\n"
                + video_prompt.lstrip()
            )
            started_at = time.monotonic()
            try:
                result = self.video.run(
                    VideoRequest(
                        video_prompt,
                        episode_dir / target_name,
                        float(duration_sec),
                        status["creative_brief"]["aspect_ratio"],
                        shot_number=8,
                        reference_paths=reference_paths,
                        supplement_id=unit_id,
                        supplement_prompt_path=prompt_path,
                        supplement_references_path=binding_path,
                    ),
                    status,
                    episode_dir,
                    authorized=authorized,
                )
            except Exception as error:
                self.usage.record(
                    project_id, episode_id, kind="provider_call", status="failed",
                    stage_id="video_generation", task_id=task_id,
                    execution_id=task.get("execution_id"), attempt=task.get("attempt"),
                    provider=status["providers"]["video"],
                    estimated_input_tokens=estimate_tokens(video_prompt),
                    video_seconds=float(duration_sec),
                    duration_ms=int((time.monotonic() - started_at) * 1000), error=str(error),
                )
                set_task_state(status, "video_generation", task_id, "failed", error=str(error))
                status["stages"]["video_generation"].update({"state": "blocked", "error": str(error)})
                status["run"].update({"state": "blocked", "last_error": str(error), "waiting_for": None})
                self.statuses.save(status)
                return status
            self.usage.record(
                project_id, episode_id, kind="provider_call", status="success",
                stage_id="video_generation", task_id=task_id,
                execution_id=task.get("execution_id"), attempt=task.get("attempt"),
                provider=result.get("provider", status["providers"]["video"]),
                estimated_input_tokens=estimate_tokens(video_prompt),
                video_seconds=float(duration_sec),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            asset_id = _stable_asset_id("video_generation", target_name)
            if asset_id in status["assets"] and status["assets"][asset_id].get("status") == "deleted":
                status["assets"].pop(asset_id, None)
            register_asset(
                status, episode_dir, asset_id, "video_generation", "video", "shot_video",
                "补充单元 %s" % unit_id.upper(), target_name,
                metadata={"provider": result["provider"], **result["metadata"]}, task_id=task_id,
            )
            set_task_state(
                status, "video_generation", task_id, "passed",
                review_reason="真实视频文件、时长和画面元数据合同通过",
            )
            consume_video_supplement_approval(status, unit_id)
            status["metrics"]["video_generations"] += 1
            stage = status["stages"]["video_generation"]
            stage["state"] = "waiting_confirmation"
            stage["outputs"] = list(dict.fromkeys(stage.get("outputs", []) + [target_name]))
            status["run"].update({
                "state": "waiting_confirmation",
                "current_stage": "video_generation",
                "last_error": None,
                "waiting_for": "video_shot_approval",
            })
            self.statuses.save(status)
            self.events.append(
                project_id, episode_id, "done", "补充视频单元 %s 生成完成" % unit_id.upper(),
                stage="video_generation", runner="codex",
                meta={"unit_id": unit_id, "path": target_name},
            )
            return status

    def confirm_video_shots_batch(
        self,
        project_id: str,
        episode_id: str,
        shot_numbers: Sequence[int],
        max_concurrency: int = MAX_VIDEO_CONCURRENCY,
        authorized: bool = False,
    ) -> Dict[str, Any]:
        """Approve and generate several video units concurrently."""
        if not authorized:
            raise ProviderCallNotAuthorized(
                "video batch confirmation requires an explicit current user action"
            )
        selected = sorted(set(int(number) for number in shot_numbers))
        if not selected:
            raise ValueError("at least one video shot is required")
        if not 1 <= max_concurrency <= MAX_VIDEO_CONCURRENCY:
            raise ValueError(
                "max_concurrency must be between 1 and %d"
                % MAX_VIDEO_CONCURRENCY
            )
        episode_dir = self.episode_dir(project_id, episode_id)
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            shot_count = resolved_shot_count(status["creative_brief"])
            if any(number < 1 or number > shot_count for number in selected):
                raise ValueError("video shot selection is invalid")
            if any(
                status["tasks"].get("video_generation:shot%02d" % number, {}).get("state")
                == "passed"
                for number in selected
            ):
                raise RuntimeError("batch contains an already generated video shot")
            for number in selected:
                approve_video_shot(status, episode_dir, number)
            base_status = deepcopy(status)
            self.statuses.save(status)

        def run_one(number: int):
            local_status = deepcopy(base_status)
            try:
                outputs = self._generate_videos(
                    local_status,
                    episode_dir,
                    authorized,
                    shot_numbers=[number],
                    persist_status=False,
                )
                return number, local_status, outputs, None
            except Exception as error:
                return number, local_status, [], error

        with ThreadPoolExecutor(max_workers=min(max_concurrency, len(selected))) as pool:
            results = list(pool.map(run_one, selected))

        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            failures = []
            for number, local_status, outputs, error in results:
                task_key = "video_generation:shot%02d" % number
                if error is not None:
                    failures.append((number, error))
                    require_video_shot_approval(status, number)
                    continue
                for asset_id, asset in local_status.get("assets", {}).items():
                    if asset.get("stage") == "video_generation" and asset.get("task_id") == "shot%02d" % number:
                        status["assets"][asset_id] = asset
                status["tasks"][task_key] = local_status["tasks"][task_key]
                status["confirmations"]["video_generation"]["shots"]["shot%02d" % number] = local_status["confirmations"]["video_generation"]["shots"]["shot%02d" % number]
                status["metrics"]["video_generations"] = int(status["metrics"].get("video_generations", 0)) + 1
            all_passed = all(
                status["tasks"].get("video_generation:shot%02d" % number, {}).get("state") == "passed"
                for number in range(1, shot_count + 1)
            )
            if all_passed:
                pass_review(status, "video_generation", "全部镜头均通过视频文件与元数据合同")
                complete_stage(
                    status,
                    "video_generation",
                    [asset["path"] for asset in status["assets"].values() if asset.get("stage") == "video_generation"],
                    episode_dir,
                )
            else:
                status["stages"]["video_generation"]["state"] = "blocked" if failures else "waiting_confirmation"
                status["run"].update({
                    "state": "blocked" if failures else "waiting_confirmation",
                    "current_stage": "video_generation",
                    "last_error": str(failures[0][1]) if failures else None,
                    "waiting_for": None if failures else "video_shot_approval",
                })
            self.statuses.save(status)
            return status

    def pause(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        """Pause an episode, including a native Agent handoff that has no result."""
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            if status["run"].get("state") == "waiting_agent":
                stage_id = cancel_waiting_agent_and_pause(status)
                content = "已取消未提交的原生子 Agent 任务，并在阶段边界暂停"
            else:
                pause_run(status)
                stage_id = status["run"]["current_stage"]
                content = "已在阶段边界暂停"
            self._emit(status, "decision", content, stage_id, runner="codex")
            self.statuses.save(status)
            return status

    def recover_interrupted_and_pause(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        """Close a process-interrupted run without invoking any provider."""
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            stage_id = recover_interrupted_run_and_pause(status)
            self._emit(
                status,
                "decision",
                "已恢复中断的 %s，并在阶段边界暂停" % status["stages"][stage_id]["label"],
                stage_id,
                runner="codex",
            )
            self.statuses.save(status)
            return status
