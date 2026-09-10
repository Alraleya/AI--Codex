import base64
import json
import tempfile
import unittest
from pathlib import Path

from backend.core.assets import register_asset
from backend.core.status import make_creative_brief, new_status
from backend.workflow.runner import ChainRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
) + b"x" * 2048


class ReferenceBindingTests(unittest.TestCase):
    def test_binding_uses_plan_omits_failed_optional_and_preserves_prompt_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            episode = workspace / "projects" / "p" / "episodes" / "e"
            episode.mkdir(parents=True)
            brief = make_creative_brief("反诈", "七十二变", target_duration_sec=10)
            brief["shot_plan"] = {
                "state": "resolved", "shot_count": 1, "durations_sec": [10], "reason": "一个生成单元"
            }
            brief["storyboard_plan"] = {
                "state": "resolved",
                "shots": [{
                    "shot_number": 1, "columns": 2, "rows": 2, "panel_count": 4,
                    "timepoints_sec": [0, 3, 6, 10], "reason": "执行状态",
                    "storyboard_required": True,
                }],
            }
            status = new_status("p", "e", brief)
            plan = {
                "version": "1.0",
                "assets": [
                    {"id": "char_wukong", "type": "character", "name": "悟空", "required": True, "reason": "身份", "shots": [1]},
                    {"id": "prop_phone", "type": "prop", "name": "手机", "required": False, "reason": "可省略", "shots": [1]},
                ],
                "shots": [{
                    "shot_number": 1, "storyboard_required": True,
                    "references": [
                        {"asset_id": "char_wukong", "necessity": "required", "purpose": "身份"},
                        {"asset_id": "prop_phone", "necessity": "optional", "purpose": "道具"},
                    ],
                }],
            }
            (episode / "asset_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            prompt = "[本单元可见角色] 悟空\n角色与站位锁：悟空位于画面中央。"
            (episode / "shot01_prompt_video.txt").write_text(prompt, encoding="utf-8")
            (episode / "shot01_prompt_storyboard.txt").write_text("故事板执行Prompt", encoding="utf-8")
            (episode / "char_wukong_sheet.png").write_bytes(PNG)
            (episode / "shot01_storyboard.png").write_bytes(PNG)
            register_asset(status, episode, "char.wukong", "character_design", "image", "character_sheet", "悟空", "char_wukong_sheet.png", metadata={"planned_asset_id": "char_wukong"})
            register_asset(status, episode, "board.1", "storyboard_generation", "image", "storyboard_sheet", "故事板", "shot01_storyboard.png")
            runner = ChainRunner(PROJECT_ROOT, workspace)
            runner._bind_references(status, episode, for_video=True)
            binding = json.loads((episode / "shot01_video_references.json").read_text(encoding="utf-8"))
            self.assertEqual([item["asset_id"] for item in binding["references"]], ["shot01_storyboard", "char_wukong"])
            self.assertEqual(binding["omitted_optional_assets"], ["prop_phone"])
            self.assertEqual(
                binding["character_reference_closure"],
                {
                    "mode": "explicit",
                    "verified": True,
                    "visible_character_ids": ["char_wukong"],
                    "planned_character_reference_ids": ["char_wukong"],
                    "issues": [],
                },
            )
            self.assertEqual((episode / "shot01_prompt_video.txt").read_text(encoding="utf-8"), prompt)

    def test_video_binding_reports_a_visible_character_without_a_planned_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            episode = workspace / "projects" / "p" / "episodes" / "e"
            episode.mkdir(parents=True)
            brief = make_creative_brief("打斗", "悟空横扫白虎", target_duration_sec=4)
            brief["shot_plan"] = {
                "state": "resolved", "shot_count": 1, "durations_sec": [4], "reason": "单元"
            }
            status = new_status("p", "e", brief)
            plan = {
                "version": "1.0",
                "assets": [
                    {"id": "char_wukong", "type": "character", "name": "孙悟空", "required": True, "reason": "主角", "shots": [1]},
                    {"id": "char_white_tiger", "type": "character", "name": "白虎典狱长", "required": True, "reason": "对手", "shots": [1]},
                ],
                "shots": [{
                    "shot_number": 1,
                    "storyboard_required": False,
                    "references": [
                        {"asset_id": "char_wukong", "necessity": "required", "purpose": "悟空身份"},
                    ],
                }],
            }
            (episode / "asset_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            (episode / "shot01_prompt_video.txt").write_text(
                "[本单元可见角色] 悟空、白虎\n角色与站位锁：悟空横扫白虎。",
                encoding="utf-8",
            )
            (episode / "char_wukong_sheet.png").write_bytes(PNG)
            register_asset(
                status, episode, "char.wukong", "character_design", "image",
                "character_sheet", "悟空", "char_wukong_sheet.png",
                metadata={"planned_asset_id": "char_wukong"},
            )
            runner = ChainRunner(PROJECT_ROOT, workspace)
            runner._bind_references(status, episode, for_video=True)
            binding = json.loads(
                (episode / "shot01_video_references.json").read_text(encoding="utf-8")
            )
            closure = binding["character_reference_closure"]
            self.assertEqual(
                [issue["type"] for issue in closure["issues"]],
                ["visible_character_missing_reference"],
            )
            self.assertIn("char_white_tiger", closure["issues"][0]["message"])
            self.assertIn("不阻塞", status["stages"]["video_binding"]["handoff"]["summary"])

    def test_video_binding_reports_a_stale_character_reference_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            episode = workspace / "projects" / "p" / "episodes" / "e"
            episode.mkdir(parents=True)
            brief = make_creative_brief("打斗", "纯悟空打斗", target_duration_sec=4)
            brief["shot_plan"] = {
                "state": "resolved", "shot_count": 1, "durations_sec": [4], "reason": "单元"
            }
            status = new_status("p", "e", brief)
            plan = {
                "version": "1.0",
                "assets": [
                    {"id": "char_wukong", "type": "character", "name": "孙悟空", "required": True, "reason": "主角", "shots": [1]},
                    {"id": "char_wujing", "type": "character", "name": "沙悟净", "required": True, "reason": "旧角色", "shots": [1]},
                ],
                "shots": [{
                    "shot_number": 1,
                    "storyboard_required": False,
                    "references": [
                        {"asset_id": "char_wukong", "necessity": "required", "purpose": "悟空身份"},
                        {"asset_id": "char_wujing", "necessity": "required", "purpose": "旧绑定"},
                    ],
                }],
            }
            (episode / "asset_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            (episode / "shot01_prompt_video.txt").write_text(
                "[本单元可见角色] 悟空\n角色与站位锁：纯悟空打斗。",
                encoding="utf-8",
            )
            for asset_id, filename, label in (
                ("char_wukong", "char_wukong_sheet.png", "悟空"),
                ("char_wujing", "char_wujing_sheet.png", "悟净"),
            ):
                (episode / filename).write_bytes(PNG)
                register_asset(
                    status, episode, "asset." + asset_id, "character_design",
                    "image", "character_sheet", label, filename,
                    metadata={"planned_asset_id": asset_id},
                )
            runner = ChainRunner(PROJECT_ROOT, workspace)
            runner._bind_references(status, episode, for_video=True)
            binding = json.loads(
                (episode / "shot01_video_references.json").read_text(encoding="utf-8")
            )
            closure = binding["character_reference_closure"]
            self.assertEqual(
                [issue["type"] for issue in closure["issues"]],
                ["possibly_stale_character_reference"],
            )
            self.assertIn("char_wujing", closure["issues"][0]["message"])
            self.assertIn("char_wujing", status["stages"]["video_binding"]["handoff"]["summary"])

    def test_legacy_prompt_ignores_characters_in_negative_role_clauses(self):
        from backend.workflow.runner import _validate_video_character_reference_closure

        plan = {
            "version": "1.0",
            "assets": [
                {"id": "char_wukong", "type": "character", "name": "孙悟空", "required": True, "reason": "主角", "shots": [1]},
                {"id": "char_wujing", "type": "character", "name": "沙悟净", "required": True, "reason": "旧角色", "shots": [1]},
            ],
            "shots": [{
                "shot_number": 1,
                "storyboard_required": False,
                "references": [
                    {"asset_id": "char_wukong", "necessity": "required", "purpose": "悟空身份"},
                ],
            }],
        }
        prompt = (
            "U01-01｜0-4S｜动作全景｜悟空横扫敌人｜无｜金色棍影\n"
            "角色与站位锁：纯悟空打斗；悟净仅保持既定连续性，不新增动作。"
        )
        closure = _validate_video_character_reference_closure(
            plan, plan["shots"][0], prompt
        )
        self.assertEqual(closure["mode"], "legacy_exclusive")
        self.assertTrue(closure["verified"])
        self.assertEqual(closure["visible_character_ids"], ["char_wukong"])


if __name__ == "__main__":
    unittest.main()
