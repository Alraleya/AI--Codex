import tempfile
import unittest
import inspect
from pathlib import Path

from backend.core.assets import register_asset
from backend.core.status import make_creative_brief, new_status
from backend.workflow.chain import (
    approve_video_shot,
    compute_video_shot_fingerprint,
    flow_next,
    invalidate_from,
    is_video_shot_confirmation_valid,
    skip_stage,
)
from backend.workflow.contracts import validate_output_names
from backend.workflow.runner import ChainRunner, MAX_VIDEO_CONCURRENCY


class NewWorkflowTests(unittest.TestCase):
    def _resolved_brief(self, *, direct=False):
        brief = make_creative_brief(
            "反诈", "七十二变", target_duration_sec=10,
            production_mode="dialogue_direct" if direct else "storyboard",
            style="写实",
            aspect_ratio="16:9",
            video_model="fast",
        )
        brief["shot_plan"] = {
            "state": "resolved", "shot_count": 1, "durations_sec": [10], "reason": "一个生成单元"
        }
        brief["storyboard_plan"] = (
            {"state": "pending", "shots": []}
            if direct else
            {
                "state": "resolved",
                "shots": [{
                    "shot_number": 1, "columns": 2, "rows": 2, "panel_count": 4,
                    "timepoints_sec": [0, 3, 6, 10], "reason": "执行状态",
                    "storyboard_required": True,
                }],
            }
        )
        return brief

    def test_story_outputs_include_locked_provider_and_storyboard_prompts(self):
        brief = self._resolved_brief()
        valid = [
            "outline.md", "directing.md", "script.md", "storyboard.md",
            "shot01_prompt_video.txt", "shot01_prompt_storyboard.txt",
        ]
        self.assertEqual(validate_output_names("story_design", valid, brief), valid)

    def test_flow_orders_planning_before_design_and_binding_after_storyboard(self):
        status = new_status("p", "e", self._resolved_brief())
        status["stages"]["story_design"]["state"] = "done"
        status["stages"]["story_design"]["handoff"] = {"summary": "锁定", "outputs": []}
        self.assertEqual(flow_next(status).stage_id, "asset_planning")
        for stage_id in ("asset_planning", "character_design", "visual_design"):
            status["stages"][stage_id]["state"] = "done"
            status["stages"][stage_id]["handoff"] = {"summary": "完成", "outputs": []}
        self.assertEqual(flow_next(status).stage_id, "storyboard_binding")
        status["stages"]["storyboard_binding"]["state"] = "done"
        status["stages"]["storyboard_binding"]["handoff"] = {"summary": "绑定", "outputs": []}
        self.assertEqual(flow_next(status).stage_id, "storyboard_generation")

    def test_dialogue_direct_skips_only_storyboard_binding_and_generation(self):
        status = new_status("p", "e", self._resolved_brief(direct=True))
        for stage_id in ("story_design", "asset_planning", "character_design", "visual_design"):
            status["stages"][stage_id]["state"] = "done"
            status["stages"][stage_id]["handoff"] = {"summary": "完成", "outputs": []}
        self.assertEqual(flow_next(status).stage_id, "storyboard_binding")
        self.assertEqual(flow_next(status).action, "skip")
        skip_stage(status, "storyboard_binding", "无故事板")
        self.assertEqual(flow_next(status).stage_id, "storyboard_generation")
        self.assertEqual(flow_next(status).action, "skip")
        skip_stage(status, "storyboard_generation", "无故事板")
        self.assertEqual(flow_next(status).stage_id, "video_binding")

    def test_visual_design_invalidation_keeps_locked_story_and_prompts(self):
        status = new_status("p", "e", self._resolved_brief())
        for stage in status["stages"].values():
            stage["state"] = "done"
        affected = invalidate_from(status, "visual_design")
        self.assertEqual(affected[0], "visual_design")
        self.assertEqual(status["stages"]["story_design"]["state"], "done")
        self.assertEqual(status["creative_brief"]["shot_plan"]["state"], "resolved")
        self.assertEqual(status["creative_brief"]["storyboard_plan"]["state"], "resolved")

    def test_video_approval_is_bound_to_first_stage_prompt_and_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = new_status("p", "e", self._resolved_brief())
            status["creative_brief"]["video_session_id"] = "10001"
            prompt = root / "shot01_prompt_video.txt"
            binding = root / "shot01_video_references.json"
            prompt.write_text("锁定 Prompt A", encoding="utf-8")
            binding.write_text("{}", encoding="utf-8")
            register_asset(status, root, "prompt", "story_design", "prompt", "video_prompt", "视频Prompt", prompt.name)
            register_asset(status, root, "binding", "video_binding", "document", "reference_binding", "绑定", binding.name)
            for stage_id in status["stages"]:
                if stage_id == "video_generation":
                    break
                status["stages"][stage_id]["state"] = "done"
            approve_video_shot(status, root, 1)
            self.assertTrue(is_video_shot_confirmation_valid(status, root, 1))
            first = compute_video_shot_fingerprint(status, root, 1)
            prompt.write_text("锁定 Prompt B", encoding="utf-8")
            self.assertFalse(is_video_shot_confirmation_valid(status, root, 1))
            self.assertNotEqual(first, compute_video_shot_fingerprint(status, root, 1))

    def test_video_batch_allows_up_to_two_concurrent_units(self):
        default = inspect.signature(ChainRunner.confirm_video_shots_batch).parameters[
            "max_concurrency"
        ].default
        self.assertEqual(MAX_VIDEO_CONCURRENCY, 2)
        self.assertEqual(default, 2)
        with self.assertRaisesRegex(ValueError, "between 1 and 2"):
            ChainRunner.confirm_video_shots_batch(
                object(), "p", "e", [1], max_concurrency=3, authorized=True
            )


if __name__ == "__main__":
    unittest.main()
