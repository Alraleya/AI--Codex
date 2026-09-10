import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .status import utc_now, validate_id


class EpisodeLockedError(RuntimeError):
    pass


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class EpisodeLock:
    def __init__(
        self,
        workspace_root: Path,
        project_id: str,
        episode_id: str,
        stale_after_sec: int = 3600,
    ):
        validate_id(project_id, "project_id")
        validate_id(episode_id, "episode_id")
        self.path = Path(workspace_root) / "locks" / project_id / (episode_id + ".lock")
        self.stale_after_sec = stale_after_sec
        self.token: Optional[str] = None

    def _is_stale(self) -> bool:
        try:
            self.path.stat()
        except FileNotFoundError:
            return False
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            # A live owner remains authoritative even when an image or video
            # provider runs longer than the nominal stale window.
            if _process_exists(int(owner["pid"])):
                return False
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                return False
            return age > self.stale_after_sec

    def acquire(self) -> "EpisodeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            token = uuid.uuid4().hex
            try:
                descriptor = os.open(
                    str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                if not self._is_stale():
                    raise EpisodeLockedError("episode is already running")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                payload = {
                    "pid": os.getpid(),
                    "token": token,
                    "acquired_at": utc_now(),
                }
                os.write(descriptor, json.dumps(payload).encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.token = token
            return self
        raise EpisodeLockedError("failed to recover stale episode lock")

    def release(self) -> None:
        if self.token is None:
            return
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if owner.get("token") == self.token:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.token = None

    def __enter__(self) -> "EpisodeLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
