import tempfile
import unittest
from pathlib import Path

from backend.core.events import EventLog


class EventLogTests(unittest.TestCase):
    def test_jsonl_is_separate_and_tail_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory))
            for index in range(5):
                log.append(
                    "paperplane",
                    "ep01",
                    "progress",
                    "step %d" % index,
                    stage="story_design",
                )
            recent = log.tail("paperplane", "ep01", limit=2)
            self.assertEqual([event["content"] for event in recent], ["step 3", "step 4"])
            self.assertTrue(log.path_for("paperplane", "ep01").name.endswith(".jsonl"))

    def test_invalid_event_enums_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory))
            with self.assertRaises(ValueError):
                log.append("paperplane", "ep01", "fake", "no")


if __name__ == "__main__":
    unittest.main()

