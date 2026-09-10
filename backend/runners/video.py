import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from backend.core.assets import validate_image, validate_video
from backend.core.status import normalize_video_session_id
from backend.workflow.chain import (
    is_video_confirmation_valid,
    is_video_shot_confirmation_valid,
    is_video_supplement_confirmation_valid,
)
from backend.core.authorization import ProviderCallNotAuthorized


class VideoProviderUnavailable(RuntimeError):
    pass


class VideoGenerationError(RuntimeError):
    pass


VIDEO_MODEL_CLI = {
    "fast": "seedance2.0fast_vip",
    "mini": "seedance2.0mini",
}

# The current Jimeng/Seedance API rejects payloads with more than nine images.
MAX_VIDEO_REFERENCES = 12


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    target_path: Path
    duration_sec: float
    aspect_ratio: str
    shot_number: int = 1
    reference_paths: Sequence[Path] = ()
    supplement_id: Optional[str] = None
    supplement_prompt_path: Optional[Path] = None
    supplement_references_path: Optional[Path] = None

    def validate(self) -> None:
        if not re.fullmatch(
            r"shot\d+[a-z]?_video_jimeng_v[1-9]\d*\.mp4", self.target_path.name
        ):
            raise ValueError("video target name does not match the versioned contract")
        if float(self.duration_sec) <= 0:
            raise ValueError("video duration must be positive")
        if not re.fullmatch(r"[1-9]\d*:[1-9]\d*", self.aspect_ratio):
            raise ValueError("video aspect ratio is invalid")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("video prompt is blank")
        if len(self.reference_paths) > MAX_VIDEO_REFERENCES:
            raise ValueError(
                "a video request may contain at most nine references"
            )
        for path in self.reference_paths:
            validate_image(path)
        if self.supplement_id and (
            not self.supplement_prompt_path or not self.supplement_references_path
        ):
            raise ValueError("supplemental video approval inputs are incomplete")


