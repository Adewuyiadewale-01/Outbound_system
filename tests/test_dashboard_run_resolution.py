import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ORCHESTRATION.monitor_app import local_server


class DashboardRunResolutionTests(unittest.TestCase):
    @staticmethod
    def report_lead(lead_id):
        return {
            "lead_id": lead_id,
            "company": f"Company {lead_id}",
            "website": "https://example.com",
            "status": "reconciled",
            "executives": [
                {
                    "name": "Ada Example",
                    "title": "Founder",
                    "linkedin_url": f"https://linkedin.com/in/{lead_id}",
                }
            ],
        }

    def test_prefers_run_matching_all_review_ids_over_newer_empty_run(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            (runs / "20260729_160150.json").write_text(
                json.dumps({"run_id": "empty", "leads": []}), encoding="utf-8"
            )
            (runs / "20260719_131839.json").write_text(
                json.dumps(
                    {
                        "run_id": "matching",
                        "leads": [{"id": "lead-1"}, {"id": "lead-2"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(local_server, "LEAD_PREP_RUNS_DIR", runs):
                resolved = local_server.latest_matching_review_run(
                    [{"run_id": "lead-1"}, {"run_id": "lead-2"}]
                )
        self.assertEqual(resolved["data"]["run_id"], "matching")

    def test_review_write_date_can_make_an_older_run_current(self):
        run = {"review": {"write": {"date": "7/29/2026"}}}
        self.assertTrue(local_server.run_matches_day(run, "2026-07-29"))

    def test_manual_research_requires_p1_name_title_and_linkedin_profile(self):
        ready = {
            "executives": [
                {
                    "name": "Ada Example",
                    "title": "Managing Director",
                    "linkedin_url": "https://www.linkedin.com/in/ada-example/",
                    "email": "",
                }
            ]
        }
        self.assertTrue(local_server.manual_lead_ready(ready))
        self.assertFalse(
            local_server.manual_lead_ready(
                {**ready, "executives": [{**ready["executives"][0], "title": ""}]}
            )
        )
        self.assertFalse(
            local_server.manual_lead_ready(
                {
                    **ready,
                    "executives": [
                        {
                            **ready["executives"][0],
                            "linkedin_url": "https://www.linkedin.com/company/example",
                        }
                    ],
                }
            )
        )

    def test_manual_research_preserves_empty_p1_when_p2_is_entered(self):
        executives = local_server.normalize_manual_executives(
            [
                {},
                {
                    "name": "Second Person",
                    "title": "Partner",
                    "linkedin_url": "https://linkedin.com/in/second-person",
                },
                {},
            ]
        )
        self.assertEqual(len(executives), 2)
        self.assertEqual(executives[0]["name"], "")
        self.assertEqual(executives[1]["name"], "Second Person")

    def test_manual_research_rejects_non_profile_linkedin_urls(self):
        with self.assertRaisesRegex(ValueError, "linkedin.com/in"):
            local_server.normalize_manual_executives(
                [{"linkedin_url": "https://linkedin.com/company/example"}]
            )

    def test_manual_research_save_writes_only_local_computation_state(self):
        dashboard = {
            "review": {"date": "2026-07-29", "group_row": 563},
            "run": {"file": ""},
            "processing": {
                "manual_editable": True,
                "leads": [
                    {
                        "run_id": "lead-1",
                        "company": "Example",
                        "website": "https://example.com",
                        "use": "Potential leads",
                        "primary_lane": "Design",
                        "executives": [],
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            computations = Path(directory)
            with (
                patch.object(local_server, "LEAD_PREP_COMPUTATIONS_DIR", computations),
                patch.object(local_server, "read_lead_prep_dashboard", return_value=dashboard),
                patch.object(local_server, "run_project_script") as runner,
            ):
                result = local_server.invoke(
                    "save-manual-lead-research",
                    {
                        "lead_id": "lead-1",
                        "executives": [
                            {
                                "name": "Ada Example",
                                "title": "Founder",
                                "linkedin_url": "https://linkedin.com/in/ada-example",
                                "email": "",
                            }
                        ],
                    },
                )
            runner.assert_not_called()
            computation = json.loads(
                next(computations.glob("manual_*.json")).read_text(encoding="utf-8")
            )
        self.assertTrue(result["ok"])
        self.assertEqual(computation["leads"][0]["status"], "manual_ready")

    def test_manual_bridge_refuses_incomplete_group_without_running_writer(self):
        dashboard = {
            "processing": {
                "computation_mode": "manual",
                "computation_file": "/tmp/manual.json",
                "bridged": False,
                "bridge_ready": False,
            }
        }
        with (
            patch.object(local_server, "read_lead_prep_dashboard", return_value=dashboard),
            patch.object(local_server, "run_project_script") as runner,
        ):
            result = local_server.invoke("bridge-manual-lead-processing", {})
        runner.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("every lead", result["error"])

    def test_final_report_marks_verified_full_write_completed(self):
        report = local_server.processing_final_report(
            {
                "status": "written",
                "leads": [self.report_lead("lead-1"), self.report_lead("lead-2")],
                "writes": [
                    {
                        "created_at": "2026-07-29T22:10:00",
                        "destination_tab": "Pre-final",
                        "rows_written": 2,
                        "skipped_unresolved_count": 0,
                        "queue_batch": {
                            "fingerprint": "queue-1",
                            "prefinal_publish": {"verified": True},
                        },
                    }
                ],
            }
        )
        self.assertEqual(report["outcome"], "completed")
        self.assertTrue(report["terminal"])
        self.assertEqual(report["counts"]["rows_written"], 2)
        self.assertTrue(report["write"]["verified"])

    def test_final_report_exposes_partial_write_and_skipped_reason(self):
        report = local_server.processing_final_report(
            {
                "status": "write_partial",
                "leads": [self.report_lead("lead-1"), self.report_lead("lead-2")],
                "writes": [
                    {
                        "destination_tab": "Pre-final",
                        "rows_written": 1,
                        "skipped_unresolved_count": 1,
                        "skipped_unresolved": [
                            {
                                "lead_id": "lead-2",
                                "company": "Company lead-2",
                                "reasons": ["missing_p1_linkedin"],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(report["outcome"], "partial")
        self.assertEqual(report["counts"]["rows_skipped"], 1)
        self.assertIn("missing p1 linkedin", report["issues"][0]["message"])

    def test_final_report_marks_persisted_failure_and_waiting_states(self):
        failed = local_server.processing_final_report(
            {
                "status": "failed",
                "leads": [self.report_lead("lead-1")],
                "errors": ["research write failed"],
            }
        )
        waiting = local_server.processing_final_report({})
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["counts"]["errors"], 1)
        self.assertEqual(waiting["outcome"], "waiting")
        self.assertFalse(waiting["available"])


if __name__ == "__main__":
    unittest.main()
