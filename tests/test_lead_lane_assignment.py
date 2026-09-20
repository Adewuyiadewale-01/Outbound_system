import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lead_exec_research import assign_primary_lanes


class LeadLaneAssignmentTests(unittest.TestCase):
    def test_balances_even_group_with_design_block_first(self):
        leads = [{"id": f"lead-{index}"} for index in range(50)]
        counts = assign_primary_lanes(leads)
        self.assertEqual(counts, {"Automation": 25, "Design": 25})
        self.assertTrue(all(lead["primary_lane"] == "Design" for lead in leads[:25]))
        self.assertTrue(all(lead["primary_lane"] == "Automation" for lead in leads[25:]))

    def test_automation_receives_single_odd_remainder(self):
        leads = [{"id": f"lead-{index}"} for index in range(5)]
        counts = assign_primary_lanes(leads)
        self.assertEqual(counts, {"Automation": 3, "Design": 2})
        self.assertEqual(
            [lead["primary_lane"] for lead in leads],
            ["Design", "Design", "Automation", "Automation", "Automation"],
        )


if __name__ == "__main__":
    unittest.main()
