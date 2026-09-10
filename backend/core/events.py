import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .status import utc_now, validate_id


EVENT_TYPES = {"info", "progress", "asset", "decision", "done", "block", "error"}
RUNNERS = {
    "codex",
    "subagent",
    "codex_imagegen",
    "jimeng_cli",
    "workbench",
    "jimeng_image_cli",
}


class EventLog:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)

    def path_for(self, project_id: str, episode_id: str) -> Path:
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        return self.workspace_root / "events" / project_id / (episode_id + ".jsonl")

    def append(
        self,
        project_id: str,
        episode_id: str,
        event_type: str,
        content: str,
        stage: Optional[str] = None,
        runner: str = "workbench",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise ValueError("invalid event type: %s" % event_type)
        if runner not in RUNNERS:
            raise ValueError("invalid runner: %s" % runner)
        event = {
            "ts": utc_now(),
            "type": event_type,
            "stage": stage,
            "runner": runner,
            "content": content,
            "meta": dict(meta or {}),
        }
        path = self.path_for(project_id, episode_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event

    def tail(self, project_id: str, episode_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        path = self.path_for(project_id, episode_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
