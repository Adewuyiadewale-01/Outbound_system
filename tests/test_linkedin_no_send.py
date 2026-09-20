import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helpers"))

import linkedin_helper
import linkedin_outreach_session


class NoSendModalTests(unittest.TestCase):
    def setUp(self):
        self.cdp = Mock()
        self.sim = Mock()
        self.profile_state = {"state": "connect_direct"}
        self.modal_state = {
            "modalRootFound": True,
            "sendWithoutNote": {"disabled": False},
            "emailRequired": False,
        }

    def test_modal_check_requires_confirmed_dismissal(self):
        with (
            patch.object(linkedin_helper, "_wait_for_linkedin_ready", return_value={"ready": True}),
            patch.object(linkedin_helper, "_click_connect_button", return_value=True),
            patch.object(linkedin_helper, "_wait_for_connect_modal", return_value=True),
            patch.object(linkedin_helper, "_inspect_connect_modal", return_value=self.modal_state),
            patch.object(linkedin_helper, "_dismiss_connect_modal", return_value=False),
            patch.object(linkedin_helper, "check_circuit_breakers", return_value=None),
            patch.object(linkedin_helper, "human_delay", return_value=0),
        ):
            result = linkedin_helper.verify_no_note_send_ui(
                self.cdp, self.sim, {}, "https://linkedin.com/in/example", self.profile_state
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["closed"])
        self.assertEqual(result["error"], "connection_modal_not_dismissed")

    def test_modal_check_passes_after_confirmed_dismissal(self):
        with (
            patch.object(linkedin_helper, "_wait_for_linkedin_ready", return_value={"ready": True}),
            patch.object(linkedin_helper, "_click_connect_button", return_value=True),
            patch.object(linkedin_helper, "_wait_for_connect_modal", return_value=True),
            patch.object(linkedin_helper, "_inspect_connect_modal", return_value=self.modal_state),
            patch.object(linkedin_helper, "_dismiss_connect_modal", return_value=True),
            patch.object(linkedin_helper, "check_circuit_breakers", return_value=None),
            patch.object(linkedin_helper, "human_delay", return_value=0),
        ):
            result = linkedin_helper.verify_no_note_send_ui(
                self.cdp, self.sim, {}, "https://linkedin.com/in/example", self.profile_state
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["closed"])

    def test_lane_check_stops_on_first_failure(self):
        session = Mock()
        session.inspect_profile_action_state.return_value = {"state": "connect_direct"}
        session.verify_no_note_send_ui.return_value = {
            "ok": False,
            "closed": False,
            "error": "connection_modal_not_dismissed",
        }
        selected = [
            {"id": "lead-1", "company": "One", "contact_linkedin": "https://linkedin.com/in/one"},
            {"id": "lead-2", "company": "Two", "contact_linkedin": "https://linkedin.com/in/two"},
        ]

        result = linkedin_outreach_session._run_connection_modal_checks(
            session=session, selected=selected
        )

        self.assertFalse(result["ok"])
        self.assertEqual(len(result["checks"]), 1)
        self.assertEqual(session.verify_no_note_send_ui.call_count, 1)

    def test_lane_check_returns_structured_exception(self):
        session = Mock()
        session.inspect_profile_action_state.side_effect = RuntimeError("profile inspection failed")

        result = linkedin_outreach_session._run_connection_modal_checks(
            session=session,
            selected=[
                {
                    "id": "lead-1",
                    "company": "One",
                    "contact_linkedin": "https://linkedin.com/in/one",
                }
            ],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(len(result["checks"]), 1)
        self.assertIn("profile inspection failed", result["error"])

    def test_connect_click_waits_for_the_real_modal_before_failing(self):
        cdp = Mock()
        with (
            patch.object(
                linkedin_helper, "_wait_for_connect_modal", side_effect=[False, True]
            ) as wait,
            patch.object(cdp, "evaluate", return_value=True),
        ):
            self.assertTrue(linkedin_helper._click_connect_button(cdp, self.sim, "connect_direct"))
        self.assertEqual(wait.call_args_list[0].kwargs["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
