import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_lead_review_use import parse_use


class UpdateLeadReviewUseTests(unittest.TestCase):
    def test_normalizes_allowed_values(self):
        self.assertEqual(parse_use("potential leads"), "Potential leads")
        self.assertEqual(parse_use(" Case  study worthy "), "Case study worthy")
        self.assertEqual(parse_use(""), "")

    def test_rejects_unknown_value(self):
        with self.assertRaises(Exception):
            parse_use("Maybe")


if __name__ == "__main__":
    unittest.main()
