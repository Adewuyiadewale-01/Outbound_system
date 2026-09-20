import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import post_engagement as pe


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, path in {
            "STATE_DIR": root,
            "CAMPAIGNS_DIR": root / "campaigns",
            "RUNNER_LOCK_PATH": root / "runner.lock",
            "LEDGER_PATH": root / "ledger.json",
            "CONTROL_PATH": root / "control.json",
            "PENDING_ACTIONS_PATH": root / "pending.json",
            "HISTORY_PATH": root / "history.jsonl",
            "CONFIG_PATH": root / "config.json",
        }.items():
            p = patch.object(pe, name, path)
            p.start()
            self.addCleanup(p.stop)
        self.token = pe.ACTION_ACCOUNT.set("design")
        self.addCleanup(pe.ACTION_ACCOUNT.reset, self.token)
        self.day = "2026-09-08"

    def campaign(self):
        c = pe.new_campaign(
            self.day, {**pe.DEFAULT_CONFIG, "engagement_min": 3, "engagement_max": 3}
        )
        c["candidates"] = [
            {"name": "Example", "profile_url": "https://www.linkedin.com/in/example/"}
        ]
        c["sources"] = [{"submitted_url": "https://www.linkedin.com/posts/example"}]
        pe.ensure_engagement_batches(c, pe.DEFAULT_CONFIG)
        return c

    def test_shared_lock_rejects_second_runner_and_mutation(self):
        with pe.campaign_lock():
            self.assertTrue(pe.runner_active())
            with self.assertRaisesRegex(RuntimeError, "already running"):
                pe.run_campaign_schedule(self.day, True)
            with self.assertRaises(RuntimeError):
                pe.add_source("https://www.linkedin.com/posts/example", self.day)
        self.assertFalse(pe.runner_active())

    def test_pause_signals_without_overwriting_worker_snapshot(self):
        c = self.campaign()
        pe.save_campaign(c)
        with pe.campaign_lock():
            pe.pause_campaign(self.day)
            self.assertEqual(pe.load_campaign(self.day)["status"], c["status"])
            with self.assertRaises(pe.PauseRequested):
                pe.raise_if_paused(c)

    def test_account_ledger_and_deferred_queue_are_isolated(self):
        c = self.campaign()
        profile = c["candidates"][0]["profile_url"]
        pe.record_ledger_action(self.day, "connect", profile)
        pe.defer_final_action(c["candidates"][0], "connect", c)
        token = pe.ACTION_ACCOUNT.set("automation")
        try:
            self.assertFalse(pe.ledger_has(self.day, "connect", profile))
            fresh = pe.new_campaign(self.day, pe.DEFAULT_CONFIG)
            self.assertEqual(fresh["candidates"], [])
            self.assertEqual(fresh["connection_send_capacity"], 10)
            pe.resolve_deferred_action("connect", profile)
            self.assertEqual(len(pe.read_pending_actions()["actions"]), 1)
        finally:
            pe.ACTION_ACCOUNT.reset(token)

    def test_confirmed_like_recovers_counters_and_pending_intent(self):
        c = self.campaign()
        candidate = c["candidates"][0]
        pe.begin_action(c, candidate, "like", "urn:1")
        pe.record_ledger_action(
            self.day,
            "like",
            candidate["profile_url"],
            post_urn="urn:1",
            campaign_id=c["created_at"],
        )
        pe.reconcile_campaign(c)
        self.assertNotIn("pending_action", c)
        self.assertEqual(c["engaged"], 1)
        self.assertEqual(c["engagement_batches"][0]["engaged"], 1)
        self.assertEqual(candidate["status"], "engaged")
        pe.reconcile_campaign(c)
        self.assertEqual(c["engaged"], 1)

    def test_uncertain_action_stops_before_browser_connection(self):
        c = self.campaign()
        pe.begin_action(c, c["candidates"][0], "connect")
        with patch.object(pe, "_connect_campaign_browser") as connect:
            result = pe.run_campaign_schedule(self.day, True)
            self.assertEqual(result["status"], "needs_reconciliation")
            connect.assert_not_called()

    def test_resume_honors_stored_wait_before_running(self):
        c = self.campaign()
        c["next_batch_at"] = (pe.now() + timedelta(minutes=60)).isoformat()
        pe.save_campaign(c)
        calls = []
        with (
            patch.object(
                pe, "wait_for_next_batch", side_effect=lambda _: calls.append("wait") or True
            ),
            patch.object(
                pe,
                "run_campaign",
                side_effect=lambda *_: calls.append("run") or {"status": "completed"},
            ),
        ):
            pe.run_campaign_schedule(self.day, True)
        self.assertEqual(calls, ["wait", "run"])

    def test_recovered_batch_creates_missing_cooldown(self):
        c = self.campaign()
        candidate = c["candidates"][0]
        pe.record_ledger_action(
            self.day,
            "like",
            candidate["profile_url"],
            post_urn="urn:1",
            campaign_id=c["created_at"],
        )
        pe.reconcile_campaign(c)
        self.assertGreater(pe.datetime.fromisoformat(c["next_batch_at"]), pe.now())
        self.assertEqual(c["current_batch_number"], 2)

    def test_legacy_unscoped_actions_block_instead_of_resetting_quota(self):
        pe.write_json(pe.LEDGER_PATH, {"events": [{"day": self.day, "action": "connect"}]})
        with self.assertRaisesRegex(RuntimeError, "account ownership"):
            pe.reconcile_campaign(self.campaign())

    def test_execution_snapshot_resets_fallback_for_new_profile(self):
        c = self.campaign()
        pe.execution_event(
            c,
            c["candidates"][0],
            action="Opening activity",
            method="Direct URL · fallback",
            reason="DOM timeout",
        )
        pe.execution_event(
            c,
            {"name": "Second", "profile_url": "https://www.linkedin.com/in/second/"},
            action="Opening profile",
        )
        self.assertEqual(c["execution"]["fallback_reason"], "")
        self.assertEqual(c["execution"]["profile_name"], "Second")

    def test_short_engagement_batch_does_not_start_final_actions(self):
        c = self.campaign()
        pe.save_campaign(c)
        session = Mock()
        session.get_quotas.return_value = {"profile_views_today": 100, "profile_views_limit": 100}
        with (
            patch.object(pe, "_connect_campaign_browser", return_value=(Mock(), Mock(), session)),
            patch.object(pe, "collect_sources"),
            patch.object(pe, "final_action_queue") as queue,
        ):
            result = pe.run_campaign_schedule(self.day, True)
        self.assertEqual(result["status"], "paused_profile_view_limit")
        queue.assert_not_called()

    def test_failed_connection_cannot_complete_campaign(self):
        c = self.campaign()
        candidate = c["candidates"][0]
        c.update(target=1, engaged=1, connection_target=1, connection_send_capacity=1)
        c["engagement_batches"] = [{"number": 1, "target": 1, "engaged": 1, "status": "completed"}]
        candidate.update(
            status="engaged",
            likes_completed=1,
            engagement_batch=1,
            profile_parser_version=pe.PROFILE_PARSER_VERSION,
            activity_assessment_status="complete",
            recommendation={
                "action": "connect",
                "assessment_version": pe.ACTIVITY_ASSESSMENT_VERSION,
            },
        )
        pe.save_campaign(c)
        session = Mock()
        session.send_connection_only.return_value = {"success": False, "status": "failed"}
        with (
            patch.object(pe, "_connect_campaign_browser", return_value=(Mock(), Mock(), session)),
            patch.object(pe, "collect_sources"),
        ):
            result = pe.run_campaign_schedule(self.day, True)
        self.assertEqual(result["status"], "needs_reconciliation")
        self.assertEqual(result["connections_sent"], 0)
        self.assertEqual(result["pending_action"]["action"], "connect")

    def test_confirmed_connection_reconciles_without_resend(self):
        c = self.campaign()
        candidate = c["candidates"][0]
        pe.begin_action(c, candidate, "connect")
        pe.record_ledger_action(
            self.day, "connect", candidate["profile_url"], campaign_id=c["created_at"]
        )
        pe.reconcile_campaign(c)
        self.assertEqual(c["connections_sent"], 1)
        self.assertNotIn("pending_action", c)
        candidate.update(
            activity_assessment_status="complete",
            recommendation={
                "action": "connect",
                "assessment_version": pe.ACTIVITY_ASSESSMENT_VERSION,
            },
        )
        self.assertEqual(pe.final_action_queue([candidate], c), [])


if __name__ == "__main__":
    unittest.main()
