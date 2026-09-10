import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from backend.core.lock import EpisodeLock, EpisodeLockedError


class EpisodeLockTests(unittest.TestCase):
    def test_second_owner_cannot_acquire_live_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first = EpisodeLock(Path(directory), "paperplane", "ep01")
            second = EpisodeLock(Path(directory), "paperplane", "ep01")
            first.acquire()
            try:
                with self.assertRaises(EpisodeLockedError):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(first.path.exists())

    def test_dead_owner_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = EpisodeLock(Path(directory), "paperplane", "ep01")
            lock.path.parent.mkdir(parents=True)
            lock.path.write_text(
                json.dumps({"pid": 99999999, "token": "dead"}), encoding="utf-8"
            )
            lock.acquire()
            try:
                owner = json.loads(lock.path.read_text(encoding="utf-8"))
                self.assertEqual(owner["token"], lock.token)
            finally:
                lock.release()

    def test_old_lock_with_live_owner_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as directory:
            first = EpisodeLock(
                Path(directory), "paperplane", "ep01", stale_after_sec=1
            )
            second = EpisodeLock(
                Path(directory), "paperplane", "ep01", stale_after_sec=1
            )
            first.acquire()
            try:
                old = time.time() - 3600
                os.utime(first.path, (old, old))
                with self.assertRaises(EpisodeLockedError):
                    second.acquire()
            finally:
                first.release()


if __name__ == "__main__":
    unittest.main()
