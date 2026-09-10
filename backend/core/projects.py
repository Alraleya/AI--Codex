import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .events import EventLog
from .status import StatusStore, new_status, utc_now, validate_id


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


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.statuses = StatusStore(self.root)
        self.events = EventLog(self.root)

    def project_dir(self, project_id: str) -> Path:
        validate_id(project_id, "project_id")
        return self.root / "projects" / project_id

    def episode_dir(self, project_id: str, episode_id: str) -> Path:
        validate_id(episode_id, "episode_id")
        return self.project_dir(project_id) / "episodes" / episode_id

    def create_project(self, project_id: str, name: str) -> Dict[str, Any]:
        if not name.strip():
            raise ValueError("project name is required")
        path = self.project_dir(project_id) / "project.json"
        if path.exists():
            raise FileExistsError("project already exists: %s" % project_id)
        now = utc_now()
        project = {
            "project_id": project_id,
            "name": name.strip(),
            "created_at": now,
            "updated_at": now,
        }
        _atomic_json(path, project)
        return project

    def create_episode(
        self,
        project_id: str,
        episode_id: str,
        creative_brief: Mapping[str, Any],
        episode_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        project_path = self.project_dir(project_id) / "project.json"
        if not project_path.is_file():
            raise FileNotFoundError("project does not exist: %s" % project_id)
        normalized_name = (episode_name or episode_id).strip()
        if not normalized_name:
            raise ValueError("episode name is required")
        status_path = self.statuses.path_for(project_id, episode_id)
        episode_dir = self.episode_dir(project_id, episode_id)
        if status_path.exists() or episode_dir.exists():
            raise FileExistsError("episode already exists: %s" % episode_id)
        episode_dir.mkdir(parents=True)
        provided_script = creative_brief.get("provided_script")
        if isinstance(provided_script, str):
            _atomic_text(episode_dir / "inputs" / "original_script.md", provided_script)
        episode_meta = {
            "project_id": project_id,
            "episode_id": episode_id,
            "name": normalized_name,
            "created_at": utc_now(),
        }
        _atomic_json(episode_dir / "episode.json", episode_meta)
        status = new_status(project_id, episode_id, creative_brief)
        self.statuses.save(status)
        creation_message = "剧集已创建，等待开始制作"
        if "provided_script" in creative_brief:
            creation_message = "剧集已创建，用户原剧本已锁定，等待开始制作"
        self.events.append(
            project_id,
            episode_id,
            "info",
            creation_message,
            stage="story_design",
        )
        return status

    def list_projects(self) -> List[Dict[str, Any]]:
        projects_root = self.root / "projects"
        projects = []
        if not projects_root.is_dir():
            return projects
        for path in sorted(projects_root.glob("*/project.json")):
            try:
                project = json.loads(path.read_text(encoding="utf-8"))
                project["episode_count"] = len(self.list_episodes(project["project_id"]))
                projects.append(project)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return projects

    def list_episodes(self, project_id: str) -> List[Dict[str, Any]]:
        validate_id(project_id, "project_id")
        status_root = self.root / "status" / project_id
        episodes = []
        if not status_root.is_dir():
            return episodes
        for status_path in sorted(status_root.glob("*.json")):
            episode_id = status_path.stem
            try:
                status = self.statuses.load(project_id, episode_id)
                meta_path = self.episode_dir(project_id, episode_id) / "episode.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            done = sum(
                stage["state"] in {"done", "skipped", "preserved"}
                for stage in status["stages"].values()
            )
            episodes.append(
                {
                    "schema_version": status["schema_version"],
                    "flow_id": status["flow_id"],
                    "flow_version": status["flow_version"],
                    "project_id": project_id,
                    "episode_id": episode_id,
                    "name": meta.get("name", episode_id),
                    "creative_brief": status["creative_brief"],
                    "run": status["run"],
                    "annotations": status["annotations"],
                    "progress": done / len(status["stages"]),
                    "updated_at": status["updated_at"],
                }
            )
        return episodes
