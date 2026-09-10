import tempfile
import unittest
from pathlib import Path

from backend.core.assets import register_asset
from backend.core.status import make_creative_brief, new_status
from backend.core.storage import organize_completed_episode, restore_intermediate_storage


class StorageLayoutTests(unittest.TestCase):
    def test_completed_episode_separates_intermediate_files_and_can_restore_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = new_status(
                "paperplane",
                "ep01",
                make_creative_brief("雨夜的纸飞机", "逆风返回"),
            )
            status["run"]["state"] = "done"

            final_path = root / "script.md"
            final_path.write_text("final script", encoding="utf-8")
            register_asset(
                status,
                root,
                "story.script",
                "story_design",
                "document",
                "story_document",
                "剧本",
                final_path.name,
            )

            prompt_path = root / "char_girl_prompt.txt"
            prompt_path.write_text("prompt", encoding="utf-8")
            register_asset(
                status,
                root,
                "character.prompt",
                "character_design",
                "prompt",
                "character_prompt",
                "女孩 Prompt",
                prompt_path.name,
            )

            (root / "agent_result_character_design.json").write_text(
                "{}", encoding="utf-8"
            )
            package_dir = root / "storyboard_reference_packages"
            package_dir.mkdir()
            (package_dir / "shot01.png").write_bytes(b"reference")

            self.assertTrue(organize_completed_episode(status, root))
            self.assertTrue(final_path.is_file())
            self.assertFalse(prompt_path.exists())
            self.assertTrue(
                (root / "intermediate/character_design/char_girl_prompt.txt").is_file()
            )
            self.assertTrue(
                (root / "intermediate/run_metadata/agent_result_character_design.json").is_file()
            )
            self.assertTrue(
                (root / "intermediate/reference_packages/shot01.png").is_file()
            )
            self.assertEqual(
                status["assets"]["character.prompt"]["path"],
                "intermediate/character_design/char_girl_prompt.txt",
            )

            self.assertTrue(restore_intermediate_storage(status, root))
            self.assertTrue(prompt_path.is_file())
            self.assertFalse(
                (root / "intermediate/character_design/char_girl_prompt.txt").exists()
            )
            self.assertEqual(status["assets"]["character.prompt"]["path"], prompt_path.name)


if __name__ == "__main__":
    unittest.main()
