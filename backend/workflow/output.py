import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.core.assets import register_asset, resolve_asset_path
from backend.core.status import (
    is_dialogue_direct,
    normalize_shot_plan,
    normalize_storyboard_plan,
    min_video_shot_duration,
    resolved_shot_count,
)
from .contracts import validate_provided_script_content, validate_text_output_names


PANEL_LINE = re.compile(r"(?m)^P(\d{2}):\s*([0-9]+(?:\.[0-9]+)?)秒(?:｜|\|)")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VIDEO_UNIT_HEADER = re.compile(
    r"^视频生成单元 U(\d{2})｜时长([0-9]+(?:\.[0-9]+)?)S｜"
    r"画幅[:：]?([^｜\n]+)｜风格[:：](.+)$"
)
VIDEO_INTERNAL_SHOT = re.compile(
    r"^U(\d{2})-(\d{2})｜"
    r"([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)S｜"
    r"([^｜\n]+)｜([^｜\n]+)｜([^｜\n]+)｜([^｜\n]+)$"
)


def _validate_panel_spine(content: str, shot: Mapping[str, Any], filename: str) -> None:
    matches = PANEL_LINE.findall(content)
    expected_numbers = ["%02d" % number for number in range(1, shot["panel_count"] + 1)]
    if [number for number, _ in matches] != expected_numbers:
        raise ValueError("%s must contain exactly one sequential P01-Pnn spine" % filename)
    if matches[0][1] != "0.0":
        raise ValueError("%s must write P01 as exactly 0.0 seconds" % filename)
    actual_timepoints = [float(point) for _, point in matches]
    expected_timepoints = [float(point) for point in shot["timepoints_sec"]]
    if any(abs(actual - expected) > 0.001 for actual, expected in zip(actual_timepoints, expected_timepoints)):
        raise ValueError("%s panel timepoints disagree with storyboard_plan" % filename)


def _validate_storyboard_prompt_contract(
    content: str, filename: str
) -> None:
    """Require the compact shot contract used by reference selection.

    The image provider cannot reliably infer a closed cast or prop list from a
    long natural-language prompt.  These headings make the roster and the
    physical action auditable before any paid image call is made.
    """
    required = (
        "[本镜可见角色]",
        "[本镜场景]",
        "[本镜关键道具]",
        "[首帧锁定]",
        "[末帧锁定]",
        "[动作因果锁]",
    )
    missing = [heading for heading in required if heading not in content]
    if missing:
        raise ValueError(
            "%s is missing explicit storyboard locks: %s"
            % (filename, ", ".join(missing))
        )
    roster_lines = []
    for heading in ("[本镜可见角色]", "[本镜场景]", "[本镜关键道具]"):
        line = next(
            (item.strip() for item in content.splitlines() if item.strip().startswith(heading)),
            "",
        )
        roster_lines.append(line)
    if not roster_lines[0] or roster_lines[0] in {"[本镜可见角色]", "[本镜可见角色] 无"}:
        raise ValueError("%s must declare at least one visible character" % filename)
    if not roster_lines[1] or roster_lines[1] == "[本镜场景]":
        raise ValueError("%s must declare at least one scene" % filename)
    forbidden = ("所有角色", "全员", "所有道具", "若干道具", "随机角色")
    if any(token in " ".join(roster_lines) for token in forbidden):
        raise ValueError("%s storyboard roster must be closed and explicit" % filename)


