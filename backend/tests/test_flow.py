import unittest

from backend.core.flow import (
    STAGE_IDS,
    STAGES,
    StageDefinition,
    transitive_downstream,
    validate_flow,
)


class FlowDefinitionTests(unittest.TestCase):
    def test_flow_has_the_nine_product_stages(self):
        self.assertEqual(len(STAGES), 9)
        self.assertEqual(
            STAGE_IDS,
            (
                "story_design",
                "asset_planning",
                "character_design",
                "visual_design",
                "storyboard_binding",
                "storyboard_generation",
                "video_binding",
                "video_generation",
                "edit_post",
            ),
        )

    def test_model_stages_load_one_owner_skill_and_provider_stages_load_none(self):
        self.assertEqual(STAGES[0].skills, ("design-episode",))
        self.assertEqual(STAGES[1].skills, ("plan-visual-assets",))
        self.assertEqual(STAGES[2].skills, ("design-characters",))
        self.assertEqual(STAGES[3].skills, ("design-visual-assets",))
        self.assertEqual(STAGES[4].skills, ())
        self.assertEqual(STAGES[5].skills, ())
        self.assertEqual(STAGES[6].skills, ())
        self.assertEqual(STAGES[7].skills, ())
        self.assertEqual(STAGES[8].skills, ("plan-edit",))

    def test_flow_rejects_duplicate_and_forward_dependencies(self):
        duplicate = (
            StageDefinition("a", "A", (), ()),
            StageDefinition("a", "Again", (), ()),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_flow(duplicate)

        forward = (
            StageDefinition("a", "A", ("b",), ()),
            StageDefinition("b", "B", (), ()),
        )
        with self.assertRaisesRegex(ValueError, "downstream"):
            validate_flow(forward)

    def test_transitive_downstream_follows_the_real_flow(self):
        affected = transitive_downstream("storyboard_binding")
        self.assertEqual(affected[0], "storyboard_binding")
        self.assertEqual(affected[-1], "edit_post")
        self.assertNotIn("visual_design", affected)


if __name__ == "__main__":
    unittest.main()
