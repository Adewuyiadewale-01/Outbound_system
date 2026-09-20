import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cache_lead_review_dashboard import build_dashboard_cache  # noqa: E402


class LeadReviewDashboardCacheTests(unittest.TestCase):
    def test_groups_rows_and_builds_daily_summary(self):
        values = [
            [
                "Date",
                "Primary Lane",
                "Run ID",
                "Company Name",
                "Company Website",
                "Emp Count",
                "Approved",
                "Use",
                "Design Review Complete",
                "Status",
                "Notes",
                "Prep Wave",
                "Overlap Status",
                "Archive Entry ID",
            ],
            ["7/29/2026", "", "", "", "", "", "", "", "TRUE"],
            [
                "",
                "Automation",
                "lead-1",
                "Alpha",
                "https://alpha.test",
                "5",
                "TRUE",
                "Case study worthy",
                "",
                "",
                "",
                "Base",
                "Fresh",
                "",
            ],
            [
                "",
                "Design",
                "lead-2",
                "Beta",
                "https://beta.test",
                "8",
                "FALSE",
                "Potential leads",
                "",
                "",
                "",
                "Base",
                "Archive Match",
                "archive-1",
            ],
            ["7/28/2026", "", "", "", "", "", "", "", "FALSE"],
            [
                "",
                "Design",
                "lead-3",
                "Gamma",
                "https://gamma.test",
                "2",
                "FALSE",
                "Case study worthy",
                "",
                "",
                "",
                "Base",
                "Possible Match",
                "",
            ],
        ]

        payload = build_dashboard_cache(values, now=datetime(2026, 7, 29, 16, 0))

        self.assertEqual(payload["summary"]["total_leads"], 3)
        self.assertEqual(payload["summary"]["total_case_study_worthy"], 2)
        self.assertEqual(payload["today"]["prepared_count"], 2)
        self.assertEqual(payload["today"]["archive_match_count"], 1)
        self.assertTrue(payload["today"]["review_complete"])
        self.assertEqual(payload["today"]["leads"][1]["company"], "Beta")

    def test_returns_empty_today_when_no_group_exists(self):
        values = [
            ["Date", "Run ID", "Company Name", "Approved", "Use"],
            ["7/28/2026", "", "", "", ""],
            ["", "lead-1", "Alpha", "FALSE", "Potential leads"],
        ]

        payload = build_dashboard_cache(values, now=datetime(2026, 7, 29, 8, 0))

        self.assertEqual(payload["today"]["prepared_count"], 0)
        self.assertEqual(payload["today"]["leads"], [])


if __name__ == "__main__":
    unittest.main()
