import sys
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

import linkedin_outreach_session as obf  # noqa: E402


class FakeWorksheet:
    def __init__(self):
        self.appended = []

    def append_row(self, row, value_input_option):
        self.appended.append((row, value_input_option))


class ObfControlRecoveryTests(unittest.TestCase):
    def test_weekend_guard_covers_saturday_and_sunday(self):
        self.assertTrue(obf._is_obf_weekend("2026-08-08"))
        self.assertTrue(obf._is_obf_weekend("2026-08-09"))
        self.assertFalse(obf._is_obf_weekend("2026-08-10"))

    def test_prepare_and_run_return_weekend_hold_without_touching_external_systems(self):
        prepare = obf.prepare_8_30_session(
            Namespace(creds="unused", date="2026-08-08", mock_daily_json=None, mock_queue_json=None)
        )
        run = obf.run(
            Namespace(
                creds="unused",
                date="2026-08-09",
                mock_daily_json=None,
                mock_queue_json=None,
                mock_quotas_json=None,
                dry_run=False,
            )
        )

        self.assertEqual(prepare["status"], "skipped_weekend")
        self.assertFalse(prepare["ready"])
        self.assertEqual(run["status"], "skipped_weekend")
        self.assertEqual(run["successful_sends"], 0)

    def test_auto_created_row_copies_latest_completed_target_and_start_row(self):
        worksheet = FakeWorksheet()
        headers = obf.OUTREACH_CONTROL_HEADERS
        rows = [
            {
                "Date": "8/4/2026",
                "Base Target": "20",
                "Rollover": "0",
                "Effective Target": "20",
                "Current Progress": "20",
                "Status": "Done",
                "Approved": "TRUE",
                "Prospects Start Row": "100",
            },
            {
                "Date": "8/5/2026",
                "Base Target": "12",
                "Rollover": "0",
                "Effective Target": "12",
                "Current Progress": "8",
                "Status": "Partial",
                "Approved": "TRUE",
                "Prospects Start Row": "120",
            },
        ]

        created = obf._append_auto_created_control_row(worksheet, headers, rows, "8/6/2026")

        self.assertEqual(created["target_source"], "previous_completed_row")
        self.assertEqual(created["source_date"], "8/4/2026")
        self.assertEqual(created["target"], 20)
        self.assertEqual(created["prospects_start_row"], 100)
        self.assertEqual(created["lane_targets"], {"Design": 10, "Automation": 10})
        row = dict(zip(headers, worksheet.appended[0][0]))
        self.assertEqual(row["Date"], "8/6/2026")
        self.assertEqual(row["Base Target"], "20")
        self.assertEqual(row["Current Progress"], "0")
        self.assertEqual(row["Status"], "Planned")
        self.assertEqual(row["Approved"], "TRUE")

    def test_auto_created_row_uses_default_when_no_completed_row_exists(self):
        worksheet = FakeWorksheet()

        created = obf._append_auto_created_control_row(
            worksheet,
            obf.OUTREACH_CONTROL_HEADERS,
            [],
            "8/6/2026",
        )

        self.assertEqual(created["target_source"], "default")
        self.assertEqual(created["target"], 30)
        self.assertEqual(created["lane_targets"], {"Design": 15, "Automation": 15})
        row = dict(zip(obf.OUTREACH_CONTROL_HEADERS, worksheet.appended[0][0]))
        self.assertIn(obf.DEFAULT_LANE_SPLIT_MARKER, row["Notes"])

    def test_baseline_queue_selects_exactly_fifteen_per_lane(self):
        queue = [{"id": f"d-{index}", "primary_lane": "Design"} for index in range(20)] + [
            {"id": f"a-{index}", "primary_lane": "Automation"} for index in range(20)
        ]

        selected, selected_counts, available_counts = obf._select_queue_for_lane_targets(
            queue, {"Design": 15, "Automation": 15}
        )

        self.assertEqual(len(selected), 30)
        self.assertEqual(selected_counts, {"Design": 15, "Automation": 15})
        self.assertEqual(available_counts, {"Design": 20, "Automation": 20})

    def test_prep_auto_assigns_only_blank_lane_in_imminent_batch(self):
        queue = [
            {"id": f"design-{index}", "primary_lane": "Design", "_row_number": index + 2}
            for index in range(5)
        ] + [{"id": "blank", "primary_lane": "", "_row_number": 7}]

        assigned = obf._auto_assign_missing_primary_lanes(
            queue=queue,
            remaining=6,
            lane_targets={},
        )

        self.assertEqual(queue[-1]["primary_lane"], "Automation")
        self.assertEqual(
            assigned["assignments"],
            [
                {
                    "prospect_id": "blank",
                    "company": "",
                    "row_number": 7,
                    "primary_lane": "Automation",
                }
            ],
        )

    def test_prep_auto_assignment_respects_explicit_lane_deficits(self):
        queue = [
            {"id": "design-1", "primary_lane": "Design", "_row_number": 2},
            {"id": "design-2", "primary_lane": "Design", "_row_number": 3},
            {"id": "blank-1", "primary_lane": "", "_row_number": 4},
            {"id": "blank-2", "primary_lane": "", "_row_number": 5},
            {"id": "blank-3", "primary_lane": "", "_row_number": 6},
            {"id": "already-automation", "primary_lane": "Automation", "_row_number": 7},
        ]

        assigned = obf._auto_assign_missing_primary_lanes(
            queue=queue,
            remaining=6,
            lane_targets={"Design": 3, "Automation": 3},
        )

        self.assertEqual(
            [row["primary_lane"] for row in queue],
            ["Design", "Design", "Automation", "Design", "Automation", "Automation"],
        )
        self.assertEqual(len(assigned["assignments"]), 3)

    def test_lane_targets_scale_with_remaining_volume(self):
        self.assertEqual(
            obf._control_lane_targets({"Effective Target": "6"}, remaining=6),
            {"Design": 3, "Automation": 3},
        )
        self.assertEqual(
            obf._control_lane_targets({"Effective Target": "5"}, remaining=5),
            {"Design": 3, "Automation": 2},
        )


if __name__ == "__main__":
    unittest.main()
