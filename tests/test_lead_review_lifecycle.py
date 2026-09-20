import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lead_review_lifecycle import (  # noqa: E402
    archive_promotion_plan,
    classify_computation_leads,
    classify_review_rows,
    computation_next_action,
)


def review_row(index, lane, *, approved=False, use=""):
    return {
        "Run ID": f"lead-{index}",
        "Primary Lane": lane,
        "Approved": approved,
        "Use": use,
    }


class ReviewRoutingTests(unittest.TestCase):
    def test_disabled_approval_routes_two_balanced_thirty_lead_batches(self):
        rows = [review_row(index, "Design") for index in range(30)]
        rows += [review_row(100 + index, "Automation") for index in range(30)]

        first_batch = classify_review_rows(rows[::2], checkpoint="first", approval_required=False)
        second_batch = classify_review_rows(rows[1::2], checkpoint="first", approval_required=False)

        self.assertEqual(first_batch["action"], "process_all_lanes")
        self.assertEqual(len(first_batch["prefinal"]), 30)
        self.assertEqual(len(second_batch["prefinal"]), 30)
        self.assertEqual(
            sum(row["Primary Lane"] == "Design" for row in first_batch["prefinal"]), 15
        )
        self.assertEqual(
            sum(row["Primary Lane"] == "Automation" for row in first_batch["prefinal"]), 15
        )

    def test_first_checkpoint_skips_entire_group_below_design_threshold(self):
        rows = [review_row(i, "Design", approved=i < 19) for i in range(25)]
        rows += [review_row(100 + i, "Automation") for i in range(25)]

        plan = classify_review_rows(rows, checkpoint="first", threshold=20)

        self.assertEqual(plan["action"], "skip_waiting_fallback")
        self.assertEqual(plan["approved_design_count"], 19)
        self.assertEqual(plan["prefinal"], [])
        self.assertEqual(plan["archive"], [])

    def test_fallback_archives_entire_group_below_design_threshold(self):
        rows = [review_row(i, "Design", approved=i < 3) for i in range(25)]
        rows += [review_row(100 + i, "Automation") for i in range(25)]

        plan = classify_review_rows(rows, checkpoint="fallback", threshold=20)

        self.assertEqual(plan["action"], "process_archive_all")
        self.assertEqual(len(plan["archive"]), 50)
        self.assertEqual(plan["prefinal"], [])

    def test_first_checkpoint_routes_only_design_rows_without_twenty_row_cap(self):
        rows = [review_row(i, "Design", approved=i < 22) for i in range(25)]
        rows[22]["Use"] = "Case study worthy"
        rows[23]["Use"] = "Exclude"
        rows += [review_row(100 + i, "Automation") for i in range(25)]

        plan = classify_review_rows(rows, checkpoint="first", threshold=20)

        self.assertEqual(plan["action"], "process_mixed")
        self.assertEqual(len(plan["prefinal"]), 24)
        self.assertEqual(len(plan["archive"]), 1)
        routed = {row["Run ID"]: row for row in plan["prefinal"]}
        self.assertEqual(routed["lead-22"]["Primary Lane"], "Automation")
        self.assertEqual(routed["lead-23"]["Primary Lane"], "Automation")
        self.assertEqual(routed["lead-0"]["Primary Lane"], "Design")
        self.assertNotIn("lead-100", routed)

    def test_fallback_routes_every_eligible_row_without_twenty_row_cap(self):
        rows = [review_row(i, "Design", approved=i < 22) for i in range(25)]
        rows[22]["Use"] = "Case study worthy"
        rows[23]["Use"] = "Exclude"
        rows += [review_row(100 + i, "Automation") for i in range(25)]

        plan = classify_review_rows(rows, checkpoint="fallback", threshold=20)

        self.assertEqual(plan["action"], "process_mixed")
        self.assertEqual(len(plan["prefinal"]), 49)
        self.assertEqual(len(plan["archive"]), 1)

    def test_fallback_automation_scope_processes_remaining_automation_rows(self):
        rows = [review_row(100 + i, "Automation") for i in range(25)]

        plan = classify_review_rows(
            rows, checkpoint="fallback", threshold=20, lane_scope="automation"
        )

        self.assertEqual(plan["action"], "process_remaining_automation")
        self.assertEqual(len(plan["prefinal"]), 25)
        self.assertEqual(plan["archive"], [])
        self.assertTrue(all(row["Primary Lane"] == "Automation" for row in plan["prefinal"]))

    def test_approved_design_count_does_not_include_approved_automation(self):
        rows = [review_row(i, "Design", approved=i < 19) for i in range(25)]
        rows += [review_row(100 + i, "Automation", approved=True) for i in range(25)]

        plan = classify_review_rows(rows, checkpoint="first", threshold=20)

        self.assertFalse(plan["threshold_met"])
        self.assertEqual(plan["approved_design_count"], 19)

    def test_sliced_first_checkpoint_processes_slice_after_whole_group_gate(self):
        rows = [review_row(i, "Design", approved=i < 7) for i in range(12)]
        rows += [review_row(100 + i, "Automation") for i in range(12)]

        plan = classify_review_rows(rows, checkpoint="first", threshold=20, review_slice="1/3")

        self.assertTrue(plan["threshold_met"])
        self.assertEqual(plan["action"], "process_mixed")
        self.assertEqual(len(plan["prefinal"]), 19)
        routed = {row["Run ID"]: row for row in plan["prefinal"]}
        self.assertIn("lead-100", routed)


