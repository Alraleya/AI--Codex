import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.api import create_server
from backend.core.assets import register_asset
from backend.core.status import make_creative_brief
from backend.core.usage import UsageLedger


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ViewerAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary.name)
        self.server = create_server(
            "127.0.0.1",
            0,
            PROJECT_ROOT,
            self.workspace_root,
        )
        self.server.app.workspace.create_project("paperplane", "纸飞机")
        self.server.app.workspace.create_episode(
            "paperplane",
            "ep01",
            make_creative_brief("雨夜纸飞机", "纸飞机逆风返回"),
            episode_name="雨夜",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = "http://%s:%d" % (host, port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_viewer_lists_projects_episodes_status_and_events(self):
        _, projects = self.request("GET", "/api/projects")
        self.assertEqual(projects["projects"][0]["episode_count"], 1)

        _, episodes = self.request("GET", "/api/projects/paperplane/episodes")
        self.assertEqual(episodes["episodes"][0]["name"], "雨夜")
        self.assertEqual(episodes["episodes"][0]["annotations"]["pending_count"], 0)

        _, status = self.request(
            "GET", "/api/episodes/ep01/status?project=paperplane"
        )
        self.assertEqual(len(status["stages"]), 9)
        self.assertEqual(status["annotations"]["state"], "clear")

        _, events = self.request(
            "GET", "/api/episodes/ep01/events?project=paperplane"
        )
        self.assertEqual(events["events"][0]["type"], "info")

    def test_usage_endpoint_exposes_accounting_without_provider_tokens(self):
        UsageLedger(self.workspace_root).record(
            "paperplane", "ep01", kind="provider_call", status="success",
            stage_id="story_design", task_id="story_design",
            estimated_input_tokens=42,
        )
        _, usage = self.request("GET", "/api/episodes/ep01/usage?project=paperplane")
        self.assertEqual(usage["totals"]["provider_calls"], 1)
        self.assertEqual(usage["totals"]["estimated_input_tokens"], 42)
        self.assertIsNone(usage["totals"]["actual_input_tokens"])
        self.assertIn("agent_timing", usage)
        self.assertEqual(usage["agent_timing"]["source"], "not_started")

    def test_annotation_write_updates_status_cursor_and_can_be_read(self):
        code, annotation = self.request(
            "POST",
            "/api/episodes/ep01/annotations",
            {
                "project": "paperplane",
                "stage": "story_design",
                "asset_id": None,
                "type": "suggestion",
                "content": "结尾需要更明确地兑现纸飞机的回家线索。",
            },
        )
        self.assertEqual(code, 201)
        self.assertEqual(annotation["id"], "a_000001")
        self.assertEqual(annotation["type"], "suggestion")

        _, status = self.request(
            "GET", "/api/episodes/ep01/status?project=paperplane"
        )
        self.assertEqual(status["annotations"]["latest_revision"], 1)
        self.assertEqual(status["annotations"]["pending_count"], 1)

        _, result = self.request(
            "GET", "/api/episodes/ep01/annotations?project=paperplane"
        )
        self.assertEqual(result["annotations"][0]["content"], annotation["content"])

    def test_viewer_serves_only_registered_active_assets(self):
        app = self.server.app
        episode_dir = app.workspace.episode_dir("paperplane", "ep01")
        (episode_dir / "outline.md").write_text("真实文档", encoding="utf-8")
        status = app.workspace.statuses.load("paperplane", "ep01")
        register_asset(
            status,
            episode_dir,
            "story.outline",
            "story_design",
            "document",
            "outline",
            "提纲",
            "outline.md",
        )
        app.workspace.statuses.save(status)

        with urlopen(
            self.base + "/api/episodes/ep01/assets/outline.md?project=paperplane",
            timeout=5,
        ) as response:
            self.assertEqual(response.read().decode("utf-8"), "真实文档")
        with self.assertRaises(HTTPError) as error:
            urlopen(
                self.base + "/api/episodes/ep01/assets/secret.md?project=paperplane",
                timeout=5,
            )
        self.assertEqual(error.exception.code, 404)

    def test_removed_production_routes_are_not_available_from_workbench(self):
        with self.assertRaises(HTTPError) as error:
            self.request(
                "POST",
                "/api/episodes/ep01/advance",
                {"project": "paperplane", "authorized": True},
            )
        self.assertEqual(error.exception.code, 404)

    def test_annotation_rejects_unknown_asset(self):
        with self.assertRaises(HTTPError) as error:
            self.request(
                "POST",
                "/api/episodes/ep01/annotations",
                {
                    "project": "paperplane",
                    "stage": "story_design",
                    "asset_id": "missing",
                    "content": "不存在的资产",
                },
            )
        self.assertEqual(error.exception.code, 400)

    def test_annotation_type_and_asset_version_history_are_visible(self):
        app = self.server.app
        episode_dir = app.workspace.episode_dir("paperplane", "ep01")
        (episode_dir / "board.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 2048)
        status = app.workspace.statuses.load("paperplane", "ep01")
        register_asset(
            status,
            episode_dir,
            "board.01",
            "storyboard_generation",
            "image",
            "storyboard_sheet",
            "镜头 01",
            "board.png",
            task_id="shot01",
        )
        app.workspace.statuses.save(status)

        _, annotation = self.request(
            "POST",
            "/api/episodes/ep01/annotations",
            {
                "project": "paperplane",
                "stage": "storyboard_generation",
                "asset_id": "board.01",
                "type": "redo",
                "content": "号码牌必须重做",
            },
        )
        self.assertEqual(annotation["task_id"], "shot01")
        self.assertEqual(annotation["asset_revision"], 1)
        self.assertEqual(annotation["type"], "redo")

        _, versions = self.request(
            "GET",
            "/api/episodes/ep01/asset-versions?project=paperplane&asset_id=board.01",
        )
        self.assertEqual(versions["active"]["asset_revision"], 1)


if __name__ == "__main__":
    unittest.main()
