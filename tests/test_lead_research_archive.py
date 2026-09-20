import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lead_research_archive import (  # noqa: E402
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    canonical_domain,
    canonical_linkedin_company,
    has_reusable_research,
    hydrate_computation_lead,
)


def run_lead(
    lead_id: str,
    company: str,
    website: str,
    linkedin: str = "",
):
    return {
        "id": lead_id,
        "company": {
            "name": company,
            "website": website,
            "linkedin": linkedin,
        },
    }


def computation_lead(
    lead_id: str,
    company: str,
    website: str,
    linkedin: str = "",
):
    return {
        "lead_id": lead_id,
        "company": company,
        "website": website,
        "company_linkedin": linkedin,
        "executives": [
            {
                "name": "Ada Example",
                "title": "Founder",
                "linkedin_url": "https://linkedin.com/in/ada-example",
                "email": "",
            }
        ],
        "search_tasks": [],
        "search_results": [],
        "destination_row": {},
        "status": "reconciled",
        "notes": [],
    }


class NormalizationTests(unittest.TestCase):
    def test_domain_normalization_ignores_protocol_www_and_path(self):
        self.assertEqual(
            canonical_domain("https://www.Example.com/about/?utm_source=test"),
            "example.com",
        )

    def test_linkedin_company_normalization_ignores_locale_and_query(self):
        self.assertEqual(
            canonical_linkedin_company("https://nl.linkedin.com/company/Example-Co/about/?x=1"),
            "company/example-co",
        )

    def test_reusable_research_requires_a_real_contact(self):
        self.assertFalse(
            has_reusable_research({"destination_row": {"P1 Name": "", "P1 LinkedIn": ""}})
        )
        self.assertFalse(
            has_reusable_research({"executives": [{"name": "Ada", "linkedin_url": ""}]})
        )
        self.assertTrue(
            has_reusable_research(
                {
                    "executives": [
                        {"name": "Ada Lovelace", "linkedin_url": "https://linkedin.com/in/ada"}
                    ]
                }
            )
        )


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "archive.json"
        self.archive = ResearchArchive(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_archives_and_matches_by_lead_id(self):
        lead = computation_lead("lead-1", "Example Co", "https://example.com")
        self.archive.archive_computation({"leads": [lead]})

        match = self.archive.match(run_lead("lead-1", "Renamed Example", ""))

        self.assertEqual(match["status"], MATCH_AVAILABLE)
        self.assertIn("lead_id", match["matched_on"])

    def test_matches_changed_lead_id_by_domain(self):
        lead = computation_lead("lead-1", "Example Co", "https://www.example.com/about")
        self.archive.archive_computation({"leads": [lead]})

        match = self.archive.match(run_lead("lead-2", "Example Co BV", "example.com"))

        self.assertEqual(match["status"], MATCH_AVAILABLE)
        self.assertIn("website_domain", match["matched_on"])

    def test_name_only_match_is_a_conflict_not_an_automatic_reuse(self):
        lead = computation_lead("lead-1", "Example Co", "")
        self.archive.archive_computation({"leads": [lead]})

        match = self.archive.match(run_lead("lead-2", "Example Co", ""))

        self.assertEqual(match["status"], MATCH_CONFLICT)
        self.assertEqual(match["confidence"], "name_only")

    def test_unknown_company_is_fresh(self):
        lead = computation_lead("lead-1", "Example Co", "https://example.com")
        self.archive.archive_computation({"leads": [lead]})

        match = self.archive.match(run_lead("lead-2", "Different Co", "https://different.test"))

        self.assertEqual(match["status"], MATCH_FRESH)

    def test_consumed_entry_is_never_reported_as_fresh(self):
        lead = computation_lead("lead-1", "Example Co", "https://example.com")
        result = self.archive.archive_computation({"leads": [lead]})
        self.archive.mark_consumed(result["archive_entry_ids"], destination="Pre-final")

        match = self.archive.match(run_lead("lead-2", "Example Co", "https://example.com"))

        self.assertEqual(match["status"], MATCH_CONSUMED)

    def test_archive_upsert_is_idempotent(self):
        lead = computation_lead("lead-1", "Example Co", "https://example.com")
        self.archive.archive_computation({"leads": [lead]})
        self.archive.archive_computation({"leads": [lead]})

        payload = json.loads(self.path.read_text())
        self.assertEqual(len(payload["entries"]), 1)

    def test_hydration_preserves_current_identity_and_reuses_research(self):
        archived = computation_lead("old-id", "Old name", "https://example.com")
        result = self.archive.archive_computation({"leads": [archived]})
        entry = self.archive.get(result["archive_entry_ids"][0])
        current = {
            "lead_id": "new-id",
            "company": "Current name",
            "website": "https://example.com/new",
            "executives": [],
            "status": "research_pending",
        }

        hydrated = hydrate_computation_lead(current, entry)

        self.assertEqual(hydrated["lead_id"], "new-id")
        self.assertEqual(hydrated["company"], "Current name")
        self.assertEqual(hydrated["executives"][0]["name"], "Ada Example")
        self.assertEqual(hydrated["research_source"], "archive")
        self.assertEqual(hydrated["status"], "archive_reused")


if __name__ == "__main__":
    unittest.main()
