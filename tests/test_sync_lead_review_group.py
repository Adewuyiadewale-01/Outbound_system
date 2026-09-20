import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sync_lead_review_group import build_value_updates, normalize_rows, parse_payload


class SyncLeadReviewGroupTests(unittest.TestCase):
    def test_normalizes_review_decisions(self):
        rows = normalize_rows(
            [
                {
                    "row": 12,
                    "run_id": "run-1",
                    "approved": True,
                    "use": "potential leads",
                    "expected_approved": False,
                    "expected_use": "",
                }
            ]
        )
        self.assertEqual(rows[0]["use"], "Potential leads")
        self.assertTrue(rows[0]["approved"])
        self.assertFalse(rows[0]["expected_approved"])

    def test_rejects_duplicate_sheet_rows(self):
        with self.assertRaises(ValueError):
            normalize_rows(
                [
                    {"row": 12, "run_id": "run-1"},
                    {"row": 12, "run_id": "run-2"},
                ]
            )

    def test_payload_requires_rows_array(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_payload('{"group_row": 10}')

    def test_batch_ranges_are_worksheet_relative(self):
        updates = build_value_updates(
            [{"row": 564, "approved": True, "use": "Potential leads"}],
            {"Approved": 6, "Use": 7, "Design Review Complete": 8},
            563,
        )
        self.assertEqual(updates[0]["range"], "G564:H564")
        self.assertEqual(updates[1]["range"], "I563")


if __name__ == "__main__":
    unittest.main()
