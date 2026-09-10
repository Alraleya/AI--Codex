import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from backend.core.assets import validate_image
from backend.core.authorization import ProviderCallNotAuthorized
from backend.core.models import DEFAULT_MODEL


ASSET_TYPES = {"character_sheet", "scene_sheet", "prop_sheet", "storyboard"}


class ImageProviderUnavailable(RuntimeError):
    pass


class ImageGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    target_path: Path
    asset_type: str
    reference_paths: Tuple[Path, ...] = ()

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("image prompt is required")
        if self.asset_type not in ASSET_TYPES:
            raise ValueError("unsupported image asset type: %s" % self.asset_type)
        if self.target_path.suffix.lower() != ".png":
            raise ValueError("canonical image targets must use .png")
        for reference in self.reference_paths:
            validate_image(reference)


class CodexImagegenRunner:
    def __init__(
        self,
        executable: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout_sec: int = 900,
    ):
        self.executable = executable or shutil.which("codex")
        self.model = model
        self.timeout_sec = timeout_sec

    def health_check(self) -> Dict[str, str]:
        if not self.executable:
            raise ImageProviderUnavailable("Codex CLI is not installed")
        checks = (
            ("version", [self.executable, "--version"], 10),
            ("auth", [self.executable, "login", "status"], 15),
            ("features", [self.executable, "features", "list"], 15),
        )
        results = {}
        try:
            for name, command, timeout in checks:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if completed.returncode != 0:
                    raise ImageProviderUnavailable(
                        completed.stderr.strip() or "Codex %s check failed" % name
                    )
                results[name] = completed.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ImageProviderUnavailable("Codex image health check failed: %s" % error)
        if not re.search(
            r"^\s*image_generation\s+\S+\s+true\s*$",
            results["features"],
            flags=re.MULTILINE,
        ):
            raise ImageProviderUnavailable(
                "Codex built-in image_generation feature is unavailable"
            )
        return {
            "provider": "codex_imagegen",
            "version": results["version"],
            "auth": results["auth"],
            "feature": "image_generation",
        }

    def build_command(
        self, job_dir: Path, references: Sequence[Path] = ()
    ) -> Sequence[str]:
        if not self.executable:
            raise ImageProviderUnavailable("Codex CLI is not installed")
        command = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--model",
            self.model,
            "--cd",
            str(Path(job_dir).resolve()),
        ]
        for reference in references:
            command.extend(["--image", str(Path(reference).resolve())])
        command.append("-")
        return command

    def run(self, request: ImageRequest, authorized: bool = False) -> Dict[str, object]:
        if not authorized:
            raise ProviderCallNotAuthorized(
                "image generation requires an explicit current user action"
            )
        request.validate()
        target = Path(request.target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="manga-imagegen-") as directory:
            job_dir = Path(directory)
            staged_output = job_dir / target.name
            reference_paths = [str(Path(path).resolve()) for path in request.reference_paths]
            reference_instruction = ""
            if reference_paths:
                reference_instruction = (
                    "\nLocal reference images are available at these exact filesystem paths:\n- "
                    + "\n- ".join(reference_paths)
                    + "\nPass this exact list through the image generation tool's "
                    "referenced_image_paths argument. Do not use recent-conversation image "
                    "counting for these local files. Inspect the references first if the skill "
                    "requires it, then generate the requested derived asset.\n"
                )
            instruction = (
                "The current workbench action explicitly authorizes this paid image call. "
                "Project policy says text and image generation run directly; only video "
                "generation requires a separate confirmation. Do not ask for image "
                "confirmation again.\n"
                "Use the built-in image_gen.imagegen tool exactly once now and follow "
                "$imagegen. Do not merely describe the generation.\n"
                "Asset type: %s\n"
                "After generation, copy the selected real image bytes from the built-in "
                "tool's default output path to exactly: %s\n"
                "Verify that the copied file exists before finishing.\n"
                "Do not create placeholders, gradients, text stand-ins, or alternative filenames.\n"
                "%s"
                "Generation prompt:\n%s"
                % (
                    request.asset_type,
                    target.name,
                    reference_instruction,
                    request.prompt,
                )
            )
            try:
                completed = subprocess.run(
                    self.build_command(job_dir, request.reference_paths),
                    input=instruction,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ImageGenerationError(
                    "Codex image generation timed out after %d seconds" % self.timeout_sec
                )
            if completed.returncode != 0:
                raise ImageGenerationError(
                    completed.stderr.strip() or "Codex image generation failed"
                )
            try:
                mime = validate_image(staged_output)
            except (FileNotFoundError, ValueError) as error:
                detail = self._last_agent_message(completed.stdout)
                if detail:
                    raise ImageGenerationError(
                        "Codex did not return a valid project image: %s; last response: %s"
                        % (error, detail)
                    )
                raise ImageGenerationError(
                    "Codex did not return a valid project image: %s" % error
                )
            if mime != "image/png":
                raise ImageGenerationError("Codex image output is not a real PNG")
            os.replace(str(staged_output), str(target))
        return {
            "path": str(target),
            "mime": mime,
            "size": target.stat().st_size,
            "provider": "codex_imagegen",
            "asset_type": request.asset_type,
        }

    @staticmethod
    def _last_agent_message(stdout: str) -> str:
        messages = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append(text.strip())
        return messages[-1] if messages else ""


class JimengImageRunner:
    def __init__(
        self,
        executable: Optional[str] = None,
        args_template: Optional[Sequence[str]] = None,
        timeout_sec: int = 900,
    ):
        self.executable = executable or os.environ.get("JIMENG_IMAGE_CLI")
        if args_template is None:
            encoded = os.environ.get("JIMENG_IMAGE_ARGS_JSON", "")
            if encoded:
                try:
                    args_template = json.loads(encoded)
                except json.JSONDecodeError:
                    raise ImageProviderUnavailable("JIMENG_IMAGE_ARGS_JSON is invalid JSON")
        self.args_template = tuple(args_template or ())
        self.timeout_sec = timeout_sec

    def health_check(self) -> Dict[str, str]:
        if not self.executable or not Path(self.executable).is_file():
            raise ImageProviderUnavailable("Jimeng image CLI is not configured")
        if not os.access(self.executable, os.X_OK):
            raise ImageProviderUnavailable("Jimeng image CLI is not executable")
        if not self.args_template:
            raise ImageProviderUnavailable("Jimeng image argument template is not configured")
        joined = "\0".join(self.args_template)
        for required in ("{prompt_file}", "{output}", "{asset_type}"):
            if required not in joined:
                raise ImageProviderUnavailable("Jimeng image template is missing %s" % required)
        return {"provider": "jimeng_image_cli", "executable": self.executable}

    def _command(self, prompt_file: Path, output: Path, asset_type: str) -> Sequence[str]:
        self.health_check()
        values = {
            "prompt_file": str(prompt_file),
            "output": str(output),
            "asset_type": asset_type,
        }
        try:
            arguments = [argument.format(**values) for argument in self.args_template]
        except KeyError as error:
            raise ImageProviderUnavailable("unknown Jimeng image template field: %s" % error)
        return [self.executable] + arguments

    def run(self, request: ImageRequest, authorized: bool = False) -> Dict[str, object]:
        if not authorized:
            raise ProviderCallNotAuthorized(
                "image generation requires an explicit current user action"
            )
        request.validate()
        target = Path(request.target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        prompt_handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="jimeng-prompt-", suffix=".txt", delete=False
        )
        output_handle = tempfile.NamedTemporaryFile(
            prefix=".jimeng-image-", suffix=".png", dir=str(target.parent), delete=False
        )
        prompt_path = Path(prompt_handle.name)
        staged_output = Path(output_handle.name)
        prompt_handle.close()
        output_handle.close()
        staged_output.unlink()
        try:
            prompt_path.write_text(request.prompt, encoding="utf-8")
            completed = subprocess.run(
                self._command(prompt_path, staged_output, request.asset_type),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
            if completed.returncode != 0:
                raise ImageGenerationError(
                    completed.stderr.strip() or "Jimeng image generation failed"
                )
            mime = validate_image(staged_output)
            if mime != "image/png":
                raise ImageGenerationError("Jimeng image output is not a real PNG")
            os.replace(str(staged_output), str(target))
        except subprocess.TimeoutExpired:
            raise ImageGenerationError(
                "Jimeng image generation timed out after %d seconds" % self.timeout_sec
            )
        finally:
            try:
                prompt_path.unlink()
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
            "provider": "jimeng_image_cli",
            "asset_type": request.asset_type,
        }
