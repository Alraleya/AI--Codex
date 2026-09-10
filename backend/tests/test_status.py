import tempfile
import unittest
from pathlib import Path

from backend.core.flow import STAGE_IDS
from backend.core.status import (
    StatusStore,
    make_creative_brief,
    new_status,
    normalize_shot_plan,
    normalize_storyboard_plan,
    storyboard_required_shots,
    validate_status,
)
from backend.core.models import MODEL_OPTIONS


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.brief = make_creative_brief(
            "雨夜的纸飞机",
            "纸飞机逆风飞回女孩手中",
            style="宫崎骏",
            aspect_ratio="16:9",
            storyboard_decision="yes",
            video_model="fast",
        )

    def test_brief_expands_style_and_defers_shot_plan_to_story_design(self):
        self.assertEqual(self.brief["target_duration_sec"], 7)
        self.assertEqual(
            self.brief["shot_plan"],
            {"state": "pending", "shot_count": None, "durations_sec": [], "reason": ""},
        )
        self.assertEqual(self.brief["storyboard_plan"], {"state": "pending", "shots": []})
        self.assertIn("手绘动画质感", self.brief["style_constraints"])
        self.assertNotEqual(self.brief["style"], self.brief["style_constraints"])

    def test_jia_zhangke_style_expands_into_visible_production_constraints(self):
        brief = make_creative_brief(
            "县城车站的重逢",
            "停运广播响起时，两个人隔着候车厅认出彼此",
            style="贾樟柯式风格",
        )
        self.assertEqual(brief["style"], "贾樟柯式风格")
        self.assertIn("纪实电影质感", brief["style_constraints"])
        self.assertIn("固定机位", brief["style_constraints"])
        self.assertIn("同期环境声", brief["style_constraints"])

    def test_new_brief_does_not_invent_visual_decisions(self):
        brief = make_creative_brief("新剧集", "未知风格", target_duration_sec=10)
        self.assertIsNone(brief["style"])
        self.assertEqual(brief["style_constraints"], "")
        self.assertIsNone(brief["aspect_ratio"])
        self.assertEqual(brief["storyboard_decision"], "no")
        self.assertEqual(brief["production_mode"], "dialogue_direct")
        self.assertEqual(brief["video_model"], "pending")

    def test_explicit_script_settings_override_compatibility_defaults(self):
        script = "总时长：12秒\n画幅：16:9\n风格：写实\n"
        brief = make_creative_brief("新剧集", "脚本设置", provided_script=script)
        self.assertEqual(brief["target_duration_sec"], 12)
        self.assertEqual(brief["aspect_ratio"], "16:9")
        self.assertEqual(brief["style"], "写实")
        self.assertIn("真实材质", brief["style_constraints"])

    def test_new_status_contains_every_stage_immediately(self):
        status = new_status("paperplane", "ep01", self.brief)
        self.assertEqual(tuple(status["stages"]), STAGE_IDS)
        self.assertTrue(all(stage["state"] == "pending" for stage in status["stages"].values()))
        self.assertEqual(status["providers"]["text"], "codex")
        self.assertIn(status["providers"]["model"], MODEL_OPTIONS)
        self.assertNotIn("logs", status)
        self.assertNotIn("fallback_chain", status)
        self.assertEqual(
            status["stages"]["story_design"]["execution_mode"], "main_agent"
        )
        self.assertEqual(
            status["stages"]["storyboard_generation"]["execution_mode"],
            "parallel_tasks",
        )
        self.assertIn("story_design:story_design", status["tasks"])
        self.assertEqual(status["repairs"]["state"], "clear")
        self.assertNotIn("parallel_requests", status)
        self.assertNotIn("dispatch", status["tasks"]["story_design:story_design"])
        validate_status(status)

    def test_status_store_writes_atomically_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StatusStore(Path(directory))
            status = new_status("paperplane", "ep01", self.brief)
            path = store.save(status)
            loaded = store.load("paperplane", "ep01")
            self.assertEqual(loaded, status)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_ids_and_providers_are_restricted(self):
        with self.assertRaises(ValueError):
            new_status("../escape", "ep01", self.brief)
        with self.assertRaises(ValueError):
            new_status("paperplane", "ep01", self.brief, image_provider="mock")
        with self.assertRaisesRegex(ValueError, "at least 4 seconds"):
            make_creative_brief("过短剧集", "一闪而过", target_duration_sec=3)
        self.assertEqual(
            make_creative_brief("完整徒步日", "从起点走到终点", target_duration_sec=165)[
                "target_duration_sec"
            ],
            165,
        )
        self.assertEqual(
            make_creative_brief("长集", "完整故事", target_duration_sec=230)[
                "target_duration_sec"
            ],
            230,
        )

    def test_model_selection_is_limited_to_the_three_gpt56_variants(self):
        status = new_status("paperplane", "ep01", self.brief)
        for model in MODEL_OPTIONS:
            status["providers"]["model"] = model
            validate_status(status)
        status["providers"]["model"] = "gpt-5.5"
        with self.assertRaisesRegex(ValueError, "invalid Codex model"):
            validate_status(status)

    def test_professional_development_brief_is_normalized_into_status(self):
        development = {
            "logline": "  女孩追逐回家的纸飞机。  ",
            "protagonist": "迷路女孩",
            "goal": "抓住纸飞机",
            "obstacle": "横风阻挡",
            "stakes": "失去回家线索",
            "emotional_turn": "慌乱转为笃定",
            "beats": [
                {
                    "time_range": "0–7 秒",
                    "visual_action": "女孩追上纸飞机",
                    "dramatic_purpose": "兑现回家线索",
                    "sound_cue": "雨声转风铃",
                }
            ],
            "ending_payoff": "家门亮起",
            "visual_anchor": "白色纸飞机",
            "continuity_checks": ["运动方向统一"],
        }
        brief = make_creative_brief("雨夜纸飞机", "逆风返回", development_brief=development)
        status = new_status("paperplane", "ep02", brief)
        self.assertEqual(status["creative_brief"]["development_brief"]["logline"], "女孩追逐回家的纸飞机。")
        self.assertEqual(status["creative_brief"]["development_brief"]["beats"][0]["time_range"], "0–7 秒")

    def test_user_provided_script_is_stored_verbatim(self):
        script = "场景：雨夜。\n女孩：我一定要回家。\n"
        brief = make_creative_brief(
            "雨夜纸飞机", "逆风返回", provided_script=script
        )
        status = new_status("paperplane", "ep03", brief)
        self.assertEqual(status["creative_brief"]["provided_script"], script)
        validate_status(status)

    def test_dialogue_direct_mode_is_explicit_and_valid(self):
        brief = make_creative_brief(
            "室内对话", "两个人把误会说开", production_mode="dialogue_direct"
        )
        self.assertEqual(brief["production_mode"], "dialogue_direct")
        validate_status(new_status("paperplane", "ep04", brief))
        with self.assertRaisesRegex(ValueError, "production_mode"):
            make_creative_brief("主题", "钩子", production_mode="unknown")

    def test_storyboard_decision_can_be_pending_before_production(self):
        brief = make_creative_brief(
            "室内对话", "两个人把误会说开", storyboard_decision="pending"
        )
        self.assertEqual(brief["storyboard_decision"], "pending")
        validate_status(new_status("paperplane", "ep05", brief))

    def test_resolved_shot_plan_requires_exact_count_and_total_duration(self):
        # Existing stored plans remain readable even when they predate the 4-second rule.
        plan = normalize_shot_plan(
            {
                "state": "resolved",
                "shot_count": 2,
                "durations_sec": [2.5, 4.5],
                "reason": "先建立环境，再切近景兑现动作。",
            },
            7,
            require_resolved=True,
        )
        self.assertEqual(plan["durations_sec"], [2.5, 4.5])
        with self.assertRaisesRegex(ValueError, "sum"):
            normalize_shot_plan(dict(plan, durations_sec=[2, 4]), 7)
        with self.assertRaisesRegex(ValueError, "match"):
            normalize_shot_plan(dict(plan, durations_sec=[7]), 7)

    def test_newly_resolved_video_shots_must_be_between_four_and_fifteen_seconds(self):
        valid = normalize_shot_plan(
            {
                "state": "resolved",
                "shot_count": 2,
                "durations_sec": [4, 15],
                "reason": "四秒建立动作，十五秒完成连续变化。",
            },
            19,
            require_resolved=True,
            enforce_video_duration_limits=True,
        )
        self.assertEqual(valid["durations_sec"], [4, 15])
        for durations in ([3.9, 15], [4, 15.1]):
            with self.assertRaisesRegex(ValueError, "between 4 and 15"):
                normalize_shot_plan(
                    {
                        "state": "resolved",
                        "shot_count": 2,
                        "durations_sec": durations,
                        "reason": "边界外方案必须拒绝。",
                    },
                    sum(durations),
                    require_resolved=True,
                    enforce_video_duration_limits=True,
                )

    def test_storyboard_grid_and_timepoints_are_model_owned_and_exact(self):
        shot_plan = {
            "state": "resolved",
            "shot_count": 1,
            "durations_sec": [7],
            "reason": "连续单镜",
        }
        plan = normalize_storyboard_plan(
            {
                "state": "resolved",
                "shots": [
                    {
                        "shot_number": 1,
                        "columns": 3,
                        "rows": 2,
                        "panel_count": 6,
                        "timepoints_sec": [0, 1, 2.2, 3.8, 5.2, 7],
                        "reason": "六个动作节点足以表达连续变化。",
                    }
                ],
            },
            shot_plan,
            require_resolved=True,
        )
        self.assertEqual(plan["shots"][0]["panel_count"], 6)
        broken = plan["shots"][0].copy()
        broken["panel_count"] = 5
        with self.assertRaisesRegex(ValueError, "grid size"):
            normalize_storyboard_plan({"state": "resolved", "shots": [broken]}, shot_plan)
        broken = plan["shots"][0].copy()
        broken["timepoints_sec"] = [0.1, 1, 2.2, 3.8, 5.2, 7]
        with self.assertRaisesRegex(ValueError, "P01"):
            normalize_storyboard_plan({"state": "resolved", "shots": [broken]}, shot_plan)

    def test_storyboard_requirement_is_per_shot_and_session_is_numeric(self):
        shot_plan = {
            "state": "resolved",
            "shot_count": 2,
            "durations_sec": [4, 4],
            "reason": "一镜动作和一镜反应",
        }
        brief = make_creative_brief("双镜测试", "完成动作", target_duration_sec=8)
        brief["shot_plan"] = shot_plan
        brief["storyboard_plan"] = normalize_storyboard_plan(
            {
                "state": "resolved",
                "shots": [
                    {
                        "shot_number": 1,
                        "columns": 2,
                        "rows": 2,
                        "panel_count": 4,
                        "timepoints_sec": [0, 1, 2, 4],
                        "reason": "需要建立空间关系",
                        "storyboard_required": True,
                    },
                    {
                        "shot_number": 2,
                        "columns": 2,
                        "rows": 2,
                        "panel_count": 4,
                        "timepoints_sec": [0, 1, 2, 4],
                        "reason": "简单反应不需要出板",
                        "storyboard_required": False,
                    },
                ],
            },
            shot_plan,
            require_resolved=True,
        )
        self.assertEqual([shot["shot_number"] for shot in storyboard_required_shots(brief)], [1])
        self.assertTrue(brief["video_session_id"].isdigit())


if __name__ == "__main__":
    unittest.main()
