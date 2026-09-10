import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from backend.core.assets import register_asset
from backend.core.projects import Workspace
from backend.core.status import make_creative_brief
from backend.workflow.asset_reuse import (
    build_asset_reuse_proposal,
    confirm_asset_reuse,
)
from backend.workflow.chain import flow_next
from backend.workflow.runner import ChainRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _png_bytes(marker: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + marker * 2048


class AssetReuseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.workspace.create_project("series", "Series")
        brief = make_creative_brief(
            "episode", "hook", target_duration_sec=10,
            style="写实", aspect_ratio="16:9",
            storyboard_decision="no", video_model="fast",
        )
        self.workspace.create_episode("series", "source", brief)
        self.workspace.create_episode("series", "target", brief)
        self.source_dir = self.workspace.episode_dir("series", "source")
        self.target_dir = self.workspace.episode_dir("series", "target")
        self.runner = ChainRunner(PROJECT_ROOT, self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_triplet(self, directory: Path, asset_id: str, marker: bytes) -> None:
        (directory / (asset_id + "_sheet.md")).write_text(
            "# locked identity", encoding="utf-8"
        )
        (directory / (asset_id + "_prompt.txt")).write_text(
            "locked provider prompt", encoding="utf-8"
        )
        (directory / (asset_id + "_sheet.png")).write_bytes(_png_bytes(marker))

    def _register_source_character(self, asset_id: str) -> None:
        self._write_triplet(self.source_dir, asset_id, b"s")
        status = self.workspace.statuses.load("series", "source")
        status["stages"]["character_design"]["state"] = "done"
        for suffix, kind, role in (
            ("_sheet.md", "document", "character_sheet"),
            ("_prompt.txt", "prompt", "image_prompt"),
            ("_sheet.png", "image", "character_sheet"),
        ):
            name = asset_id + suffix
            register_asset(
                status,
                self.source_dir,
                "source.%s" % hashlib.sha256(name.encode()).hexdigest()[:16],
                "character_design",
                kind,
                role,
                name,
                name,
                metadata={"planned_asset_id": asset_id},
                task_id=asset_id,
            )
        self.workspace.statuses.save(status)

    def _write_target_plan(self, asset_id: str, asset_type: str = "character") -> None:
        plan = {
            "version": "1.0",
            "assets": [{
                "id": asset_id,
                "type": asset_type,
                "name": asset_id,
                "required": True,
                "reason": "continuity",
                "shots": [1],
            }],
            "shots": [{
                "shot_number": 1,
                "storyboard_required": False,
                "references": [{
                    "asset_id": asset_id,
                    "necessity": "required",
                    "purpose": "identity",
                }],
            }],
        }
        (self.target_dir / "asset_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )

    def test_previous_episode_candidate_waits_for_user_then_skips_provider(self):
        asset_id = "char_returning"
        self._register_source_character(asset_id)
        self._write_target_plan(asset_id)
        status = self.workspace.statuses.load("series", "target")
        status["stages"]["story_design"]["state"] = "done"
        status["stages"]["asset_planning"]["state"] = "done"

        proposal = build_asset_reuse_proposal(self.root, status, self.target_dir)
        status["asset_reuse"] = proposal
        decision = flow_next(status, self.target_dir)
        self.assertEqual(decision.action, "wait_asset_reuse")
        self.assertEqual(proposal["items"][0]["recommended"], "reuse")
        self.assertEqual(
            proposal["items"][0]["source"]["kind"], "previous_episode"
        )

        status["asset_reuse"] = confirm_asset_reuse(proposal, [asset_id])
        outputs = self.runner._execute_stage(
            status, "character_design", self.target_dir, authorized=False
        )
        self.assertEqual(
            set(outputs),
            {
                asset_id + "_sheet.md",
                asset_id + "_prompt.txt",
                asset_id + "_sheet.png",
            },
        )
        self.assertEqual(
            (self.target_dir / (asset_id + "_sheet.png")).read_bytes(),
            (self.source_dir / (asset_id + "_sheet.png")).read_bytes(),
        )
        image = next(
            asset
            for asset in status["assets"].values()
            if asset["path"] == asset_id + "_sheet.png"
        )
        self.assertEqual(image["metadata"]["provider"], "asset_reuse")
        self.assertEqual(image["metadata"]["reuse_source_episode"], "source")
        self.assertEqual(status["metrics"]["image_generations"], 0)

    def test_public_makeup_takes_priority_over_previous_episode(self):
        asset_id = "char_canonical"
        self._register_source_character(asset_id)
        self._write_target_plan(asset_id)
        makeup = self.root / "projects" / "series" / "makeup"
        makeup.mkdir(parents=True)
        self._write_triplet(makeup, asset_id, b"p")
        public_image = makeup / (asset_id + "_sheet.png")
        manifest = {
            "usage": {
                "lookup_first": True,
                "reuse_before_regenerate": True,
                "allow_direct_reference": True,
            },
            "characters": [{
                "id": asset_id,
                "display_name": "Canonical",
                "sheet": asset_id + "_sheet.png",
                "spec": asset_id + "_sheet.md",
                "prompt": asset_id + "_prompt.txt",
                "sha256": hashlib.sha256(public_image.read_bytes()).hexdigest(),
            }],
            "props": [],
        }
        (makeup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        status = self.workspace.statuses.load("series", "target")
        proposal = build_asset_reuse_proposal(self.root, status, self.target_dir)
        self.assertEqual(proposal["items"][0]["source"]["kind"], "project_makeup")

    def test_confirmed_reuse_archives_and_replaces_wrong_current_triplet(self):
        asset_id = "char_returning"
        self._register_source_character(asset_id)
        self._write_target_plan(asset_id)
        self._write_triplet(self.target_dir, asset_id, b"w")
        status = self.workspace.statuses.load("series", "target")
        status["stages"]["asset_planning"]["state"] = "done"
        for suffix, kind, role in (
            ("_sheet.md", "document", "character_sheet"),
            ("_prompt.txt", "prompt", "image_prompt"),
            ("_sheet.png", "image", "character_sheet"),
        ):
            name = asset_id + suffix
            register_asset(
                status,
                self.target_dir,
                "character_design.%s" % hashlib.sha256(name.encode()).hexdigest()[:16],
                "character_design",
                kind,
                role,
                name,
                name,
                metadata={"provider": "codex_imagegen", "planned_asset_id": asset_id},
                task_id=asset_id,
            )
        proposal = build_asset_reuse_proposal(self.root, status, self.target_dir)
        status["asset_reuse"] = confirm_asset_reuse(proposal, [asset_id])

        self.runner._materialize_confirmed_reuse_assets(
            status, "character_design", self.target_dir
        )

        self.assertTrue(
            any(
                item.get("original_path") == asset_id + "_sheet.png"
                for item in status["asset_history"].values()
            )
        )
        self.assertEqual(
            (self.target_dir / (asset_id + "_sheet.png")).read_bytes(),
            (self.source_dir / (asset_id + "_sheet.png")).read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
