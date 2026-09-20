import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "ORCHESTRATION" / "watcher"
if str(WATCHER) not in sys.path:
    sys.path.insert(0, str(WATCHER))

from orchestration_watcher import due_lead_prep_checkpoint  # noqa: E402


class LeadPrepWatcherTests(unittest.TestCase):
    def setUp(self):
        self.timezone = ZoneInfo("Africa/Lagos")
        self.config = {
            "autonomous_prep_enabled": False,
            "prep_time": "14:00",
            "first_review_deadline": "18:00",
            "fallback_review_deadline": "22:00",
            "base_volume": 50,
            "overlap_scan_mode": "auto",
            "fresh_volume_top_up_mode": "auto",
        }

    def test_daily_cache_is_due_ten_minutes_after_prep_even_when_prep_is_manual(self):
        due = due_lead_prep_checkpoint(
            datetime(2026, 7, 29, 14, 10, tzinfo=self.timezone),
            {"checkpoints": {}},
            self.config,
        )

        self.assertEqual(due["name"], "lead_review_daily_cache")
        self.assertTrue(due["commands"][0][-1].endswith("cache_lead_review_dashboard.py"))

    def test_watcher_does_not_schedule_process_approved_leads(self):
        due = due_lead_prep_checkpoint(
            datetime(2026, 7, 29, 22, 5, tzinfo=self.timezone),
            {"checkpoints": {}},
            self.config,
        )
        command_text = " ".join(part for command in due["commands"] for part in command)

        self.assertNotIn("resume-approved", command_text)


if __name__ == "__main__":
    unittest.main()