class ComputationRoutingTests(unittest.TestCase):
    def test_lane_override_is_applied_to_unapproved_reviewed_design(self):
        leads = []
        for index in range(20):
            leads.append(
                {"lead_id": f"d-{index}", "primary_lane": "Design", "review_approved": True}
            )
        leads.append(
            {
                "lead_id": "d-reviewed",
                "primary_lane": "Design",
                "review_approved": False,
                "use": "Exclude",
            }
        )

        plan = classify_computation_leads(leads, checkpoint="first")

        by_id = {lead["lead_id"]: lead for lead in plan["prefinal"]}
        self.assertEqual(by_id["d-reviewed"]["primary_lane"], "Automation")
        self.assertEqual(by_id["d-reviewed"]["original_lane"], "Design")

    def test_automation_scoped_computation_routes_on_fallback(self):
        leads = [
            {"lead_id": f"a-{index}", "primary_lane": "Automation", "review_approved": False}
            for index in range(3)
        ]

        plan = classify_computation_leads(leads, checkpoint="fallback", lane_scope="automation")

        self.assertEqual(plan["action"], "process_remaining_automation")
        self.assertEqual(len(plan["prefinal"]), 3)
        self.assertEqual(plan["archive"], [])

    def test_status_requires_research_before_finalization(self):
        computation = {"leads": [{"lead_id": "a", "status": "research_pending", "executives": []}]}
        self.assertEqual(computation_next_action(computation)["next_action"], "research_batch")


class ArchivePromotionTests(unittest.TestCase):
    def test_promotion_selects_twenty_design_and_twenty_automation(self):
        rows = [
            {
                "Archive Entry ID": f"d-{i}",
                "Status": "Available",
                "Original Lane": "Design",
                "Approved": True,
            }
            for i in range(25)
        ]
        rows += [
            {
                "Archive Entry ID": f"a-{i}",
                "Status": "Available",
                "Original Lane": "Automation",
                "Approved": False,
            }
            for i in range(30)
        ]

        plan = archive_promotion_plan(rows)

        self.assertTrue(plan["ready"])
        self.assertEqual(len(plan["design"]), 20)
        self.assertEqual(len(plan["automation"]), 20)

    def test_promotion_waits_until_twenty_automation_are_available(self):
        rows = [
            {
                "Archive Entry ID": f"d-{i}",
                "Status": "Available",
                "Original Lane": "Design",
                "Approved": True,
            }
            for i in range(20)
        ]
        rows += [
            {"Archive Entry ID": f"a-{i}", "Status": "Available", "Original Lane": "Automation"}
            for i in range(19)
        ]

        plan = archive_promotion_plan(rows)

        self.assertFalse(plan["ready"])
        self.assertEqual(plan["automation_available_count"], 19)


if __name__ == "__main__":
    unittest.main()
