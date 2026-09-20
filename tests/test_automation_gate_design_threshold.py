import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import automation_gate  # noqa: E402


class DesignThresholdTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(threshold=20, all_leads=False, lane_scope="all")

    def test_automation_approvals_do_not_satisfy_design_threshold(self):
        rows = [
            {"Run ID": f"d-{i}", "Primary Lane": "Design", "Approved": i < 19} for i in range(25)
        ]
        rows += [
            {"Run ID": f"a-{i}", "Primary Lane": "Automation", "Approved": True} for i in range(25)
        ]
        group = {
            "headers": ["Run ID", "Primary Lane", "Approved"],
            "rows": rows,
            "group_row": 2,
            "group_date": "2026-08-09",
            "review_complete": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(automation_gate, "CLAIMS_DIR", Path(directory)),
            patch.object(automation_gate, "read_current_review_group", return_value=group),
            patch.object(automation_gate, "dns_preflight_with_retries", return_value={"ok": True}),
        ):
            result = automation_gate.status_payload(self.args())

        self.assertEqual(result["approved_count"], 44)
        self.assertEqual(result["approved_design_count"], 19)
        self.assertFalse(result["ready"])

    def test_all_leads_can_be_scoped_to_design_lane(self):
        rows = [
            {
                "Run ID": f"d-{i}",
                "Company Name": f"Design {i}",
                "Primary Lane": "Design",
                "Approved": i < 20,
            }
            for i in range(25)
        ]
        rows += [
            {
                "Run ID": f"a-{i}",
                "Company Name": f"Automation {i}",
                "Primary Lane": "Automation",
                "Approved": False,
            }
            for i in range(25)
        ]
        group = {
            "headers": ["Run ID", "Company Name", "Primary Lane", "Approved"],
            "rows": rows,
            "group_row": 2,
            "group_date": "2026-08-09",
            "review_complete": False,
        }
        args = argparse.Namespace(threshold=20, all_leads=True, lane_scope="design")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(automation_gate, "CLAIMS_DIR", Path(directory)),
            patch.object(automation_gate, "read_current_review_group", return_value=group),
            patch.object(automation_gate, "dns_preflight_with_retries", return_value={"ok": True}),
        ):
            result = automation_gate.status_payload(args)

        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_count"], 25)
        self.assertEqual(result["lane_scope"], "design")
        self.assertTrue(all(lead_id.startswith("d-") for lead_id in result["selected_lead_ids"]))

    def test_all_leads_can_be_scoped_to_automation_lane(self):
        rows = [
            {
                "Run ID": f"d-{i}",
                "Company Name": f"Design {i}",
                "Primary Lane": "Design",
                "Approved": True,
            }
            for i in range(20)
        ]
        rows += [
            {
                "Run ID": f"a-{i}",
                "Company Name": f"Automation {i}",
                "Primary Lane": "Automation",
                "Approved": False,
            }
            for i in range(30)
        ]
        group = {
            "headers": ["Run ID", "Company Name", "Primary Lane", "Approved"],
            "rows": rows,
            "group_row": 2,
            "group_date": "2026-08-09",
            "review_complete": False,
        }
        args = argparse.Namespace(threshold=20, all_leads=True, lane_scope="automation")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(automation_gate, "CLAIMS_DIR", Path(directory)),
            patch.object(automation_gate, "read_current_review_group", return_value=group),
            patch.object(automation_gate, "dns_preflight_with_retries", return_value={"ok": True}),
        ):
            result = automation_gate.status_payload(args)

        self.assertTrue(result["ready"])
        self.assertEqual(result["selected_count"], 30)
        self.assertEqual(result["lane_scope"], "automation")
        self.assertTrue(all(lead_id.startswith("a-") for lead_id in result["selected_lead_ids"]))

    def test_review_slice_splits_selected_rows_into_stable_thirds(self):
        rows = [
            {
                "Run ID": f"lead-{i}",
                "Company Name": f"Company {i}",
                "Primary Lane": "Design",
                "Approved": True,
            }
            for i in range(100)
        ]
        group = {
            "headers": ["Run ID", "Company Name", "Primary Lane", "Approved"],
            "rows": rows,
            "group_row": 2,
            "group_date": "2026-08-09",
            "review_complete": False,
        }
        counts = []
        first_ids = []
        for review_slice in ("1/3", "2/3", "3/3"):
            args = argparse.Namespace(
                threshold=20,
                all_leads=True,
                lane_scope="all",
                review_slice=review_slice,
            )
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(automation_gate, "CLAIMS_DIR", Path(directory)),
                patch.object(automation_gate, "read_current_review_group", return_value=group),
                patch.object(
                    automation_gate, "dns_preflight_with_retries", return_value={"ok": True}
                ),
            ):
                result = automation_gate.status_payload(args)
            counts.append(result["selected_count"])
            first_ids.append(result["selected_lead_ids"][0])

        self.assertEqual(counts, [34, 33, 33])
        self.assertEqual(first_ids, ["lead-0", "lead-1", "lead-2"])


if __name__ == "__main__":
    unittest.main()
