import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lead_exec_research import (  # noqa: E402
    annotate_overlap_scan,
    build_destination_row_from_computation,
    build_review_row,
    collect_overlap_top_up_waves,
    next_approved_action,
)
from lead_research_archive import MATCH_AVAILABLE, MATCH_FRESH, ResearchArchive  # noqa: E402


class ApprovedWorkflowStateTests(unittest.TestCase):
    def test_manual_destination_preserves_entered_person_order_and_source_tab(self):
        row = build_destination_row_from_computation(
            {
                "lead_id": "lead-1",
                "company": "Example",
                "website": "https://example.com",
                "source_tab": "Employee_db",
                "executives": [
                    {
                        "name": "First Choice",
                        "title": "Manager",
                        "linkedin_url": "https://linkedin.com/in/first-choice",
                        "research_source": "manual_dashboard",
                    },
                    {
                        "name": "Second Choice",
                        "title": "Founder",
                        "linkedin_url": "https://linkedin.com/in/second-choice",
                        "research_source": "manual_dashboard",
                    },
                ],
            }
        )
        self.assertEqual(row["P1 Name"], "First Choice")
        self.assertEqual(row["P2 Name"], "Second Choice")
        self.assertEqual(row["Source Tab"], "Employee_db")

    def test_new_ready_group_is_claimed_before_freeze(self):
        action = next_approved_action(
            {"ready": True, "claim_status": ""},
            None,
            {},
        )
        self.assertEqual(action, "claim_and_freeze")

    def test_archive_only_computation_uses_safe_group_removal(self):
        action = next_approved_action(
            {"ready": True, "claim_status": "processing"},
            {"publication_mode": "archive_only", "status": "reconciled"},
            {},
        )
        self.assertEqual(action, "archive_unreviewed")

    def test_archive_conflict_blocks_destination_write(self):
        action = next_approved_action(
            {"ready": True, "claim_status": "processing"},
            {"publication_mode": "publish_approved", "status": "reconciled"},
            {"archive_conflicts": 1},
        )
        self.assertEqual(action, "resolve_archive_conflicts")


class OverlapScanTests(unittest.TestCase):
    def test_preparation_scan_only_annotates_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = ResearchArchive(Path(directory) / "archive.json")
            archive.archive_computation(
                {
                    "leads": [
                        {
                            "lead_id": "old-id",
                            "company": "Example",
                            "website": "https://example.com",
                            "executives": [
                                {
                                    "name": "Ada Example",
                                    "linkedin_url": "https://linkedin.com/in/ada-example",
                                }
                            ],
                        }
                    ]
                }
            )
            prepared = {
                "id": "new-id",
                "company": {"name": "Example", "website": "https://www.example.com"},
                "employees_from_sheet": [],
            }

            match = annotate_overlap_scan(prepared, archive, overlap_scan_enabled=True)

            self.assertEqual(match["status"], MATCH_AVAILABLE)
            self.assertEqual(prepared["overlap_status"], MATCH_AVAILABLE)
            self.assertNotIn("research_source", prepared)
            self.assertNotIn("executives", prepared)
            self.assertEqual(archive.get(match["archive_entry_id"])["status"], "available")

    def test_review_row_exposes_overlap_status_not_research_source(self):
        row = build_review_row(
            {},
            Path("run.json"),
            {
                "id": "lead-1",
                "company": {"name": "Example", "website": "https://example.com"},
                "archive_match": {"status": MATCH_AVAILABLE, "archive_entry_id": "archive-1"},
            },
        )

        self.assertEqual(row["Overlap Status"], "Archive Match")
        self.assertNotIn("Research Source", row)

    def test_disabled_top_up_mode_stops_after_base_batch(self):
        def unexpected_wave(_label, _target):
            self.fail("Top-up collector must not run when the mode is disabled.")

        selected, waves, shortfall = collect_overlap_top_up_waves(
            48,
            unexpected_wave,
            enabled=False,
        )

        self.assertEqual(selected, [])
        self.assertEqual(waves, [])
        self.assertEqual(shortfall, 48)

    def test_enabled_top_up_mode_continues_until_fresh_shortfall_is_restored(self):
        queued_waves = [
            [
                {"archive_match": {"status": MATCH_AVAILABLE}},
                {"archive_match": {"status": MATCH_FRESH}},
            ],
            [{"archive_match": {"status": MATCH_FRESH}}],
        ]

        def collect_wave(_label, _target):
            return queued_waves.pop(0)

        selected, waves, shortfall = collect_overlap_top_up_waves(
            2,
            collect_wave,
            enabled=True,
        )

        self.assertEqual(len(selected), 3)
        self.assertEqual(len(waves), 2)
        self.assertEqual(shortfall, 0)


if __name__ == "__main__":
    unittest.main()
