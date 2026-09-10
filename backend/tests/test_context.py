import tempfile
import unittest
from pathlib import Path

from backend.core.assets import register_asset
from backend.core.status import make_creative_brief, new_status
from backend.workflow.context import ContextBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ContextBuilderTests(unittest.TestCase):
    def _register(self, status, root, stage, name, kind="document", role=None):
        path = root / name
        if not path.exists():
            path.write_text(name, encoding="utf-8")
        register_asset(
            status,
            root,
            "%s.%s" % (stage, name),
            stage,
            kind,
            role or ("video_prompt" if kind == "prompt" else "document"),
            name,
            name,
        )

    def test_story_design_locks_complete_user_script_to_mechanical_split(self):
        script = "总时长：10S\n镜头1｜1.5S｜美女进入\n镜头2｜8.5S｜悟空挥棒\n"
        status = new_status(
            "p", "e", make_creative_brief("反诈", "七十二变", provided_script=script, target_duration_sec=10)
        )
        context = ContextBuilder(PROJECT_ROOT).build("story_design", status, Path(tempfile.mkdtemp()))
        self.assertIn("script.md 必须逐字一致", context.prompt)
        self.assertIn("只允许按视频 Provider 的生成单元机械拆分", context.prompt)
        self.assertIn("不得评判、润色、扩写", context.prompt)
        self.assertIn("Uxx-yy 内部分镜", context.prompt)
        self.assertNotIn("改写为连续叙述式镜头节奏", context.prompt)
        self.assertIn(script, context.prompt)

    def test_asset_planning_reads_locked_prompts_and_does_not_write_design_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = new_status("p", "e", make_creative_brief("反诈", "七十二变", target_duration_sec=10))
            status["creative_brief"]["shot_plan"] = {
                "state": "resolved", "shot_count": 1, "durations_sec": [10], "reason": "一个生成单元"
            }
            for name, kind in (
                ("script.md", "document"),
                ("storyboard.md", "document"),
                ("shot01_prompt_video.txt", "prompt"),
                ("shot01_prompt_storyboard.txt", "prompt"),
            ):
                self._register(status, root, "story_design", name, kind)
            context = ContextBuilder(PROJECT_ROOT).build("asset_planning", status, root)
            self.assertEqual(
                {path.name for path in context.loaded_text_paths},
                {"script.md", "storyboard.md", "shot01_prompt_video.txt", "shot01_prompt_storyboard.txt"},
            )
            self.assertIn("不得生成定妆 Prompt", context.prompt)

    def test_character_design_requires_asset_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = new_status("p", "e", make_creative_brief("反诈", "七十二变"))
            for name in ("script.md", "directing.md"):
                self._register(status, root, "story_design", name)
            with self.assertRaisesRegex(FileNotFoundError, "asset_plan.json"):
                ContextBuilder(PROJECT_ROOT).build("character_design", status, root)

    def test_context_has_no_removed_final_prompt_stage(self):
        status = new_status("p", "e", make_creative_brief("反诈", "七十二变"))
        with self.assertRaisesRegex(ValueError, "unknown stage"):
            ContextBuilder(PROJECT_ROOT).build("final_video_prompts", status, Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
