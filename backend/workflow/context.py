import json
from fnmatch import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from backend.core.assets import resolve_asset_path
from backend.core.flow import STAGE_BY_ID, STAGE_IDS
from backend.core.status import is_dialogue_direct
from .asset_reuse import reusable_asset_inventory


TEXT_INPUTS: Dict[str, Tuple[Tuple[str, bool], ...]] = {
    "story_design": (),
    "asset_planning": (
        ("script.md", True),
        ("storyboard.md", False),
        ("shot*_prompt_video.txt", True),
        ("shot*_prompt_storyboard.txt", False),
    ),
    "character_design": (
        ("asset_plan.json", True),
        ("script.md", True),
        ("directing.md", True),
    ),
    "visual_design": (
        ("asset_plan.json", True),
        ("script.md", True),
        ("storyboard.md", False),
        ("char_*_sheet.md", False),
    ),
    "storyboard_binding": (
        ("asset_plan.json", True),
        ("storyboard.md", True),
        ("shot*_prompt_storyboard.txt", False),
    ),
    "storyboard_generation": (
        ("storyboard.md", True),
        ("shot*_prompt_storyboard.txt", True),
        ("shot*_storyboard_references.json", True),
        ("char_*_sheet.md", True),
        ("scene_*_sheet.md", True),
        ("prop_*_sheet.md", False),
    ),
    "video_binding": (
        ("asset_plan.json", True),
        ("shot*_prompt_video.txt", True),
    ),
    "video_generation": (
        ("shot*_prompt_video.txt", True),
        ("shot*_video_references.json", True),
    ),
    "edit_post": (
        ("script.md", True),
        ("storyboard.md", False),
        ("shot*_prompt_video.txt", True),
    ),
}

IMAGE_INPUTS: Dict[str, Tuple[Tuple[str, bool], ...]] = {
    "visual_design": (("char_*_sheet.png", False),),
    "storyboard_binding": (
        ("char_*_sheet.png", False),
        ("scene_*_sheet.png", False),
        ("prop_*_sheet.png", False),
    ),
    "storyboard_generation": (
        ("char_*_sheet.png", True),
        ("scene_*_sheet.png", True),
    ),
    "video_binding": (
        ("shot*_storyboard.png", False),
        ("char_*_sheet.png", False),
        ("scene_*_sheet.png", False),
        ("prop_*_sheet.png", False),
    ),
}

# Once every earlier node has a handoff, only keep the current node's canonical
# text inputs.  The omitted upstream files remain on disk and are still the
# source of truth; their decisions arrive through the short handoff section.
COMPACT_TEXT_INPUTS: Dict[str, Tuple[Tuple[str, bool], ...]] = {
    "story_design": (),
    "asset_planning": TEXT_INPUTS["asset_planning"],
    "character_design": TEXT_INPUTS["character_design"],
    "visual_design": TEXT_INPUTS["visual_design"],
    "storyboard_binding": TEXT_INPUTS["storyboard_binding"],
    "storyboard_generation": (
        ("storyboard.md", True),
        ("shot*_prompt_storyboard.txt", True),
        ("shot*_storyboard_references.json", True),
    ),
    "video_binding": TEXT_INPUTS["video_binding"],
    "video_generation": TEXT_INPUTS["video_generation"],
    "edit_post": TEXT_INPUTS["edit_post"],
}


@dataclass(frozen=True)
class StageContext:
    prompt: str
    image_paths: Tuple[Path, ...]
    loaded_text_paths: Tuple[Path, ...]
    loaded_skill_paths: Tuple[Path, ...]


def _slim_status(status: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": status["schema_version"],
        "project_id": status["project_id"],
        "episode_id": status["episode_id"],
        "run": dict(status["run"]),
        "providers": dict(status["providers"]),
        "stages": {
            stage_id: {
                "state": stage["state"],
                "revision": stage["revision"],
                "inputs": list(stage["inputs"]),
                "outputs": list(stage["outputs"]),
            }
            for stage_id, stage in status["stages"].items()
        },
    }


