import tempfile
import unittest
from pathlib import Path

from backend.core.annotations import AnnotationStore
from backend.core.assets import register_asset
from backend.core.lock import EpisodeLock
from backend.core.projects import Workspace
from backend.core.repairs import RepairPlanStore
from backend.core.status import make_creative_brief


class RepairPlanTests(unittest.TestCase):
    def test_plan_tracks_watermark_tasks_and_resolves_only_included_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            workspace.create_project("repair", "修复")
            workspace.create_episode(
                "repair",
                "ep01",
                make_creative_brief("号码牌", "修复错误号码牌"),
            )
            episode_dir = workspace.episode_dir("repair", "ep01")
            (episode_dir / "shot01_storyboard.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"x" * 2048
            )
            status = workspace.statuses.load("repair", "ep01")
            register_asset(
                status,
                episode_dir,
                "storyboard.01",
                "storyboard_generation",
                "image",
                "storyboard_sheet",
                "镜头 01",
                "shot01_storyboard.png",
                task_id="shot01",
            )
            workspace.statuses.save(status)

            annotations = AnnotationStore(root)
            redo = annotations.add(
                "repair",
                "ep01",
                "storyboard_generation",
                "号码牌改为 A286",
                annotation_type="redo",
                asset_id="storyboard.01",
            )
            repairs = RepairPlanStore(root)
            plan = repairs.create(
                "repair",
                "ep01",
                "storyboard_generation",
                [
                    {
                        "task_id": "shot01",
                        "asset_id": "storyboard.01",
                        "instruction": "保持构图，只修号码牌。",
                    }
                ],
                annotation_ids=[redo["id"]],
                source="annotation",
            )
            late = annotations.add(
                "repair", "ep01", "storyboard_generation", "表情可以更夸张"
            )
            self.assertEqual(plan["annotation_watermark"], 1)
            self.assertEqual(annotations.list("repair", "ep01")[0]["status"], "planned")

            with EpisodeLock(root, "repair", "ep01"):
                status = workspace.statuses.load("repair", "ep01")
                plan = repairs.load("repair", "ep01", plan["id"])
                repairs.begin_locked(status, plan)
                repairs.set_target_state_locked(status, plan, "shot01", "running")
                repairs.set_target_state_locked(
                    status,
                    plan,
                    "shot01",
                    "verifying",
                    result_asset_id="storyboard.01",
                )
                repairs.set_target_state_locked(
                    status,
                    plan,
                    "shot01",
                    "passed",
                    result_asset_id="storyboard.01",
                )
                repairs.complete_locked(status, plan, reason="新版本审查通过")
                workspace.statuses.save(status)

            all_annotations = annotations.list("repair", "ep01")
            self.assertEqual(all_annotations[0]["status"], "resolved")
            self.assertEqual(all_annotations[1]["id"], late["id"])
            self.assertEqual(all_annotations[1]["status"], "open")
            status = workspace.statuses.load("repair", "ep01")
            self.assertEqual(status["annotations"]["handled_revision"], 1)
            self.assertEqual(status["annotations"]["pending_count"], 1)
            self.assertEqual(status["repairs"]["state"], "clear")
            self.assertEqual(
                status["tasks"]["storyboard_generation:shot01"]["state"], "passed"
            )


if __name__ == "__main__":
    unittest.main()
