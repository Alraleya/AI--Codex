import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.authorization import ProviderCallNotAuthorized
from backend.runners.image import (
    CodexImagegenRunner,
    ImageGenerationError,
    ImageProviderUnavailable,
    ImageRequest,
    JimengImageRunner,
)
from backend.runners.video import MAX_VIDEO_REFERENCES, JimengVideoRunner, VideoRequest


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


class _HealthCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ImageProviderTests(unittest.TestCase):
    @patch("backend.runners.image.subprocess.run")
    def test_codex_imagegen_health_checks_login_and_builtin_feature(self, run):
        run.side_effect = [
            _HealthCompleted("codex-cli 1.2.3\n"),
            _HealthCompleted("Logged in using ChatGPT\n"),
            _HealthCompleted("image_generation stable true\n"),
        ]
        result = CodexImagegenRunner(executable="/opt/bin/codex").health_check()
        self.assertEqual(result["feature"], "image_generation")
        self.assertEqual(run.call_count, 3)

    @patch("backend.runners.image.subprocess.run")
    def test_codex_imagegen_health_rejects_disabled_builtin_feature(self, run):
        run.side_effect = [
            _HealthCompleted("codex-cli 1.2.3\n"),
            _HealthCompleted("Logged in using ChatGPT\n"),
            _HealthCompleted("image_generation stable false\n"),
        ]
        with self.assertRaisesRegex(ImageProviderUnavailable, "feature is unavailable"):
            CodexImagegenRunner(executable="/opt/bin/codex").health_check()

    @patch("backend.runners.image.subprocess.run")
    def test_jimeng_uses_temp_prompt_list_args_and_validates_png(self, run):
        observed_prompt = []

        def generate(command, **kwargs):
            self.assertIsInstance(command, list)
            self.assertNotIn("shell", kwargs)
            prompt_path = Path(command[command.index("--prompt") + 1])
            output_path = Path(command[command.index("--output") + 1])
            observed_prompt.append((prompt_path, prompt_path.read_text(encoding="utf-8")))
            output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
            return _Completed()

        run.side_effect = generate
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "char_girl_sheet.png"
            runner = JimengImageRunner(
                executable="/bin/echo",
                args_template=(
                    "--prompt",
                    "{prompt_file}",
                    "--output",
                    "{output}",
                    "--type",
                    "{asset_type}",
                ),
            )
            result = runner.run(
                ImageRequest("真实角色定妆", target, "character_sheet"), authorized=True
            )
            self.assertEqual(result["mime"], "image/png")
            self.assertTrue(target.is_file())
            self.assertEqual(observed_prompt[0][1], "真实角色定妆")
            self.assertFalse(observed_prompt[0][0].exists())

    @patch("backend.runners.image.subprocess.run")
    def test_codex_imagegen_is_staged_outside_episode_and_requires_authorization(self, run):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shot01_storyboard.png"
            runner = CodexImagegenRunner(executable="/opt/bin/codex")
            request = ImageRequest("雨夜纸飞机", target, "storyboard")
            with self.assertRaises(ProviderCallNotAuthorized):
                runner.run(request)
            run.assert_not_called()

            def generate(command, **kwargs):
                job_dir = Path(command[command.index("--cd") + 1])
                self.assertNotEqual(job_dir, target.parent)
                instruction = kwargs["input"]
                self.assertIn("explicitly authorizes this paid image call", instruction)
                self.assertIn("image_gen.imagegen tool exactly once", instruction)
                self.assertIn("copy the selected real image bytes", instruction)
                (job_dir / target.name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
                return _Completed()

            run.side_effect = generate
            result = runner.run(request, authorized=True)
            self.assertEqual(result["provider"], "codex_imagegen")
            self.assertTrue(target.is_file())

    @patch("backend.runners.image.subprocess.run")
    def test_codex_imagegen_names_local_reference_paths_in_tool_instruction(self, run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "prop_staff_sheet.png"
            reference = root / "char_monk_sheet.png"
            reference.write_bytes(b"\x89PNG\r\n\x1a\n" + b"r" * 2048)

            def generate(command, **kwargs):
                instruction = kwargs["input"]
                self.assertIn(str(reference.resolve()), instruction)
                self.assertIn("referenced_image_paths", instruction)
                self.assertIn("Do not use recent-conversation image counting", instruction)
                job_dir = Path(command[command.index("--cd") + 1])
                (job_dir / target.name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
                return _Completed()

            run.side_effect = generate
            result = CodexImagegenRunner(executable="/opt/bin/codex").run(
                ImageRequest(
                    "月牙铲道具定妆",
                    target,
                    "prop_sheet",
                    reference_paths=(reference,),
                ),
                authorized=True,
            )
            self.assertEqual(result["provider"], "codex_imagegen")
            self.assertTrue(target.is_file())

    @patch("backend.runners.image.subprocess.run")
    def test_codex_imagegen_surfaces_last_agent_message_when_file_is_missing(self, run):
        completed = _Completed()
        completed.stdout = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"image tool was unavailable"}}\n'
        )
        run.return_value = completed
        with tempfile.TemporaryDirectory() as directory:
            runner = CodexImagegenRunner(executable="/opt/bin/codex")
            request = ImageRequest(
                "雨夜纸飞机", Path(directory) / "shot01_storyboard.png", "storyboard"
            )
            with self.assertRaisesRegex(ImageGenerationError, "image tool was unavailable"):
                runner.run(request, authorized=True)


