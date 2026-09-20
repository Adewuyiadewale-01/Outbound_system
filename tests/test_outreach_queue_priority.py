import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

import outreach_helper  # noqa: E402


def prospect(row_number, prospect_id):
    return {
        "_row_number": row_number,
        "ID": prospect_id,
        "Company": prospect_id,
        "P1 Name": "Contact",
        "P1 LinkedIn": "https://www.linkedin.com/in/contact",
        "Outreach Status": "",
    }


class OutreachQueuePriorityTests(unittest.TestCase):
    @patch("outreach_helper.read_tab")
    def test_start_row_prioritizes_new_rows_then_uses_older_unused_overflow(self, read_tab):
        read_tab.return_value = {
            "rows": [
                prospect(2, "old-1"),
                prospect(3, "old-2"),
                prospect(4, "new-1"),
                prospect(5, "new-2"),
            ],
            "row_count": 4,
        }

        result = outreach_helper.load_prospect_queue(
            limit=3,
            shuffle=False,
            start_row=4,
        )

        self.assertEqual([item["id"] for item in result["queue"]], ["new-1", "new-2", "old-1"])


if __name__ == "__main__":
    unittest.main()
