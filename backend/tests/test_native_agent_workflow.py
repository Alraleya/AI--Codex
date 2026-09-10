import tempfile
import hashlib
import unittest
from pathlib import Path

from backend.core.projects import Workspace
from backend.core.status import make_creative_brief
from backend.workflow.runner import ChainRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def story_payload(staging_dir):
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        "outline.md": "提纲",
        "directing.md": "导演设计",
        "script.md": "剧本",
        "storyboard.md": "分镜",
        "shot01_prompt_video.txt": (
            "视频生成单元 U01｜时长7S｜画幅16:9｜风格：写实\n"
            "按以下内部镜头逐字执行：\n"
            "U01-01｜0-7S｜中景，固定机位｜女孩放飞纸飞机｜无｜纸飞机逆风返回\n"
            "字幕：无。\n"
            "[本单元可见角色] 女孩\n"
            "角色与站位锁：女孩位于画面中央。\n"
            "动作因果锁：先放飞，纸飞机再逆风返回。\n"
            "负面约束：不新增角色。"
        ),
        "shot01_prompt_storyboard.txt": (
            "[本镜可见角色] char_girl\n[本镜场景] scene_alley\n"
            "[本镜关键道具] 无\n[首帧锁定] 女孩站立\n[末帧锁定] 女孩站稳\n"
            "[动作因果锁] 无接触\n[版式] 2×2\n"
            "P01: 0.0秒｜开始\nP02: 2秒｜承接\nP03: 4秒｜变化\nP04: 7秒｜结束"
        ),
    }
    files = []
    for name, content in contents.items():
        path = staging_dir / name
        path.write_text(content, encoding="utf-8")
        data = path.read_bytes()
        files.append(
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return {
        "files": files,
        "review": {"passed": True, "reason": "候选自检通过"},
        "summary": "完成",
        "shot_plan": {
            "state": "resolved",
            "shot_count": 1,
            "durations_sec": [7],
            "reason": "单一连续动作无需切镜。",
        },
        "storyboard_plan": {
            "state": "resolved",
            "shots": [{
                "shot_number": 1,
                "columns": 2,
                "rows": 2,
                "panel_count": 4,
                "timepoints_sec": [0, 2, 4, 7],
                "reason": "执行锁定分镜",
                "storyboard_required": True,
            }],
        },
    }


class NativeAgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.workspace.create_project("paperplane", "纸飞机")
        self.workspace.create_episode(
            "paperplane",
            "ep01",
            make_creative_brief(
                "雨夜纸飞机",
                "纸飞机逆风返回",
                style="写实",
                aspect_ratio="16:9",
                storyboard_decision="yes",
                video_model="fast",
            ),
        )
        self.runner = ChainRunner(PROJECT_ROOT, self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_stage_generation_commits_after_self_check_in_one_handoff(self):
        waiting = self.runner.advance("paperplane", "ep01", authorized=True)
        first = waiting["agent_request"]["request"]
        self.assertEqual(waiting["run"]["state"], "waiting_agent")
        self.assertEqual(first["kind"], "stage_generation")
        self.assertEqual(first["stage_id"], "story_design")
        self.assertEqual(first["task_id"], "story_design")
        self.assertEqual(first["execution_id"], "story_design:story_design.e0001")
        self.assertEqual(first["payload"]["output_dir"].split("/")[-2:], ["story_design", first["execution_id"]])
        self.assertEqual(
            self.runner.advance("paperplane", "ep01", authorized=True)["agent_request"]["request"]["id"],
            first["id"],
        )

        done = self.runner.submit_agent_result(
            "paperplane", "ep01", first["id"], story_payload(first["payload"]["output_dir"])
        )
        self.assertEqual(done["agent_request"]["state"], "clear")
        self.assertEqual(done["stages"]["story_design"]["state"], "done")
        self.assertEqual(done["run"]["current_stage"], "asset_planning")
        self.assertEqual(done["stages"]["story_design"]["handoff"]["summary"], "完成")
        task = done["tasks"]["story_design:story_design"]
        self.assertEqual(task["task_id"], "story_design:story_design")
        self.assertEqual(task["execution_id"], first["execution_id"])
        self.assertEqual(task["execution_history"], [first["execution_id"]])

    def test_stage_generation_materializes_inline_child_content(self):
        waiting = self.runner.advance("paperplane", "ep01", authorized=True)
        request = waiting["agent_request"]["request"]
        staged = story_payload(request["payload"]["output_dir"])
        staged["files"] = [
            {"path": item["path"], "content": (Path(request["payload"]["output_dir"]) / item["path"]).read_text(encoding="utf-8")}
            for item in staged["files"]
        ]
        done = self.runner.submit_agent_result(
            "paperplane", "ep01", request["id"], staged
        )
        self.assertEqual(done["stages"]["story_design"]["state"], "done")
        episode_dir = self.root / "projects" / "paperplane" / "episodes" / "ep01"
        self.assertEqual((episode_dir / "script.md").read_text(encoding="utf-8"), "剧本")

    def test_reviewer_cannot_submit_a_stale_request_id(self):
        waiting = self.runner.advance("paperplane", "ep01", authorized=True)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.runner.submit_agent_result(
                "paperplane", "ep01", "not-the-request", story_payload(
                    waiting["agent_request"]["request"]["payload"]["output_dir"]
                )
            )
        stored = self.workspace.statuses.load("paperplane", "ep01")
        self.assertEqual(stored["agent_request"]["request"]["id"], waiting["agent_request"]["request"]["id"])

    def test_pause_cancels_an_unsubmitted_native_handoff_at_a_stage_boundary(self):
        self.runner.advance("paperplane", "ep01", authorized=True)
        paused = self.runner.pause("paperplane", "ep01")
        self.assertEqual(paused["run"]["state"], "paused")
        self.assertEqual(paused["run"]["current_stage"], "story_design")
        self.assertEqual(paused["agent_request"], {"state": "clear", "request": None})
        self.assertEqual(paused["stages"]["story_design"]["state"], "pending")

    def test_failed_self_check_is_a_state_machine_block_not_a_silent_retry(self):
        waiting = self.runner.advance("paperplane", "ep01", authorized=True)
        generation = waiting["agent_request"]["request"]
        result = story_payload(generation["payload"]["output_dir"])
        result["review"] = {"passed": False, "reason": "动作因果不连续"}
        blocked = self.runner.submit_agent_result(
            "paperplane",
            "ep01",
            generation["id"],
            result,
        )
        self.assertEqual(blocked["run"]["state"], "blocked")
        self.assertEqual(blocked["stages"]["story_design"]["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