class ContextBuilder:
    def __init__(self, project_root: Path, workspace_root: Path = None):
        self.project_root = Path(project_root).resolve()
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )
        self.skills_root = self.project_root / "backend" / "skills" / "stages"

    def _load_skills(self, stage_id: str) -> Tuple[List[str], List[Path]]:
        documents = []
        paths = []
        for skill_name in STAGE_BY_ID[stage_id].skills:
            path = self.skills_root / skill_name / "SKILL.md"
            if not path.is_file():
                raise FileNotFoundError("stage skill is missing: %s" % skill_name)
            documents.append(path.read_text(encoding="utf-8"))
            paths.append(path)
        return documents, paths

    @staticmethod
    def _handoffs_ready(status: Mapping[str, Any], stage_id: str) -> bool:
        index = STAGE_IDS.index(stage_id)
        if index == 0:
            return False
        for upstream_id in STAGE_IDS[:index]:
            upstream = status["stages"][upstream_id]
            if upstream.get("state") not in {"done", "skipped", "preserved"} or not upstream.get("handoff"):
                return False
        return True

    @staticmethod
    def _match_files(
        episode_dir: Path,
        specifications: Sequence[Tuple[str, bool]],
        status: Mapping[str, Any],
        allowed_kinds: Sequence[str],
    ) -> List[Path]:
        matched = []
        seen = set()
        active_paths = {
            asset["path"]
            for asset in status["assets"].values()
            if asset.get("kind") in allowed_kinds
            and asset.get("stage") in status["stages"]
            and asset.get("revision")
            == status["stages"][asset["stage"]]["revision"]
        }
        for pattern, required in specifications:
            candidates = sorted(
                resolve_asset_path(episode_dir, name)
                for name in active_paths
                if fnmatch(name, pattern)
            )
            candidates = [
                path
                for path in candidates
                if path.is_file() and not path.is_symlink() and path.name not in seen
            ]
            if required and not candidates:
                raise FileNotFoundError("required stage input is missing: %s" % pattern)
            for path in candidates:
                resolve_asset_path(episode_dir, path.name)
                matched.append(path)
                seen.add(path.name)
        return matched

    def build(
        self, stage_id: str, status: Mapping[str, Any], episode_dir: Path
    ) -> StageContext:
        if stage_id not in STAGE_BY_ID:
            raise ValueError("unknown stage: %s" % stage_id)
        episode_dir = Path(episode_dir).resolve()
        skills, skill_paths = self._load_skills(stage_id)
        use_compact_context = self._handoffs_ready(status, stage_id)
        direct_mode = is_dialogue_direct(status["creative_brief"])
        text_inputs = (
            COMPACT_TEXT_INPUTS.get(stage_id, TEXT_INPUTS.get(stage_id, ()))
            if use_compact_context
            else TEXT_INPUTS.get(stage_id, ())
        )
        image_inputs = IMAGE_INPUTS.get(stage_id, ())
        text_paths = self._match_files(
            episode_dir,
            text_inputs,
            status,
            ("document", "prompt"),
        )
        image_paths = self._match_files(
            episode_dir,
            image_inputs,
            status,
            ("image",),
        )

        sections = [
            "你正在执行 AI 漫剧工作台的单一阶段：%s。" % stage_id,
            "这是一个新的独立节点上下文，不得依赖主会话历史。不得检查目录或读取未在下方提供的文件。不得修改 status.json，不得推进阶段，不得调用付费 provider。",
            "只返回符合输出 JSON Schema 的对象。请在 files 中返回每个文字产物的 path 和完整 content；状态机负责安全写入、计算 size/SHA-256、校验并原子落盘。不要依赖共享文件系统，也不要返回 output_dir 中的虚假文件清单。",
            (
                (
                    "本阶段必须返回已解析的 shot_plan；对话直通模式返回 storyboard_plan=null，并直接返回每个视频生成单元的最终 Prompt。"
                    if direct_mode
                    else "本阶段必须同时返回已解析的 shot_plan 和 storyboard_plan，并直接返回每个视频生成单元不可变的最终视频 Prompt 与故事板执行 Prompt。"
                )
                if stage_id == "story_design"
                else "本阶段不得重新决定视频单元、分镜或 Prompt；shot_plan 与 storyboard_plan 必须返回 null。"
            ),
            "\n## 固定创意简报\n```json\n%s\n```"
            % json.dumps(status["creative_brief"], ensure_ascii=False, indent=2),
            "\n## 精简状态快照\n```json\n%s\n```"
            % json.dumps(_slim_status(status), ensure_ascii=False, indent=2),
        ]
        if stage_id == "asset_planning" and self.workspace_root is not None:
            inventory = reusable_asset_inventory(
                self.workspace_root,
                status["project_id"],
                status["episode_id"],
            )
            sections.append(
                "\n## 已验证可复用资产目录（用稳定 ID 命中）\n"
                "资产规划必须优先使用下列已有 ID；不得为同一角色、场景或道具换 ID 规避复用。"
                "本节只帮助规划，状态机将在下一步生成用户确认清单。\n```json\n%s\n```"
                % json.dumps(inventory, ensure_ascii=False, indent=2)
            )
        if stage_id in {"character_design", "visual_design"}:
            sections.append(
                "\n## 用户已确认的资产复用决策（状态机权威）\n"
                "decision=reuse 的资产将由状态机原字节复制已有定妆三件套，本节点不得重新设计或返回它们的文件。"
                "只为 decision=generate 且类型属于当前阶段的资产返回文字设定和 Prompt。\n```json\n%s\n```"
                % json.dumps(status.get("asset_reuse", {}), ensure_ascii=False, indent=2)
            )
        provided_script = status["creative_brief"].get("provided_script")
        if stage_id == "story_design" and provided_script is not None:
            sections.append(
                "\n## 用户已提供剧本：不可改写锁（流程硬规则）\n"
                "这是本集的原始锁定输入，已保存为 inputs/original_script.md。script.md 必须逐字一致。只允许按视频 Provider 的生成单元机械拆分并提取资产名称；一个生成单元内部可以保留多个短镜头。"
                "不得评判、润色、扩写、删减、改序、重新分配时长或补充表演。只有无法拆分、缺少单元时长、单元为空或违反 Provider 硬限制时才能阻塞。\n"
                "若用户要求优化镜头提示词格式，所有镜头内容必须逐字保留，并统一映射为‘视频生成单元 Uxx + Uxx-yy 内部分镜’结构化执行稿；不得转换成连续叙述式长段落，不得删减、同义改写、合并或补写任何镜头内容。每条内部分镜必须显式保留编号、单元内时间、景别/运镜、画面、台词/字幕和动作特效/连续性；每个单元末尾必须保留字幕、角色与站位锁、动作因果锁和负面约束。\n"
                "```text\n%s\n```" % provided_script
            )
        if use_compact_context:
            handoffs = []
            for upstream_id in STAGE_IDS[: STAGE_IDS.index(stage_id)]:
                upstream = status["stages"][upstream_id]
                handoff = upstream["handoff"]
                handoffs.append(
                    "### %s\n%s\n产物索引：%s"
                    % (
                        upstream_id,
                        handoff["summary"],
                        ", ".join(handoff["outputs"]) or "无",
                    )
                )
            sections.append(
                "\n## 上游节点压缩交接\n"
                "以下摘要是上游结论；未列出的上游全文不属于当前上下文。\n\n%s"
                % "\n\n".join(handoffs)
            )
        for index, skill in enumerate(skills, 1):
            sections.append("\n## 当前阶段 Skill %d\n%s" % (index, skill))
        if text_paths:
            sections.append("\n## 必要上游文字产物")
            for path in text_paths:
                sections.append(
                    "\n### %s\n```text\n%s\n```"
                    % (path.name, path.read_text(encoding="utf-8"))
                )
        if image_paths:
            sections.append(
                "\n## 已附加的必要图片（本地绝对路径）\n%s"
                % "\n".join("- %s" % path.resolve() for path in image_paths)
            )
        if stage_id == "edit_post":
            video_metadata = [
                {
                    "path": asset["path"],
                    "size": asset["size"],
                    "metadata": asset.get("metadata", {}),
                }
                for asset in status["assets"].values()
                if asset["stage"] == "video_generation" and asset["kind"] == "video"
            ]
            if not video_metadata:
                raise FileNotFoundError("verified video metadata is required for edit_post")
            sections.append(
                "\n## 已验证视频元数据\n```json\n%s\n```"
                % json.dumps(video_metadata, ensure_ascii=False, indent=2)
            )
        sections.append(
            "\n## 统一输出合同\n最终只返回一个 JSON 对象，顶层字段必须是 files、review、summary、shot_plan、storyboard_plan；不得返回 Markdown 包裹或其他顶层字段。review 必须包含 passed(boolean) 和 reason(string)，可额外包含 issues(string[])。summary 不超过1200字。用户已提供完整剧本时只检查拆分与文件合同，不做语义或导演审查。"
        )
        return StageContext(
            prompt="\n".join(sections),
            image_paths=tuple(image_paths),
            loaded_text_paths=tuple(text_paths),
            loaded_skill_paths=tuple(skill_paths),
        )