class JimengVideoRunner:
    def __init__(
        self,
        executable: Optional[str] = None,
        args_template: Optional[Sequence[str]] = None,
        timeout_sec: int = 1800,
    ):
        self.executable = executable or os.environ.get("JIMENG_VIDEO_CLI") or shutil.which("dreamina")
        if args_template is None:
            encoded = os.environ.get("JIMENG_VIDEO_ARGS_JSON", "")
            if encoded:
                try:
                    args_template = json.loads(encoded)
                except json.JSONDecodeError:
                    raise VideoProviderUnavailable("JIMENG_VIDEO_ARGS_JSON is invalid JSON")
        self.args_template = tuple(args_template or ())
        self.timeout_sec = timeout_sec
        self.native_dreamina = bool(
            self.executable
            and Path(self.executable).name == "dreamina"
            and not self.args_template
        )

    def health_check(self) -> Dict[str, str]:
        if not self.executable or not Path(self.executable).is_file():
            raise VideoProviderUnavailable("Jimeng video CLI is not configured")
        if not os.access(self.executable, os.X_OK):
            raise VideoProviderUnavailable("Jimeng video CLI is not executable")
        if self.native_dreamina:
            return {"provider": "jimeng_video_cli", "executable": self.executable}
        if not self.args_template:
            raise VideoProviderUnavailable("Jimeng video argument template is not configured")
        joined = "\0".join(self.args_template)
        for required in (
            "{prompt_file}",
            "{references_file}",
            "{duration_sec}",
            "{aspect_ratio}",
            "{model_version}",
            "{session_id}",
            "{output}",
        ):
            if required not in joined:
                raise VideoProviderUnavailable("Jimeng video template is missing %s" % required)
        return {"provider": "jimeng_video_cli", "executable": self.executable}

    def _command(
        self,
        prompt_file: Path,
        references_file: Path,
        output: Path,
        duration_sec: float,
        aspect_ratio: str,
        model_version: str,
        session_id: str,
    ) -> Sequence[str]:
        self.health_check()
        values = {
            "prompt_file": str(prompt_file),
            "references_file": str(references_file),
            "duration_sec": "%g" % float(duration_sec),
            "aspect_ratio": aspect_ratio,
            "model_version": model_version,
            "session_id": session_id,
            "output": str(output),
        }
        try:
            arguments = [argument.format(**values) for argument in self.args_template]
        except KeyError as error:
            raise VideoProviderUnavailable("unknown Jimeng video template field: %s" % error)
        return [self.executable] + arguments

    def _native_command(
        self,
        prompt: str,
        references: Sequence[Path],
        duration_sec: float,
        aspect_ratio: str,
        model_version: str,
        session_id: str,
    ) -> Sequence[str]:
        resolution = os.environ.get("JIMENG_VIDEO_RESOLUTION", "720p")
        # Dreamina may keep a successful task in `querying` for several minutes
        # after the UI already shows a playable preview.  The old 180s default
        # turned that normal provider state into a false generation failure.
        poll = os.environ.get("JIMENG_VIDEO_POLL_SEC", "600")
        command = [
            self.executable,
            "multimodal2video",
            "--prompt",
            prompt,
            "--duration",
            str(int(round(float(duration_sec)))),
            "--ratio",
            aspect_ratio,
            "--video_resolution",
            resolution,
            "--model_version",
            model_version,
            "--session",
            session_id,
            "--poll",
            poll,
        ]
        for reference in references:
            command.extend(["--image", str(reference.resolve())])
        return command

    @staticmethod
    def _parse_cli_json(stdout: str) -> Dict[str, Any]:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stdout):
            if character != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and (
                "gen_status" in payload
                or "status" in payload
                or "result_json" in payload
                or "result" in payload
            ):
                return payload
        tail = stdout.strip()[-800:]
        detail = "Dreamina video CLI returned no JSON result"
        if tail:
            detail += ": " + tail
        raise VideoGenerationError(detail)

    @staticmethod
    def _success_status(payload: Dict[str, Any]) -> bool:
        raw = payload.get("gen_status", payload.get("status"))
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw == 1
        value = str(raw or "").strip().lower()
        return value in {"success", "succeeded", "completed", "complete", "done", "finished", "finish", "1"}

    @staticmethod
    def _video_result(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Accept the native CLI's historical result envelopes."""
        candidates = [payload]
        for key in ("result_json", "result", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        for candidate in candidates:
            videos = candidate.get("videos")
            if isinstance(videos, list) and videos and isinstance(videos[0], dict):
                return videos[0]
            for key in ("video_url", "url", "video_uri", "video_path"):
                value = candidate.get(key)
                if value:
                    return {key: value}
        return {}

    def _run_native_dreamina(
        self,
        prompt: str,
        references: Sequence[Path],
        staged_output: Path,
        duration_sec: float,
        aspect_ratio: str,
        model_version: str,
        session_id: str,
    ) -> Dict[str, Any]:
        completed = subprocess.run(
            self._native_command(
                prompt,
                references,
                duration_sec,
                aspect_ratio,
                model_version,
                session_id,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.timeout_sec,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise VideoGenerationError(
                "Jimeng video CLI exited %d%s"
                % (completed.returncode, (": " + detail[-800:]) if detail else "")
            )
        payload = self._parse_cli_json(completed.stdout)
        if not self._success_status(payload):
            raise VideoGenerationError(
                str(
                    payload.get("fail_reason")
                    or payload.get("message")
                    or payload.get("error")
                    or "Jimeng video generation failed; provider status=%s"
                    % payload.get("gen_status", payload.get("status"))
                )
            )
        video = self._video_result(payload)
        video_url = video.get("video_url") or video.get("url") or video.get("video_uri")
        video_path = video.get("video_path")
        if video_path and Path(str(video_path)).is_file():
            shutil.copyfile(str(video_path), staged_output)
        elif video_url:
            try:
                with urllib.request.urlopen(video_url, timeout=self.timeout_sec) as response:
                    with staged_output.open("wb") as handle:
                        shutil.copyfileobj(response, handle)
            except Exception as error:
                raise VideoGenerationError(
                    "Dreamina returned a video URL but local download failed: %s" % error
                ) from error
        else:
            raise VideoGenerationError(
                "Dreamina video result did not include a downloadable video URL/path"
            )
        return {
            "submit_id": payload.get("submit_id"),
            "model_version": model_version,
            "video_url": video_url,
            "provider_width": video.get("width"),
            "provider_height": video.get("height"),
            "provider_duration_sec": video.get("duration"),
            "provider_fps": video.get("fps"),
        }

    def run(
        self,
        request: VideoRequest,
        status: Dict[str, Any],
        episode_dir: Path,
        authorized: bool = False,
    ) -> Dict[str, object]:
        if not authorized:
            raise ProviderCallNotAuthorized(
                "video generation requires an explicit current user action"
            )
        request.validate()
        if request.supplement_id:
            approval_valid = is_video_supplement_confirmation_valid(
                status,
                episode_dir,
                request.supplement_id,
                request.supplement_prompt_path,
                request.supplement_references_path,
                request.duration_sec,
            )
        else:
            approval_valid = (
                is_video_confirmation_valid(status, episode_dir)
                if "creative_brief" not in status
                else is_video_shot_confirmation_valid(
                    status, episode_dir, request.shot_number
                )
            )
        if not approval_valid:
            raise ProviderCallNotAuthorized(
                "video shot approval does not match the current upstream revision"
            )
        target = Path(request.target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        prompt_handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="jimeng-video-prompt-", suffix=".txt", delete=False
        )
        references_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="jimeng-video-references-",
            suffix=".json",
            delete=False,
        )
        output_handle = tempfile.NamedTemporaryFile(
            prefix=".jimeng-video-", suffix=".mp4", dir=str(target.parent), delete=False
        )
        prompt_path = Path(prompt_handle.name)
        references_path = Path(references_handle.name)
        staged_output = Path(output_handle.name)
        prompt_handle.close()
        references_handle.close()
        output_handle.close()
        staged_output.unlink()
        video_model = status.get("creative_brief", {}).get("video_model", "fast")
        try:
            model_version = VIDEO_MODEL_CLI[video_model]
        except KeyError as error:
            raise VideoProviderUnavailable(
                "video model must be selected as fast or mini"
            ) from error
        try:
            session_id = normalize_video_session_id(
                status.get("creative_brief", {}).get("video_session_id")
            )
        except ValueError as error:
            raise VideoProviderUnavailable(str(error)) from error
        if not session_id:
            raise VideoProviderUnavailable(
                "episode video_session_id must be configured before video generation"
            )
        try:
            prompt_path.write_text(request.prompt, encoding="utf-8")
            references_path.write_text(
                json.dumps(
                    [str(path.resolve()) for path in request.reference_paths],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if self.native_dreamina:
                native_metadata = self._run_native_dreamina(
                    request.prompt,
                    request.reference_paths,
                    staged_output,
                    request.duration_sec,
                    request.aspect_ratio,
                    model_version,
                    session_id,
                )
            else:
                completed = subprocess.run(
                    self._command(
                        prompt_path,
                        references_path,
                        staged_output,
                        request.duration_sec,
                        request.aspect_ratio,
                        model_version,
                        session_id,
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
                if completed.returncode != 0:
                    raise VideoGenerationError(
                        completed.stderr.strip() or "Jimeng video generation failed"
                    )
                native_metadata = {}
            mime, metadata = validate_video(staged_output)
            os.replace(str(staged_output), str(target))
        except subprocess.TimeoutExpired:
            raise VideoGenerationError(
                "Jimeng video generation timed out after %d seconds" % self.timeout_sec
            )
        finally:
            try:
                prompt_path.unlink()
            except FileNotFoundError:
                pass
            try:
                references_path.unlink()
            except FileNotFoundError:
                pass
            try:
                staged_output.unlink()
            except FileNotFoundError:
                pass
        return {
            "path": str(target),
            "mime": mime,
            "size": target.stat().st_size,
            "provider": "jimeng_video_cli",
            "metadata": {
                **metadata,
                **native_metadata,
                "requested_duration_sec": float(request.duration_sec),
                "requested_aspect_ratio": request.aspect_ratio,
                "model_version": model_version,
                "video_session_id": session_id,
                "reference_paths": [
                    str(path.resolve()) for path in request.reference_paths
                ],
            },
        }
