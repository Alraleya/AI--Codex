import unittest

from backend.workbench_compat import project_meituan_status


class WorkbenchCompatibilityTests(unittest.TestCase):
    def test_retained_meituan_episode_projects_to_v2_viewer_contract(self):
        raw = {
            "schema_version": "1.0",
            "flow_id": "manga_episode",
            "flow_version": "1.0",
            "project_id": "meituan_sunce",
            "episode_id": "s01e01",
            "run": {"state": "running", "current_stage": "storyboard_generation"},
            "stages": {
                "story_design": {"state": "done", "outputs": ["script.md"]},
                "prompts": {"state": "done", "outputs": ["shot01_prompt_video.txt"]},
                "scene_design": {"state": "done", "outputs": ["scene_street_sheet.png"]},
                "prop_design": {"state": "done", "outputs": ["prop_bike_sheet.png"]},
                "storyboard_generation": {"state": "running", "outputs": []},
            },
            "tasks": {},
            "assets": {
                "prompt": {
                    "stage": "prompts",
                    "task_id": "prompts",
                    "path": "shot01_prompt_video.txt",
                    "metadata": {},
                },
                "scene": {
                    "stage": "scene_design",
                    "task_id": "scene_street",
                    "path": "scene_street_sheet.png",
                    "metadata": {},
                },
            },
        }

        projected = project_meituan_status(raw)

        self.assertEqual(projected["flow_version"], "2.0")
        self.assertEqual(len(projected["stages"]), 9)
        self.assertEqual(projected["stages"]["asset_planning"]["state"], "skipped")
        self.assertEqual(projected["assets"]["prompt"]["stage"], "story_design")
        self.assertEqual(projected["assets"]["prompt"]["task_id"], "shot01")
        self.assertEqual(projected["assets"]["scene"]["stage"], "visual_design")
        self.assertTrue(projected["workbench_compatibility"]["read_only"])

    def test_other_v1_episode_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "美团孙策"):
            project_meituan_status({
                "flow_version": "1.0",
                "project_id": "other",
                "episode_id": "s01e01",
            })


if __name__ == "__main__":
    unittest.main()
