import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_prefinal_activity as activity  # noqa: E402


class ActivityRetryCapTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "key": "lead-1:P2",
            "company": "Example Co",
            "prefix": "P2",
            "profile_url": "https://www.linkedin.com/in/example",
        }

    def test_fourth_unresolved_attempt_is_exhausted(self):
        retry = {}
        for _ in range(4):
            retry = activity.next_activity_retry_record(
                retry,
                target=self.target,
                reason="activity_classification_uncertain",
                danger="activity_read_timeout",
                max_attempts=4,
            )

        self.assertEqual(retry["attempts"], 4)
        self.assertTrue(retry["exhausted"])
        self.assertEqual(retry["last_reason"], "activity_classification_uncertain")

    def test_first_three_unresolved_attempts_remain_retryable(self):
        retry = {}
        for number in range(1, 4):
            retry = activity.next_activity_retry_record(
                retry,
                target=self.target,
                reason="activity_feed_not_hydrated",
                danger="activity_read_timeout",
                max_attempts=4,
            )
            self.assertEqual(retry["attempts"], number)
            self.assertFalse(retry["exhausted"])


if __name__ == "__main__":
    unittest.main()
