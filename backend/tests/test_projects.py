import tempfile
import unittest
from pathlib import Path

from backend.core.projects import Workspace
from backend.core.status import make_creative_brief


class WorkspaceTests(unittest.TestCase):
    def test_project_and_episode_initialization_use_expected_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            workspace.create_project("paperplane", "纸飞机")
            status = workspace.create_episode(
                "paperplane",
                "ep01",
                make_creative_brief("雨夜的纸飞机", "逆风返回"),
            )
            self.assertTrue(workspace.episode_dir("paperplane", "ep01").is_dir())
            self.assertTrue(workspace.statuses.path_for("paperplane", "ep01").is_file())
            self.assertTrue(workspace.events.path_for("paperplane", "ep01").is_file())
            self.assertEqual(status["run"]["current_stage"], "story_design")

    def test_episode_requires_an_existing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            with self.assertRaises(FileNotFoundError):
                workspace.create_episode(
                    "missing",
                    "ep01",
                    make_creative_brief("主题", "钩子"),
                )

    def test_provided_script_is_saved_as_an_immutable_stage_one_input(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            workspace.create_project("paperplane", "纸飞机")
            script = "# 原剧本\n\n女孩：不要改我。\n"
            status = workspace.create_episode(
                "paperplane",
                "ep01",
                make_creative_brief("雨夜的纸飞机", "逆风返回", provided_script=script),
            )
            source = workspace.episode_dir("paperplane", "ep01") / "inputs" / "original_script.md"
            self.assertEqual(source.read_text(encoding="utf-8"), script)
            self.assertEqual(status["script_lock"]["state"], "locked")
            self.assertEqual(status["script_lock"]["source_path"], "inputs/original_script.md")

    def test_invalid_episode_name_does_not_leave_a_partial_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            workspace.create_project("paperplane", "纸飞机")
            with self.assertRaisesRegex(ValueError, "episode name"):
                workspace.create_episode(
                    "paperplane",
                    "ep01",
                    make_creative_brief("主题", "钩子"),
                    episode_name="   ",
                )
            self.assertFalse(workspace.episode_dir("paperplane", "ep01").exists())


if __name__ == "__main__":
    unittest.main()