def _validate_video_prompt_contract(
    content: str,
    shot_number: int,
    duration_sec: float,
    aspect_ratio: str,
    filename: str,
) -> None:
    """Require an auditable generation-unit and internal-shot prompt spine."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise ValueError("%s is blank" % filename)
    header = VIDEO_UNIT_HEADER.fullmatch(lines[0])
    if header is None:
        raise ValueError(
            "%s must start with a structured '视频生成单元 Uxx' header" % filename
        )
    if int(header.group(1)) != shot_number:
        raise ValueError("%s unit id disagrees with its shot number" % filename)
    if abs(float(header.group(2)) - float(duration_sec)) > 0.001:
        raise ValueError("%s header duration disagrees with shot_plan" % filename)
    if header.group(3).strip() != str(aspect_ratio).strip():
        raise ValueError("%s header aspect ratio disagrees with creative brief" % filename)
    if "按以下内部镜头逐字执行：" not in lines:
        raise ValueError("%s is missing the internal-shot execution marker" % filename)

    candidates = [line for line in lines if re.match(r"^U\d{2}-\d{2}｜", line)]
    if not candidates:
        raise ValueError("%s must contain structured Uxx-yy internal shots" % filename)
    parsed = []
    for line in candidates:
        match = VIDEO_INTERNAL_SHOT.fullmatch(line)
        if match is None:
            raise ValueError("%s has a malformed six-field internal shot" % filename)
        unit_number, internal_number = int(match.group(1)), int(match.group(2))
        if unit_number != shot_number:
            raise ValueError("%s contains an internal shot for another unit" % filename)
        parsed.append(
            (internal_number, float(match.group(3)), float(match.group(4)))
        )

    expected_numbers = list(range(1, len(parsed) + 1))
    if [number for number, _, _ in parsed] != expected_numbers:
        raise ValueError("%s internal shot ids must be sequential from 01" % filename)
    cursor = 0.0
    for internal_number, start, end in parsed:
        if abs(start - cursor) > 0.001:
            raise ValueError(
                "%s internal shot %02d does not start at the previous endpoint"
                % (filename, internal_number)
            )
        if end <= start:
            raise ValueError(
                "%s internal shot %02d has a non-positive duration"
                % (filename, internal_number)
            )
        cursor = end
    if abs(cursor - float(duration_sec)) > 0.001:
        raise ValueError("%s internal shots do not cover the full unit duration" % filename)

    required_locks = ("字幕：", "角色与站位锁：", "动作因果锁：", "负面约束：")
    missing = [
        heading
        for heading in required_locks
        if not any(line.startswith(heading) and line != heading for line in lines)
    ]
    if missing:
        raise ValueError(
            "%s is missing explicit video locks: %s"
            % (filename, ", ".join(missing))
        )


def _validate_sequence_prompt_contents(
    stage_id: str,
    files: List[Dict[str, str]],
    creative_brief: Mapping[str, Any],
    storyboard_plan: Optional[Mapping[str, Any]],
) -> None:
    by_path = {item["path"]: item["content"] for item in files}
    shots = (
        [{"shot_number": number} for number in range(1, resolved_shot_count(creative_brief) + 1)]
        if is_dialogue_direct(creative_brief)
        else storyboard_plan["shots"]
    )
    for shot in shots:
        number = shot["shot_number"]
        video_name = "shot%02d_prompt_video.txt" % number
        duration_sec = creative_brief["shot_plan"]["durations_sec"][number - 1]
        _validate_video_prompt_contract(
            by_path[video_name],
            number,
            duration_sec,
            str(creative_brief["aspect_ratio"]),
            video_name,
        )
        if shot.get("storyboard_required", False):
            board_name = "shot%02d_prompt_storyboard.txt" % number
            _validate_panel_spine(by_path[board_name], shot, board_name)
            _validate_storyboard_prompt_contract(by_path[board_name], board_name)
            layout = "%d×%d" % (shot["columns"], shot["rows"])
            if "[版式] %s" % layout not in by_path[board_name]:
                raise ValueError("%s layout disagrees with storyboard_plan" % board_name)


def _validate_asset_plan(content: str, creative_brief: Mapping[str, Any]) -> None:
    try:
        plan = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("asset_plan.json must contain valid JSON") from error
    if not isinstance(plan, dict) or set(plan) != {"version", "assets", "shots"}:
        raise ValueError("asset_plan.json has unexpected fields")
    if plan["version"] != "1.0" or not isinstance(plan["assets"], list) or not isinstance(plan["shots"], list):
        raise ValueError("asset_plan.json has invalid field types")
    asset_ids = set()
    asset_required = {}
    prefixes = {"character": "char_", "scene": "scene_", "prop": "prop_"}
    for asset in plan["assets"]:
        if not isinstance(asset, dict) or set(asset) != {"id", "type", "name", "required", "reason", "shots"}:
            raise ValueError("asset_plan asset has unexpected fields")
        asset_id = asset["id"]
        asset_type = asset["type"]
        if asset_type not in prefixes or not isinstance(asset_id, str) or not asset_id.startswith(prefixes[asset_type]):
            raise ValueError("asset_plan asset id/type is invalid")
        if asset_id in asset_ids or not isinstance(asset["required"], bool):
            raise ValueError("asset_plan asset ids must be unique and required must be boolean")
        if not isinstance(asset["name"], str) or not asset["name"].strip() or not isinstance(asset["reason"], str) or not asset["reason"].strip():
            raise ValueError("asset_plan asset name/reason is required")
        if not isinstance(asset["shots"], list) or any(not isinstance(number, int) for number in asset["shots"]):
            raise ValueError("asset_plan asset shots must be integer lists")
        asset_ids.add(asset_id)
        asset_required[asset_id] = asset["required"]
    expected_numbers = list(range(1, resolved_shot_count(creative_brief) + 1))
    if [shot.get("shot_number") for shot in plan["shots"] if isinstance(shot, dict)] != expected_numbers:
        raise ValueError("asset_plan shots must match video generation units")
    for shot in plan["shots"]:
        if set(shot) != {"shot_number", "storyboard_required", "references"} or not isinstance(shot["storyboard_required"], bool) or not isinstance(shot["references"], list):
            raise ValueError("asset_plan shot has unexpected fields")
        for reference in shot["references"]:
            if not isinstance(reference, dict) or set(reference) != {"asset_id", "necessity", "purpose"}:
                raise ValueError("asset_plan reference has unexpected fields")
            asset_id = reference["asset_id"]
            if asset_id not in asset_ids or reference["necessity"] not in {"required", "optional"}:
                raise ValueError("asset_plan reference targets an unknown asset")
            # A globally required asset may be optional for a particular unit
            # when the static-reference cap is exceeded; the locked prompt
            # still carries the continuity instruction for that unit.
            if reference["necessity"] == "required" and not asset_required[asset_id]:
                raise ValueError("asset_plan reference necessity disagrees with the asset")
    if is_dialogue_direct(creative_brief):
        if any(shot["storyboard_required"] for shot in plan["shots"]):
            raise ValueError("dialogue-direct asset plan cannot require storyboards")
    else:
        expected_flags = {
            shot["shot_number"]: shot.get("storyboard_required", True)
            for shot in creative_brief["storyboard_plan"]["shots"]
        }
        if any(
            shot["storyboard_required"] != expected_flags[shot["shot_number"]]
            for shot in plan["shots"]
        ):
            raise ValueError("asset_plan changed the locked storyboard decisions")


def validate_stage_payload(
    stage_id: str, payload: Mapping[str, Any], creative_brief: Mapping[str, object]
) -> List[Dict[str, Any]]:
    if set(payload.keys()) != {
        "files",
        "review",
        "summary",
        "shot_plan",
        "storyboard_plan",
    }:
        raise ValueError("structured result has unexpected top-level fields")
    files = payload.get("files")
    review = payload.get("review")
    summary = payload.get("summary")
    if (
        not isinstance(files, list)
        or not isinstance(review, dict)
        or not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > 1200
    ):
        raise ValueError("structured result has invalid field types")
    shot_plan = payload.get("shot_plan")
    storyboard_plan = payload.get("storyboard_plan")
    if stage_id == "story_design":
        normalized_shot_plan = normalize_shot_plan(
            shot_plan,
            creative_brief["target_duration_sec"],
            require_resolved=True,
            enforce_video_duration_limits=True,
            min_duration_sec=min_video_shot_duration(creative_brief),
        )
        if is_dialogue_direct(creative_brief):
            if storyboard_plan is not None:
                raise ValueError("dialogue-direct story_design cannot return a storyboard_plan")
        else:
            storyboard_plan = normalize_storyboard_plan(
                storyboard_plan,
                normalized_shot_plan,
                require_resolved=True,
                min_duration_sec=min_video_shot_duration(creative_brief),
            )
    elif shot_plan is not None:
        raise ValueError("only story_design may return a shot_plan")
    elif storyboard_plan is not None:
        raise ValueError("only story_design may return a storyboard_plan")
    review_keys = set(review.keys())
    if not review_keys.issubset({"passed", "reason", "issues"}) or not {
        "passed",
        "reason",
    }.issubset(review_keys):
        raise ValueError("review has unexpected fields")
    if not isinstance(review["passed"], bool) or not isinstance(review["reason"], str):
        raise ValueError("review has invalid field types")
    if "issues" in review:
        if not isinstance(review["issues"], list) or any(
            not isinstance(issue, str) for issue in review["issues"]
        ):
            raise ValueError("review issues must be a list of strings")
    normalized = []
    for item in files:
        if not isinstance(item, dict) or "path" not in item:
            raise ValueError("file result must be an object")
        path = item["path"]
        if not isinstance(path, str) or not path.strip():
            raise ValueError("file result has an invalid path")
        if set(item.keys()) == {"path", "content"}:
            content = item["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("inline file content must be a non-empty string")
            normalized.append({"path": path, "content": content})
            continue
        if set(item.keys()) == {"path", "size", "sha256"}:
            size = item["size"]
            sha256 = item["sha256"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or not isinstance(sha256, str)
                or not SHA256.fullmatch(sha256)
            ):
                raise ValueError("file manifest has invalid path, size, or sha256")
            normalized.append({"path": path, "size": size, "sha256": sha256})
            continue
        raise ValueError("file result must contain path/content or path/size/sha256")
    if normalized and any("content" in item for item in normalized) and any(
        "size" in item for item in normalized
    ):
        raise ValueError("file results cannot mix inline content and manifests")
    contract_brief = dict(creative_brief)
    if stage_id == "story_design":
        contract_brief["shot_plan"] = shot_plan
        if storyboard_plan is not None:
            contract_brief["storyboard_plan"] = storyboard_plan
    validate_text_output_names(
        stage_id,
        [item["path"] for item in normalized],
        contract_brief,
        storyboard_plan=storyboard_plan,
        shot_plan=shot_plan,
    )
    if stage_id == "story_design":
        validate_provided_script_content(normalized, creative_brief)
        if normalized and "content" in normalized[0]:
            _validate_sequence_prompt_contents(
                stage_id,
                normalized,
                contract_brief,
                storyboard_plan,
            )
    if stage_id == "asset_planning" and normalized and "content" in normalized[0]:
        _validate_asset_plan(normalized[0]["content"], creative_brief)
    return normalized


def materialize_inline_stage_files(
    payload: Mapping[str, Any], output_dir: Path
) -> Dict[str, Any]:
    """Materialize child-returned text into the deterministic staging directory."""
    files = payload["files"]
    if not files or "content" not in files[0]:
        return dict(payload)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item in files:
        path = resolve_asset_path(output_dir, item["path"])
        if path.exists() and path.is_symlink():
            raise ValueError("inline output path is symlinked: %s" % item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(item["content"])
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        data = path.read_bytes()
        manifest.append(
            {
                "path": item["path"],
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    normalized = dict(payload)
    normalized["files"] = manifest
    return normalized


def _asset_role(stage_id: str, path: str) -> str:
    if path.endswith("_prompt.txt") or "_prompt_" in path:
        return "video_prompt" if "_prompt_video" in path else "image_prompt"
    roles = {
        "outline.md": "outline",
        "directing.md": "directing",
        "script.md": "script",
        "storyboard.md": "storyboard_design",
        "asset_plan.json": "asset_plan",
        "edit_post.md": "edit_post_plan",
    }
    if path in roles:
        return roles[path]
    if path.startswith("char_"):
        return "character_sheet"
    if path.startswith("scene_"):
        return "scene_sheet"
    if path.startswith("prop_"):
        return "prop_sheet"
    if path.endswith("_references.json"):
        return "reference_binding"
    return "%s_output" % stage_id


def _read_staged_files(
    files: List[Dict[str, Any]], staging_dir: Path
) -> List[Dict[str, str]]:
    contents = []
    staging_dir = Path(staging_dir).resolve()
    for item in files:
        path = resolve_asset_path(staging_dir, item["path"])
        if path.is_symlink() or not path.is_file():
            raise ValueError("staged output is missing or symlinked: %s" % item["path"])
        if path.stat().st_size != item["size"]:
            raise ValueError("staged output size mismatch: %s" % item["path"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError("staged output sha256 mismatch: %s" % item["path"])
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("staged output is blank: %s" % item["path"])
        contents.append({"path": item["path"], "content": content})
    return contents


def persist_staged_text_outputs(
    status: Dict[str, Any],
    stage_id: str,
    payload: Mapping[str, Any],
    episode_dir: Path,
    output_dir: Path,
) -> List[str]:
    files = validate_stage_payload(stage_id, payload, status["creative_brief"])
    contents = _read_staged_files(files, output_dir)
    if stage_id == "story_design":
        validate_provided_script_content(contents, status["creative_brief"])
    storyboard_plan = payload.get("storyboard_plan")
    if stage_id == "story_design":
        validation_brief = dict(status["creative_brief"])
        validation_brief["shot_plan"] = payload["shot_plan"]
        _validate_sequence_prompt_contents(
            stage_id,
            contents,
            validation_brief,
            storyboard_plan,
        )
    if stage_id == "asset_planning":
        asset_plan = next(item["content"] for item in contents if item["path"] == "asset_plan.json")
        _validate_asset_plan(asset_plan, status["creative_brief"])
    episode_dir = Path(episode_dir)
    episode_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for item, content_item in zip(files, contents):
        target = resolve_asset_path(episode_dir, item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        staged_path = resolve_asset_path(Path(output_dir), item["path"])
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent)
        )
        os.close(descriptor)
        try:
            shutil.copyfile(staged_path, temporary_name)
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        asset_id = "%s.%s" % (
            stage_id,
            hashlib.sha256(item["path"].encode("utf-8")).hexdigest()[:16],
        )
        kind = "prompt" if target.suffix.lower() == ".txt" else "document"
        register_asset(
            status,
            episode_dir,
            asset_id,
            stage_id,
            kind,
            _asset_role(stage_id, item["path"]),
            target.stem,
            item["path"],
        )
        written.append(item["path"])
    shutil.rmtree(Path(output_dir), ignore_errors=True)
    return written
