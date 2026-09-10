import tempfile
import unittest
import json
from pathlib import Path

from backend.core.agent_timing import summarize_agent_timing
from backend.core.usage import UsageLedger, estimate_tokens, summarize_episode
from backend.core.projects import Workspace
from backend.core.status import make_creative_brief


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.workspace.create_project("demo", "演示")
        self.workspace.create_episode("demo", "ep01", make_creative_brief("测试", "测试"))
        self.ledger = UsageLedger(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_estimate_is_nonzero_but_is_not_actual_usage(self):
        self.assertGreater(estimate_tokens("一段中文提示词"), 0)

    def test_records_are_append_only_and_summarized(self):
        self.ledger.record(
            "demo", "ep01", kind="provider_call", status="success",
            stage_id="storyboard_generation", task_id="shot01",
            estimated_input_tokens=120, image_count=1,
        )
        self.ledger.record(
            "demo", "ep01", kind="reuse", status="reused",
            stage_id="storyboard_generation", task_id="shot01",
        )
        status = self.workspace.statuses.load("demo", "ep01")
        summary = summarize_episode(status, self.root, [], self.ledger)
        self.assertEqual(summary["source"], "ledger")
        self.assertEqual(summary["totals"]["provider_calls"], 1)
        self.assertEqual(summary["totals"]["reuse_count"], 1)
        self.assertEqual(summary["totals"]["estimated_input_tokens"], 120)
        self.assertIsNone(summary["totals"]["actual_input_tokens"])

    def test_agent_timing_joins_hook_lifecycle_with_usage_tokens(self):
        timing_path = self.root / "agent_timing" / "demo" / "ep01.jsonl"
        timing_path.parent.mkdir(parents=True, exist_ok=True)
        timing_path.write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "event": "subagent_start",
                        "node_id": "session:agent:1",
                        "project_id": "demo",
                        "episode_id": "ep01",
                        "request_id": "story_design.e0001.stage_generation",
                        "request_kind": "stage_generation",
                        "stage_id": "story_design",
                        "task_id": "story_design",
                        "execution_id": "story_design.e0001",
                        "session_id": "session",
                        "agent_id": "agent",
                        "started_at": "2026-01-01T00:00:02.000Z",
                        "started_epoch_ms": 2000,
                    },
                    {
                        "event": "subagent_stop",
                        "node_id": "session:agent:1",
                        "project_id": "demo",
                        "episode_id": "ep01",
                        "request_id": "story_design.e0001.stage_generation",
                        "request_kind": "stage_generation",
                        "stage_id": "story_design",
                        "task_id": "story_design",
                        "execution_id": "story_design.e0001",
                        "session_id": "session",
                        "agent_id": "agent",
                        "started_at": "2026-01-01T00:00:02.000Z",
                        "stopped_at": "2026-01-01T00:00:07.500Z",
                        "started_epoch_ms": 2000,
                        "stopped_epoch_ms": 7500,
                        "duration_ms": 5500,
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger.record(
            "demo", "ep01", kind="agent_request", status="started",
            stage_id="story_design", task_id="story_design",
            execution_id="story_design.e0001", estimated_input_tokens=800,
        )
        self.ledger.record(
            "demo", "ep01", kind="agent_result", status="success",
            stage_id="story_design", task_id="story_design",
            execution_id="story_design.e0001", estimated_output_tokens=300,
        )
        status = self.workspace.statuses.load("demo", "ep01")
        result = summarize_agent_timing(status, self.root, self.ledger.records("demo", "ep01"))
        self.assertEqual(result["source"], "hook")
        self.assertEqual(result["totals"]["nodes"], 1)
        self.assertEqual(result["totals"]["completed_nodes"], 1)
        self.assertEqual(result["totals"]["total_runtime_ms"], 5500)
        self.assertEqual(result["totals"]["estimated_total_tokens"], 1100)
        self.assertEqual(result["recent"][0]["runtime_ms"], 5500)


if __name__ == "__main__":
    unittest.main()
