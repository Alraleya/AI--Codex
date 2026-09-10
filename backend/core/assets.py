import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Tuple

from .flow import STAGE_BY_ID
from .status import utc_now
from .tasks import link_task_asset


MIN_IMAGE_BYTES = 1024
MIN_VIDEO_BYTES = 1024
ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def validate_relative_path(relative_path: str) -> PurePosixPath:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("asset path is required")
    if "\\" in relative_path:
        raise ValueError("asset paths must use forward slashes")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe asset path: %s" % relative_path)
    return path


def resolve_asset_path(episode_dir: Path, relative_path: str) -> Path:
    relative = validate_relative_path(relative_path)
    base = Path(episode_dir).resolve()
    target = (base / Path(*relative.parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("asset path escapes episode directory")
    return target


def validate_image(path: Path) -> str:
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        raise ValueError("image is missing or smaller than %d bytes" % MIN_IMAGE_BYTES)
    with path.open("rb") as handle:
        header = handle.read(16)
        handle.seek(-2, 2)
        trailer = handle.read(2)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff") and trailer == b"\xff\xd9":
        return "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("file does not contain supported PNG, JPEG, or WebP bytes")


def validate_text(path: Path) -> str:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("text asset is missing or empty")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("text asset is not valid UTF-8")
    if not content.strip():
        raise ValueError("text asset is blank")
    return "text/markdown" if path.suffix.lower() == ".md" else "text/plain"


def validate_video(path: Path) -> Tuple[str, Dict[str, Any]]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        raise ValueError("video is missing or smaller than %d bytes" % MIN_VIDEO_BYTES)
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ValueError("ffprobe is unavailable; video metadata cannot be verified")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("ffprobe rejected video: %s" % completed.stderr.strip())
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        duration = float(payload["format"]["duration"])
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("video metadata is incomplete")
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("video metadata contains invalid duration or dimensions")
    return "video/mp4", {
        "duration_sec": duration,
        "width": width,
        "height": height,
        "codec": stream.get("codec_name", ""),
    }


def register_asset(
    status: Dict[str, Any],
    episode_dir: Path,
    asset_id: str,
    stage_id: str,
    kind: str,
    role: str,
    label: str,
    relative_path: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
    asset_revision: Optional[int] = None,
    supersedes_revision: Optional[int] = None,
) -> Dict[str, Any]:
    if not ASSET_ID_PATTERN.fullmatch(asset_id):
        raise ValueError("invalid asset_id")
    if asset_id in status["assets"]:
        raise ValueError("asset_id already registered: %s" % asset_id)
    if stage_id not in STAGE_BY_ID:
        raise ValueError("unknown stage: %s" % stage_id)
    if kind not in {"document", "prompt", "image", "video"}:
        raise ValueError("invalid asset kind: %s" % kind)
    path = resolve_asset_path(episode_dir, relative_path)
    if any(asset.get("path") == relative_path for asset in status["assets"].values()):
        raise ValueError("asset path already registered: %s" % relative_path)
    asset_metadata = dict(metadata or {})
    if kind == "image":
        mime = validate_image(path)
        expected_mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(path.suffix.lower())
        if expected_mime != mime:
            raise ValueError("image extension does not match its bytes")
    elif kind == "video":
        mime, probed = validate_video(path)
        asset_metadata.update(probed)
    else:
        mime = validate_text(path)
    logical_task_id = task_id or str(asset_metadata.get("task_id") or stage_id)
    history = status.setdefault("asset_history", {})
    matching_history = [
        item
        for item in history.values()
        if item.get("logical_asset_id") == asset_id
        or item.get("original_path") == relative_path
    ]
    for item in matching_history:
        item["logical_asset_id"] = asset_id
    prior_revisions = [
        int(item.get("asset_revision", 1)) for item in matching_history
    ]
    if asset_revision is None:
        asset_revision = (max(prior_revisions) if prior_revisions else 0) + 1
    if isinstance(asset_revision, bool) or not isinstance(asset_revision, int) or asset_revision <= 0:
        raise ValueError("asset_revision must be a positive integer")
    if supersedes_revision is None and asset_revision > 1:
        supersedes_revision = asset_revision - 1
    record = {
        "stage": stage_id,
        "task_id": logical_task_id,
        "kind": kind,
        "role": role,
        "label": label,
        "path": str(validate_relative_path(relative_path)),
        "mime": mime,
        "size": path.stat().st_size,
        "revision": status["stages"][stage_id]["revision"],
        "asset_revision": asset_revision,
        "supersedes_revision": supersedes_revision,
        "status": "active",
        "created_at": utc_now(),
        "metadata": asset_metadata,
    }
    status["assets"][asset_id] = record
    link_task_asset(status, stage_id, logical_task_id, asset_id)
    return record


def archive_asset(
    status: Dict[str, Any], episode_dir: Path, asset_id: str
) -> Dict[str, Any]:
    """Copy an active asset to immutable history before its canonical path changes."""
    try:
        record = status["assets"][asset_id]
    except KeyError as error:
        raise ValueError("unknown active asset: %s" % asset_id) from error
    source = resolve_asset_path(episode_dir, record["path"])
    if not source.is_file():
        raise ValueError("active asset file is missing: %s" % record["path"])
    asset_revision = int(record.get("asset_revision", 1))
    history_id = "%s@v%d" % (asset_id, asset_revision)
    history = status.setdefault("asset_history", {})
    if history_id in history:
        return history[history_id]

    directory_name = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:16]
    archive_relative = "versions/%s/v%04d%s" % (
        directory_name,
        asset_revision,
        source.suffix.lower(),
    )
    target = resolve_asset_path(episode_dir, archive_relative)
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

    historical = dict(record)
    historical.update(
        {
            "logical_asset_id": asset_id,
            "path": archive_relative,
            "original_path": record["path"],
            "status": "superseded",
            "superseded_at": utc_now(),
        }
    )
    history[history_id] = historical
    return historical
