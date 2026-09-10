import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "backend" / "skills" / "stages"


class DesignSheetSkillContractTests(unittest.TestCase):
    def _read_skill(self, name: str) -> str:
        return (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")

    def test_character_skill_requires_hierarchical_multi_view_identity_board(self):
        skill = self._read_skill("design-characters")

        for required in (
            "exactly one stable identity",
            "dominant neutral three-quarter full-body identity anchor",
            "at least two body angles in total",
            "smaller subordinate body view",
            "side profile",
            "rear three-quarter",
            "neutral face close-up",
            "same anatomical or costume side",
        ):
            self.assertIn(required, skill)

        self.assertIn("not a cast, group, relative, duplicate", skill)
        self.assertIn("row of equal-sized front/side/back figures", skill)
        self.assertIn("the board has only one body angle", skill)
        self.assertIn("装饰物细节图按角色实际需要添加", skill)
        self.assertIn("not a universal review requirement", skill)
        self.assertIn("not, by itself, a review failure", skill)

    def test_scene_skill_requires_establishing_plan_and_axis_consistency(self):
        skill = self._read_skill("design-visual-assets")

        for required in (
            "exactly one physical environment",
            "dominant establishing view",
            "overhead plan/blocking inset",
            "primary entrance",
            "camera-safe side of the axis",
            "fixed screen-left/screen-right landmarks",
            "light origin",
            "shadow fall",
            "small reverse or oblique geometry-proof angle",
        ):
            self.assertIn(required, skill)

        self.assertIn("not several similar rooms", skill)
        self.assertIn("or a chronological storyboard", skill)
        self.assertIn("mirrors or reverses the production axis", skill)

    def test_asset_skills_obey_confirmed_reuse_and_character_weapon_authority(self):
        planning = self._read_skill("plan-visual-assets")
        characters = self._read_skill("design-characters")
        visuals = self._read_skill("design-visual-assets")

        self.assertIn("已验证可复用资产目录", planning)
        self.assertIn("角色定妆板确实没有该物件", planning)
        self.assertIn("decision=reuse", characters)
        self.assertIn("weapon and signature carried equipment", characters)
        self.assertIn("decision=reuse", visuals)
        self.assertIn("角色定妆板已包含的武器", visuals)


if __name__ == "__main__":
    unittest.main()
