import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from backend.core.status import make_creative_brief, new_status
from backend.workflow.output import persist_staged_text_outputs, validate_stage_payload


class OutputContractTests(unittest.TestCase):
    def _brief(self, provided_script=None):
        return make_creative_brief(
            "反诈七十二变",
            "手机识破悟空",
            target_duration_sec=10,
            aspect_ratio="9:16",
            provided_script=provided_script,
            storyboard_decision="yes",
        )

    def _story_payload(self, script="完整剧本"):
        video_prompt = (
            "视频生成单元 U01｜时长10S｜画幅9:16｜风格：高质感写实国漫。\n"
            "按以下内部镜头逐字执行：\n"
            "U01-01｜0-1.5S｜大全景，推近｜美女进入洞府｜无｜人物从洞口向内移动\n"
            "U01-02｜1.5-10S｜侧面中景，跟拍｜悟空挥棒击飞妖王｜无｜一次挥击，妖王向右飞出\n"
            "字幕：不添加文字。\n"
            "角色与站位锁：悟空在左，妖王在右。\n"
            "动作因果锁：悟空挥棒后妖王才向右飞出。\n"
            "负面约束：不增加角色，不重复挥棒。"
        )
        files = [
            {"path": "outline.md", "content": "提纲"},
            {"path": "directing.md", "content": "导演说明"},
            {"path": "script.md", "content": script},
            {"path": "storyboard.md", "content": "锁定分镜"},
            {"path": "shot01_prompt_video.txt", "content": video_prompt},
            {
                "path": "shot01_prompt_storyboard.txt",
                "content": (
                    "[本镜可见角色] char_wukong, char_demon\n"
                    "[本镜场景] scene_cave\n[本镜关键道具] prop_phone\n"
                    "[首帧锁定] 美女入洞\n[末帧锁定] 妖王飞出\n"
                    "[动作因果锁] 金箍棒→妖王胸口→向右→撞墙\n"
                    "[版式] 2×2\nP01: 0.0秒｜入洞\nP02: 3秒｜报警\n"
                    "P03: 6秒｜变身\nP04: 10秒｜撞墙"
                ),
            },
        ]
        return {
            "files": files,
            "review": {"passed": True, "reason": "机械拆分合同通过"},
            "summary": "完成",
            "shot_plan": {
                "state": "resolved", "shot_count": 1, "durations_sec": [10], "reason": "一个10秒生成单元"
            },
            "storyboard_plan": {
                "state": "resolved",
                "shots": [{
                    "shot_number": 1, "columns": 2, "rows": 2, "panel_count": 4,
                    "timepoints_sec": [0, 3, 6, 10], "reason": "四个执行状态",
                    "storyboard_required": True,
                }],
            },
        }

    def _stage(self, payload, root):
        output = root / "run"
        output.mkdir(parents=True, exist_ok=True)
        staged = copy.deepcopy(payload)
        manifest = []
        for item in payload["files"]:
            path = output / item["path"]
            path.write_text(item["content"], encoding="utf-8")
            data = path.read_bytes()
            manifest.append({"path": item["path"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        staged["files"] = manifest
        return staged, output

    def test_story_design_returns_locked_video_and_storyboard_prompts(self):
        validate_stage_payload("story_design", self._story_payload(), self._brief())

    def test_story_design_rejects_missing_locked_video_prompt(self):
        payload = self._story_payload()
        payload["files"] = [item for item in payload["files"] if item["path"] != "shot01_prompt_video.txt"]
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_stage_payload("story_design", payload, self._brief())

    def test_story_design_rejects_prose_time_blocks_without_internal_shots(self):
        payload = self._story_payload()
        next(
            item for item in payload["files"] if item["path"] == "shot01_prompt_video.txt"
        )["content"] = "【0—3秒】人物入场。\n【3—10秒】悟空挥棒。"
        with self.assertRaisesRegex(ValueError, "structured '视频生成单元 Uxx' header"):
            validate_stage_payload("story_design", payload, self._brief())

    def test_story_design_rejects_internal_shot_time_gap(self):
        payload = self._story_payload()
        prompt = next(
            item for item in payload["files"] if item["path"] == "shot01_prompt_video.txt"
        )
        prompt["content"] = prompt["content"].replace(
            "U01-02｜1.5-10S", "U01-02｜2-10S"
        )
        with self.assertRaisesRegex(ValueError, "previous endpoint"):
            validate_stage_payload("story_design", payload, self._brief())

    def test_user_script_remains_byte_identical(self):
        source = "总时长：10S\n01｜反诈中心识破七十二变\n"
        payload = self._story_payload(source)
        validate_stage_payload("story_design", payload, self._brief(source))
        payload["files"][2]["content"] += "改写"
        with self.assertRaisesRegex(ValueError, "不可改写"):
            validate_stage_payload("story_design", payload, self._brief(source))

    def test_asset_plan_is_planning_only_and_validates_necessity(self):
        brief = self._brief()
        brief["shot_plan"] = self._story_payload()["shot_plan"]
        brief["storyboard_plan"] = self._story_payload()["storyboard_plan"]
        plan = {
            "version": "1.0",
            "assets": [
                {"id": "char_wukong", "type": "character", "name": "悟空", "required": True, "reason": "身份锁", "shots": [1]},
                {"id": "prop_phone", "type": "prop", "name": "手机", "required": False, "reason": "普通道具", "shots": [1]},
            ],
            "shots": [{
                "shot_number": 1,
                "storyboard_required": True,
                "references": [
                    {"asset_id": "char_wukong", "necessity": "required", "purpose": "身份"},
                    {"asset_id": "prop_phone", "necessity": "optional", "purpose": "道具"},
                ],
            }],
        }
        payload = {
            "files": [{"path": "asset_plan.json", "content": json.dumps(plan, ensure_ascii=False)}],
            "review": {"passed": True, "reason": "规划完成"},
            "summary": "完成",
            "shot_plan": None,
            "storyboard_plan": None,
        }
        validate_stage_payload("asset_planning", payload, brief)
        plan["shots"][0]["references"][1]["necessity"] = "required"
        payload["files"][0]["content"] = json.dumps(plan, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "necessity"):
            validate_stage_payload("asset_planning", payload, brief)

    def test_persisted_story_design_registers_first_stage_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = new_status("p", "e", self._brief())
            payload, output = self._stage(self._story_payload(), root)
            written = persist_staged_text_outputs(status, "story_design", payload, root, output)
            self.assertIn("shot01_prompt_video.txt", written)
            self.assertTrue((root / "shot01_prompt_video.txt").is_file())


if __name__ == "__main__":
    unittest.main()
