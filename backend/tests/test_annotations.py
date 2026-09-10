import tempfile
import unittest
from pathlib import Path

from backend.core.annotations import AnnotationStore
from backend.core.projects import Workspace
from backend.core.status import make_creative_brief


class AnnotationStoreTests(unittest.TestCase):
    def test_incremental_cursor_avoids_loading_handled_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            workspace.create_project("paperplane", "纸飞机")
            workspace.create_episode(
                "paperplane",
                "ep01",
                make_creative_brief("雨夜纸飞机", "逆风返回"),
            )
            annotations = AnnotationStore(root)
            first = annotations.add(
                "paperplane", "ep01", "story_design", "加强结尾兑现"
            )
            second = annotations.add(
                "paperplane", "ep01", "character_design", "角色雨衣颜色不一致"
            )
            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            self.assertEqual(len(annotations.list("paperplane", "ep01", pending_only=True)), 2)

            summary = annotations.mark_handled("paperplane", "ep01", 1)

            self.assertEqual(summary["handled_revision"], 1)
            self.assertEqual(summary["pending_count"], 1)
            pending = annotations.list("paperplane", "ep01", pending_only=True)
            self.assertEqual([item["id"] for item in pending], ["a_000002"])
            all_annotations = annotations.list("paperplane", "ep01")
            self.assertEqual(all_annotations[0]["status"], "resolved")
            self.assertEqual(all_annotations[1]["status"], "open")

            summary = annotations.mark_handled("paperplane", "ep01")
            self.assertEqual(summary["state"], "clear")
            self.assertEqual(summary["pending_count"], 0)

    def test_redo_is_typed_and_cannot_be_silently_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            workspace.create_project("paperplane", "纸飞机")
            workspace.create_episode(
                "paperplane",
                "ep01",
                make_creative_brief("雨夜纸飞机", "逆风返回"),
            )
            annotations = AnnotationStore(root)
            redo = annotations.add(
                "paperplane",
                "ep01",
                "story_design",
                "结尾必须重做",
                annotation_type="redo",
            )

            status = workspace.statuses.load("paperplane", "ep01")
            self.assertEqual(redo["type"], "redo")
            self.assertEqual(status["annotations"]["redo_count"], 1)
            self.assertEqual(status["tasks"]["story_design:story_design"]["state"], "redo_requested")
            with self.assertRaisesRegex(ValueError, "completed repair plan"):
                annotations.mark_handled("paperplane", "ep01")


if __name__ == "__main__":
    unittest.main()