class VideoProviderTests(unittest.TestCase):
    def test_video_request_allows_nine_references_but_not_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            references = []
            for index in range(MAX_VIDEO_REFERENCES + 1):
                path = root / ("reference%02d.png" % index)
                path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"r" * 2048)
                references.append(path)

            accepted = VideoRequest(
                "锁定的视频 Prompt",
                root / "shot01_video_jimeng_v1.mp4",
                7,
                "16:9",
                reference_paths=tuple(references[:MAX_VIDEO_REFERENCES]),
            )
            accepted.validate()

            rejected = VideoRequest(
                "锁定的视频 Prompt",
                root / "shot01_video_jimeng_v1.mp4",
                7,
                "16:9",
                reference_paths=tuple(references),
            )
            with self.assertRaisesRegex(ValueError, "at most nine references"):
                rejected.validate()

    @patch("backend.runners.video.subprocess.run")
    def test_video_call_requires_current_revision_confirmation(self, run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JimengVideoRunner(
                executable="/bin/echo",
                args_template=(
                    "--prompt", "{prompt_file}",
                    "--references", "{references_file}",
                    "--duration", "{duration_sec}",
                    "--ratio", "{aspect_ratio}",
                    "--model-version", "{model_version}",
                    "--output", "{output}",
                ),
            )
            board = root / "shot01_storyboard.png"
            board.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
            prompt = "锁定的视频 Prompt"
            request = VideoRequest(
                prompt, root / "shot01_video_jimeng_v1.mp4", 7, "16:9",
                reference_paths=(board,),
            )
            status = {
                "confirmations": {
                    "video_generation": {
                        "state": "required",
                        "revision_fingerprint": None,
                    }
                },
                "creative_brief": {
                    "video_model": "fast",
                    "video_session_id": "10001",
                    "target_duration_sec": 7,
                    "shot_plan": {
                        "state": "resolved",
                        "shot_count": 1,
                        "durations_sec": [7],
                        "reason": "单镜测试",
                    },
                },
                "assets": {},
            }
            with self.assertRaises(ProviderCallNotAuthorized):
                runner.run(request, status, root, authorized=True)
            run.assert_not_called()

    @patch("backend.runners.video.validate_video")
    @patch("backend.runners.video.is_video_confirmation_valid", return_value=True)
    @patch("backend.runners.video.is_video_shot_confirmation_valid", return_value=True)
    @patch("backend.runners.video.subprocess.run")
    def test_video_submits_only_provider_payload_and_ordered_references(
        self, run, _shot_confirmation, _confirmation, validate_video
    ):
        observed = {}

        def generate(command, **kwargs):
            prompt_path = Path(command[command.index("--prompt") + 1])
            references_path = Path(command[command.index("--references") + 1])
            output_path = Path(command[command.index("--output") + 1])
            observed["prompt"] = prompt_path.read_text(encoding="utf-8")
            observed["references"] = json.loads(
                references_path.read_text(encoding="utf-8")
            )
            observed["duration"] = command[command.index("--duration") + 1]
            observed["ratio"] = command[command.index("--ratio") + 1]
            output_path.write_bytes(b"v" * 2048)
            return _Completed()

        run.side_effect = generate
        validate_video.return_value = (
            "video/mp4",
            {"duration_sec": 7, "width": 1920, "height": 1080, "codec": "h264"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = root / "shot01_storyboard.png"
            board.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
            provider_payload = (
                "参考图片1中的动作顺序。单一连续镜头，女孩先抬头，随后伸手，最后站稳。"
            )
            runner = JimengVideoRunner(
                executable="/bin/echo",
                args_template=(
                    "--prompt", "{prompt_file}",
                    "--references", "{references_file}",
                    "--duration", "{duration_sec}",
                    "--ratio", "{aspect_ratio}",
                    "--model-version", "{model_version}",
                    "--session", "{session_id}",
                    "--output", "{output}",
                ),
            )
            target = root / "shot01_video_jimeng_v1.mp4"
            runner.run(
                VideoRequest(
                    provider_payload,
                    target,
                    7,
                    "16:9",
                    reference_paths=(board,),
                ),
                {
                    "confirmations": {},
                    "creative_brief": {
                        "video_model": "fast",
                        "video_session_id": "10001",
                        "target_duration_sec": 7,
                        "shot_plan": {
                            "state": "resolved",
                            "shot_count": 1,
                            "durations_sec": [7],
                            "reason": "单镜测试",
                        },
                    },
                    "assets": {},
                },
                root,
                authorized=True,
            )
            self.assertEqual(observed["prompt"], provider_payload)
            self.assertEqual(observed["references"], [str(board.resolve())])
            self.assertEqual(observed["duration"], "7")
            self.assertEqual(observed["ratio"], "16:9")
            self.assertNotIn(str(board.resolve()), observed["prompt"])
            self.assertNotIn("P01", observed["prompt"])


if __name__ == "__main__":
    unittest.main()
