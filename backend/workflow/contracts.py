import re
from typing import Any, Iterable, List, Mapping, Sequence, Set

from backend.core.assets import validate_relative_path
from backend.core.flow import STAGE_BY_ID
from backend.core.status import (
    is_dialogue_direct,
    normalize_storyboard_plan,
    resolved_shot_count,
    storyboard_required_shots,
)


def validate_provided_script_content(
    files: Iterable[Mapping[str, Any]], creative_brief: Mapping[str, Any]
) -> None:
    """Reject any inline story-design script that changes the user's source."""
    expected = creative_brief.get("provided_script")
    if expected is None:
        return
    for item in files:
        if item.get("path") == "script.md" and "content" in item:
            if item["content"] != expected:
                raise ValueError(
                    "script.md must exactly preserve the user-provided script; "
                    "台词和剧本内容不可改写"
                )
            return


def _as_safe_names(output_names: Iterable[str]) -> List[str]:
    names = list(output_names)
    if len(names) != len(set(names)):
        raise ValueError("stage outputs contain duplicate paths")
    for name in names:
        path = validate_relative_path(name)
        if len(path.parts) != 1:
            raise ValueError("stage outputs must be files in the episode root: %s" % name)
    return names


def _grouped_keys(names: Set[str], pattern: str) -> Set[str]:
    expression = re.compile(pattern)
    keys = set()
    for name in names:
        match = expression.fullmatch(name)
        if match:
            keys.add(match.group(1))
    return keys


def _validate_complete_groups(
    names: Set[str], prefixes: Sequence[str], required: bool
) -> None:
    groups = [
        _grouped_keys(names, r"%s_(.+)%s" % (re.escape(prefix), suffix))
        for prefix, suffix in (
            (prefixes[0], r"_sheet\.md"),
            (prefixes[0], r"_prompt\.txt"),
            (prefixes[0], r"_sheet\.png"),
        )
    ]
    if required and not groups[0]:
        raise ValueError("at least one complete %s asset group is required" % prefixes[0])
    if not (groups[0] == groups[1] == groups[2]):
        raise ValueError("%s text, prompt, and image outputs must be paired" % prefixes[0])
    allowed = set()
    for key in groups[0]:
        allowed.update(
            {
                "%s_%s_sheet.md" % (prefixes[0], key),
                "%s_%s_prompt.txt" % (prefixes[0], key),
                "%s_%s_sheet.png" % (prefixes[0], key),
            }
        )
    unexpected = names - allowed
    if unexpected:
        raise ValueError("unexpected outputs: %s" % ", ".join(sorted(unexpected)))


def expected_shot_names(shot_count: int, suffix: str) -> Set[str]:
    return {"shot%02d_%s" % (number, suffix) for number in range(1, shot_count + 1)}


def _storyboard_required_shots_or_all(
    creative_brief: Mapping[str, object], storyboard_plan: Mapping[str, object] = None
) -> list:
    """Use legacy all-board behavior while validating a not-yet-committed prompt payload."""
    if storyboard_plan is not None:
        normalized = normalize_storyboard_plan(
            storyboard_plan,
            creative_brief["shot_plan"],
            require_resolved=True,
        )
        return [shot for shot in normalized["shots"] if shot.get("storyboard_required", True)]
    try:
        return storyboard_required_shots(creative_brief)
    except (KeyError, TypeError, ValueError):
        shot_count = resolved_shot_count(creative_brief)
        return [{"shot_number": number, "storyboard_required": True} for number in range(1, shot_count + 1)]


