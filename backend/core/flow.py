from dataclasses import dataclass
from typing import Dict, Iterable, Tuple


FLOW_ID = "manga_episode"
FLOW_VERSION = "2.0"


@dataclass(frozen=True)
class StageDefinition:
    id: str
    label: str
    depends_on: Tuple[str, ...]
    skills: Tuple[str, ...]
    execution_mode: str = "main_agent"
    task_unit: str = "stage"


STAGES: Tuple[StageDefinition, ...] = (
    StageDefinition(
        "story_design",
        "剧本与分镜定稿",
        (),
        ("design-episode",),
    ),
    StageDefinition(
        "asset_planning",
        "定妆资源规划",
        ("story_design",),
        ("plan-visual-assets",),
    ),
    StageDefinition(
        "character_design",
        "角色定妆",
        ("asset_planning",),
        ("design-characters",),
        "parallel_tasks",
        "character",
    ),
    StageDefinition(
        "visual_design",
        "场景与道具定妆",
        ("character_design",),
        ("design-visual-assets",),
        "parallel_tasks",
        "visual_asset",
    ),
    StageDefinition(
        "storyboard_binding",
        "故事板参考绑定",
        ("visual_design",),
        (),
    ),
    StageDefinition(
        "storyboard_generation",
        "多格故事板出图",
        ("storyboard_binding",),
        (),
        "parallel_tasks",
        "shot",
    ),
    StageDefinition(
        "video_binding",
        "视频参考绑定",
        ("storyboard_generation",),
        (),
    ),
    StageDefinition(
        "video_generation",
        "视频生成",
        ("video_binding",),
        (),
        "parallel_tasks",
        "shot",
    ),
    StageDefinition(
        "edit_post",
        "剪辑交付",
        ("video_generation",),
        ("plan-edit",),
    ),
)

STAGE_BY_ID: Dict[str, StageDefinition] = {stage.id: stage for stage in STAGES}
STAGE_IDS: Tuple[str, ...] = tuple(stage.id for stage in STAGES)


def validate_flow(stages: Iterable[StageDefinition] = STAGES) -> None:
    """Raise ValueError when a flow is ambiguous or has forward dependencies."""
    seen = set()
    for stage in stages:
        if stage.id in seen:
            raise ValueError("duplicate stage id: %s" % stage.id)
        for dependency in stage.depends_on:
            if dependency not in seen:
                raise ValueError(
                    "stage %s depends on missing or downstream stage %s"
                    % (stage.id, dependency)
                )
        if stage.execution_mode not in {"main_agent", "parallel_tasks"}:
            raise ValueError("invalid execution mode for stage %s" % stage.id)
        if not stage.task_unit:
            raise ValueError("stage %s must declare a task unit" % stage.id)
        seen.add(stage.id)


def transitive_downstream(stage_id: str) -> Tuple[str, ...]:
    if stage_id not in STAGE_BY_ID:
        raise KeyError("unknown stage: %s" % stage_id)
    affected = {stage_id}
    changed = True
    while changed:
        changed = False
        for stage in STAGES:
            if stage.id not in affected and any(
                dependency in affected for dependency in stage.depends_on
            ):
                affected.add(stage.id)
                changed = True
    return tuple(stage.id for stage in STAGES if stage.id in affected)


validate_flow()
