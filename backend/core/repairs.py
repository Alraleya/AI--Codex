import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .annotations import AnnotationStore, TERMINAL_STATUSES
from .events import EventLog
from .flow import STAGE_BY_ID, transitive_downstream
from .lock import EpisodeLock
from .status import StatusStore, utc_now, validate_id
from .tasks import ensure_task, set_task_state
from .usage import UsageLedger


PLAN_STATES = {"planned", "processing", "completed", "blocked", "superseded"}
TARGET_STATES = {"planned", "queued", "running", "verifying", "passed", "failed"}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class RepairPlanStore:
    """Persist validated Stage + Target repair plans without executing providers."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)
        self.statuses = StatusStore(self.workspace_root)
        self.annotations = AnnotationStore(self.workspace_root)
        self.events = EventLog(self.workspace_root)
        self.usage = UsageLedger(self.workspace_root)

    def repair_dir(self, project_id: str, episode_id: str) -> Path:
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        return (
            self.workspace_root
            / "projects"
            / project_id
            / "episodes"
            / episode_id
            / "repairs"
        )

    def manifest_path(self, project_id: str, episode_id: str) -> Path:
        return self.repair_dir(project_id, episode_id) / "index.json"

    def _load_manifest(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        path = self.manifest_path(project_id, episode_id)
        if not path.is_file():
            return {"schema_version": "1.0", "latest_revision": 0, "entries": []}
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "1.0"
            or not isinstance(manifest.get("entries"), list)
        ):
            raise ValueError("invalid repair plan manifest")
        return manifest

    def _plan_path(self, project_id: str, episode_id: str, plan_id: str) -> Path:
        return self.repair_dir(project_id, episode_id) / (plan_id + ".json")

    def load(self, project_id: str, episode_id: str, plan_id: str) -> Dict[str, Any]:
        path = self._plan_path(project_id, episode_id, plan_id)
        with path.open("r", encoding="utf-8") as handle:
            plan = json.load(handle)
        if plan.get("state") not in PLAN_STATES:
            raise ValueError("invalid repair plan")
        return plan

    def list(self, project_id: str, episode_id: str) -> List[Dict[str, Any]]:
        manifest = self._load_manifest(project_id, episode_id)
        plans = []
        for entry in manifest["entries"]:
            try:
                plans.append(self.load(project_id, episode_id, entry["id"]))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        return plans

    def _save_plan(self, plan: Mapping[str, Any]) -> None:
        _atomic_json(
            self._plan_path(plan["project_id"], plan["episode_id"], plan["id"]),
            plan,
        )
        manifest = self._load_manifest(plan["project_id"], plan["episode_id"])
        entry = next(
            (item for item in manifest["entries"] if item["id"] == plan["id"]),
            None,
        )
        summary = {
            "id": plan["id"],
            "revision": plan["revision"],
            "stage": plan["stage"],
            "state": plan["state"],
            "path": "repairs/%s.json" % plan["id"],
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
        }
        if entry is None:
            manifest["entries"].append(summary)
        else:
            entry.update(summary)
        manifest["latest_revision"] = max(
            int(manifest.get("latest_revision", 0)), int(plan["revision"])
        )
        _atomic_json(self.manifest_path(plan["project_id"], plan["episode_id"]), manifest)

    def create_locked(
        self,
        status: Dict[str, Any],
        stage_id: str,
        targets: Sequence[Mapping[str, Any]],
        *,
        annotation_ids: Iterable[str] = (),
        source: str = "manual",
    ) -> Dict[str, Any]:
        if stage_id not in STAGE_BY_ID:
            raise ValueError("unknown repair stage: %s" % stage_id)
        if not targets:
            raise ValueError("repair plan requires at least one target")
        if status["repairs"]["state"] in {"planned", "processing"}:
            raise RuntimeError("another repair plan is already active")

        requested_annotation_ids = list(dict.fromkeys(annotation_ids))
        declared_target_annotations = [
            annotation_id
            for target in targets
            for annotation_id in target.get("annotation_ids", ())
        ]
        needs_annotations = bool(requested_annotation_ids or declared_target_annotations)
        annotations_by_id = (
            {
                item["id"]: item
                for item in self.annotations.list(
                    status["project_id"], status["episode_id"], pending_only=True
                )
            }
            if needs_annotations
            else {}
        )
        for annotation_id in requested_annotation_ids:
            if annotation_id not in annotations_by_id:
                raise ValueError("annotation is missing or already terminal: %s" % annotation_id)
            if annotations_by_id[annotation_id]["stage"] != stage_id:
                raise ValueError("repair annotation belongs to another stage")

        normalized_targets = []
        selected_task_ids = set()
        for item in targets:
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("repair target task_id is required")
            task_id = task_id.strip()
            if task_id in selected_task_ids:
                raise ValueError("duplicate repair target: %s" % task_id)
            selected_task_ids.add(task_id)
            task = ensure_task(status, stage_id, task_id)
            asset_id = item.get("asset_id")
            base_revision = None
            if asset_id is not None:
                asset = status["assets"].get(asset_id)
                if asset is None:
                    raise ValueError("unknown repair asset: %s" % asset_id)
                if asset.get("stage") != stage_id or asset.get("task_id") != task_id:
                    raise ValueError("repair asset does not belong to the target task")
                base_revision = int(asset.get("asset_revision", 1))
            instruction = item.get("instruction", "")
            if not isinstance(instruction, str) or len(instruction.strip()) > 3000:
                raise ValueError("repair instruction is invalid")
            target_annotation_ids = list(dict.fromkeys(item.get("annotation_ids", ())))
            normalized_targets.append(
                {
                    "task_id": task_id,
                    "asset_id": asset_id,
                    "base_asset_revision": base_revision,
                    "annotation_ids": target_annotation_ids,
                    "instruction": instruction.strip(),
                    "state": "planned",
                    "attempt": 0,
                    "result_asset_id": None,
                    "error": None,
                    "updated_at": utc_now(),
                }
            )
            set_task_state(status, stage_id, task["id"], "redo_requested")

        assigned = set()
        for annotation_id in requested_annotation_ids:
            annotation = annotations_by_id[annotation_id]
            annotation_task = annotation.get("task_id") or stage_id
            matching = [
                target
                for target in normalized_targets
                if annotation_task in {stage_id, target["task_id"]}
            ]
            if not matching:
                raise ValueError(
                    "annotation %s does not match a selected repair target" % annotation_id
                )
            for target in matching:
                if annotation_id not in target["annotation_ids"]:
                    target["annotation_ids"].append(annotation_id)
            assigned.add(annotation_id)
        for target in normalized_targets:
            for annotation_id in target["annotation_ids"]:
                if annotation_id not in annotations_by_id:
                    raise ValueError("unknown target annotation: %s" % annotation_id)
                assigned.add(annotation_id)

        revision = int(status["repairs"]["latest_revision"]) + 1
        plan_id = "rp_%06d" % revision
        now = utc_now()
        plan = {
            "id": plan_id,
            "revision": revision,
            "project_id": status["project_id"],
            "episode_id": status["episode_id"],
            "stage": stage_id,
            "state": "planned",
            "source": source,
            "annotation_watermark": int(status["annotations"]["latest_revision"]),
            "annotation_ids": sorted(assigned),
            "targets": normalized_targets,
            "downstream_effects": [
                {"stage": downstream, "action": "mark_stale"}
                for downstream in transitive_downstream(stage_id)[1:]
            ],
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "error": None,
        }
        self._save_plan(plan)
        if assigned:
            self.annotations.transition_locked(
                status,
                sorted(assigned),
                "planned",
                repair_plan_id=plan_id,
            )
        status["repairs"].update(
            {
                "state": "planned",
                "latest_revision": revision,
                "active_plan_id": plan_id,
                "updated_at": now,
            }
        )
        self.usage.record(
            status["project_id"],
            status["episode_id"],
            kind="repair_plan",
            status="planned",
            stage_id=stage_id,
            repair_plan_id=plan_id,
            affected_tasks=[target["task_id"] for target in normalized_targets],
        )
        return plan

    def create(
        self,
        project_id: str,
        episode_id: str,
        stage_id: str,
        targets: Sequence[Mapping[str, Any]],
        *,
        annotation_ids: Iterable[str] = (),
        source: str = "manual",
    ) -> Dict[str, Any]:
        with EpisodeLock(self.workspace_root, project_id, episode_id):
            status = self.statuses.load(project_id, episode_id)
            plan = self.create_locked(
                status,
                stage_id,
                targets,
                annotation_ids=annotation_ids,
                source=source,
            )
            self.statuses.save(status)
            self.events.append(
                project_id,
                episode_id,
                "decision",
                "已制定修复计划 %s：%s"
                % (plan["id"], "、".join(target["task_id"] for target in plan["targets"])),
                stage=stage_id,
                runner="codex",
                meta={"repair_plan_id": plan["id"]},
            )
        return plan

    def begin_locked(self, status: Dict[str, Any], plan: Dict[str, Any]) -> None:
        if plan["state"] not in {"planned", "blocked"}:
            raise RuntimeError("repair plan is not ready to execute")
        plan["state"] = "processing"
        plan["error"] = None
        plan["updated_at"] = utc_now()
        for target in plan["targets"]:
            if target["state"] != "passed":
                target["state"] = "queued"
                target["error"] = None
                target["updated_at"] = plan["updated_at"]
                set_task_state(status, plan["stage"], target["task_id"], "queued")
        if plan["annotation_ids"]:
            pending = [
                annotation["id"]
                for annotation in self.annotations.list(
                    status["project_id"], status["episode_id"], pending_only=True
                )
                if annotation["id"] in plan["annotation_ids"]
                and annotation["status"] == "planned"
            ]
            if pending:
                self.annotations.transition_locked(
                    status,
                    pending,
                    "processing",
                    repair_plan_id=plan["id"],
                )
        status["repairs"].update(
            {
                "state": "processing",
                "active_plan_id": plan["id"],
                "updated_at": plan["updated_at"],
            }
        )
        self._save_plan(plan)

    def set_target_state_locked(
        self,
        status: Dict[str, Any],
        plan: Dict[str, Any],
        task_id: str,
        state: str,
        *,
        result_asset_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if state not in TARGET_STATES:
            raise ValueError("invalid repair target state: %s" % state)
        target = next(
            (item for item in plan["targets"] if item["task_id"] == task_id), None
        )
        if target is None:
            raise ValueError("repair plan has no target: %s" % task_id)
        target["state"] = state
        target["updated_at"] = utc_now()
        target["error"] = error
        if state == "running":
            target["attempt"] += 1
            set_task_state(status, plan["stage"], task_id, "running")
        elif state == "verifying":
            target["result_asset_id"] = result_asset_id
            set_task_state(status, plan["stage"], task_id, "reviewing")
            verifying = []
            for annotation_id in target["annotation_ids"]:
                try:
                    annotation = self.annotations._read_annotation(
                        status["project_id"], status["episode_id"], annotation_id
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if annotation["status"] == "processing":
                    verifying.append(annotation_id)
            if verifying:
                self.annotations.transition_locked(
                    status,
                    verifying,
                    "verifying",
                    repair_plan_id=plan["id"],
                )
        elif state == "passed":
            target["result_asset_id"] = result_asset_id or target.get("result_asset_id")
            set_task_state(
                status,
                plan["stage"],
                task_id,
                "passed",
                review_reason="局部生成和阶段审查通过",
            )
        elif state == "failed":
            set_task_state(status, plan["stage"], task_id, "failed", error=error)
        plan["updated_at"] = target["updated_at"]
        self._save_plan(plan)

    def complete_locked(
        self, status: Dict[str, Any], plan: Dict[str, Any], *, reason: str
    ) -> None:
        if any(target["state"] != "passed" for target in plan["targets"]):
            raise RuntimeError("repair plan cannot complete before every target passes")
        now = utc_now()
        if plan["annotation_ids"]:
            unresolved = []
            for annotation_id in plan["annotation_ids"]:
                annotation = self.annotations._read_annotation(
                    status["project_id"], status["episode_id"], annotation_id
                )
                if annotation["status"] not in TERMINAL_STATUSES:
                    unresolved.append(annotation_id)
            if unresolved:
                self.annotations.transition_locked(
                    status,
                    unresolved,
                    "resolved",
                    reason=reason,
                    repair_plan_id=plan["id"],
                )
        plan.update(
            {
                "state": "completed",
                "updated_at": now,
                "completed_at": now,
                "error": None,
            }
        )
        status["repairs"].update(
            {"state": "clear", "active_plan_id": None, "updated_at": now}
        )
        self._save_plan(plan)

    def block_locked(
        self, status: Dict[str, Any], plan: Dict[str, Any], error: str
    ) -> None:
        now = utc_now()
        plan.update({"state": "blocked", "updated_at": now, "error": error})
        for target in plan["targets"]:
            if target["state"] in {"queued", "running", "verifying", "passed"}:
                target.update({"state": "failed", "error": error, "updated_at": now})
                set_task_state(
                    status, plan["stage"], target["task_id"], "failed", error=error
                )
        status["repairs"].update(
            {"state": "blocked", "active_plan_id": plan["id"], "updated_at": now}
        )
        self._save_plan(plan)

    def supersede_locked(
        self, status: Dict[str, Any], plan: Dict[str, Any], reason: str
    ) -> None:
        """Retire a repair plan when a broader user-approved revision replaces it."""
        now = utc_now()
        for target in plan["targets"]:
            if target["state"] not in {"passed", "failed"}:
                target.update({"state": "failed", "error": reason, "updated_at": now})
                set_task_state(
                    status,
                    plan["stage"],
                    target["task_id"],
                    "failed",
                    error=reason,
                )
        plan.update(
            {
                "state": "superseded",
                "updated_at": now,
                "completed_at": now,
                "error": reason,
            }
        )
        status["repairs"].update(
            {"state": "clear", "active_plan_id": None, "updated_at": now}
        )
        self._save_plan(plan)