def validate_text_output_names(
    stage_id: str,
    output_names: Iterable[str],
    creative_brief: Mapping[str, object],
    storyboard_plan: Mapping[str, object] = None,
    shot_plan: Mapping[str, object] = None,
) -> List[str]:
    """Validate only the files returned by Codex before provider assets are added."""
    names_list = _as_safe_names(output_names)
    names = set(names_list)
    if stage_id == "story_design":
        expected = {"outline.md", "directing.md", "script.md"}
        plan = shot_plan or creative_brief.get("shot_plan")
        shot_count = int(plan["shot_count"])
        expected |= expected_shot_names(shot_count, "prompt_video.txt")
        if not is_dialogue_direct(creative_brief):
            expected.add("storyboard.md")
            expected |= {
                "shot%02d_prompt_storyboard.txt" % shot["shot_number"]
                for shot in _storyboard_required_shots_or_all(
                    creative_brief, storyboard_plan
                )
            }
    elif stage_id == "asset_planning":
        expected = {"asset_plan.json"}
    elif stage_id == "character_design":
        prefix = "char"
        sheets = _grouped_keys(names, r"%s_(.+)_sheet\.md" % prefix)
        prompts = _grouped_keys(names, r"%s_(.+)_prompt\.txt" % prefix)
        if sheets != prompts:
            raise ValueError("%s text and prompt outputs must be paired" % prefix)
        expected = {
            "%s_%s%s" % (prefix, key, suffix)
            for key in sheets
            for suffix in ("_sheet.md", "_prompt.txt")
        }
    elif stage_id == "visual_design":
        scene_sheets = _grouped_keys(names, r"scene_(.+)_sheet\.md")
        scene_prompts = _grouped_keys(names, r"scene_(.+)_prompt\.txt")
        prop_sheets = _grouped_keys(names, r"prop_(.+)_sheet\.md")
        prop_prompts = _grouped_keys(names, r"prop_(.+)_prompt\.txt")
        if scene_sheets != scene_prompts or prop_sheets != prop_prompts:
            raise ValueError("scene/prop text and prompt outputs must be paired")
        expected = {
            "%s_%s%s" % (prefix, key, suffix)
            for prefix, keys in (("scene", scene_sheets), ("prop", prop_sheets))
            for key in keys
            for suffix in ("_sheet.md", "_prompt.txt")
        }
    elif stage_id == "storyboard_binding":
        expected = {
            "shot%02d_storyboard_references.json" % shot["shot_number"]
            for shot in _storyboard_required_shots_or_all(creative_brief)
        }
    elif stage_id == "video_binding":
        shot_count = resolved_shot_count(creative_brief)
        expected = expected_shot_names(shot_count, "video_references.json")
    elif stage_id == "edit_post":
        expected = {"edit_post.md", "rough_cut.mp4"}
    else:
        raise ValueError("stage %s does not produce Codex text outputs" % stage_id)
    if names != expected:
        raise ValueError(
            "text output contract failed; missing=%s unexpected=%s"
            % (sorted(expected - names), sorted(names - expected))
        )
    return names_list


def validate_output_names(
    stage_id: str, output_names: Iterable[str], creative_brief: Mapping[str, object]
) -> List[str]:
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    names_list = _as_safe_names(output_names)
    names = set(names_list)

    if stage_id == "story_design":
        expected = {"outline.md", "directing.md", "script.md"}
        shot_count = resolved_shot_count(creative_brief)
        expected |= expected_shot_names(shot_count, "prompt_video.txt")
        if not is_dialogue_direct(creative_brief):
            expected.add("storyboard.md")
            expected |= {
                "shot%02d_prompt_storyboard.txt" % shot["shot_number"]
                for shot in _storyboard_required_shots_or_all(creative_brief)
            }
    elif stage_id == "asset_planning":
        expected = {"asset_plan.json"}
    elif stage_id == "character_design":
        _validate_complete_groups(names, ("char",), required=False)
        return names_list
    elif stage_id == "visual_design":
        scene_names = {name for name in names if name.startswith("scene_")}
        prop_names = {name for name in names if name.startswith("prop_")}
        _validate_complete_groups(scene_names, ("scene",), required=False)
        _validate_complete_groups(prop_names, ("prop",), required=False)
        return names_list
    elif stage_id == "storyboard_binding":
        expected = {
            "shot%02d_storyboard_references.json" % shot["shot_number"]
            for shot in _storyboard_required_shots_or_all(creative_brief)
        }
    elif stage_id == "storyboard_generation":
        expected = {
            "shot%02d_storyboard.png" % shot["shot_number"]
            for shot in _storyboard_required_shots_or_all(creative_brief)
        }
    elif stage_id == "video_binding":
        shot_count = resolved_shot_count(creative_brief)
        expected = expected_shot_names(shot_count, "video_references.json")
    elif stage_id == "video_generation":
        shot_count = resolved_shot_count(creative_brief)
        matched_shots = set()
        for name in names:
            match = re.fullmatch(r"shot(\d+)_video_jimeng_v[1-9]\d*\.mp4", name)
            if not match:
                raise ValueError("unexpected video output: %s" % name)
            matched_shots.add(int(match.group(1)))
        if matched_shots != set(range(1, shot_count + 1)) or len(names) != shot_count:
            raise ValueError("video output count or shot numbering is incorrect")
        return names_list
    else:
        expected = {"edit_post.md", "rough_cut.mp4"}

    if names != expected:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected)
        raise ValueError("output contract failed; missing=%s unexpected=%s" % (missing, unexpected))
    return names_list
