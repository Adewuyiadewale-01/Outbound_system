import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WATCHER = ROOT / "ORCHESTRATION" / "watcher"
if str(WATCHER) not in sys.path:
    sys.path.insert(0, str(WATCHER))

import orchestration_watcher as watcher  # noqa: E402


class WatcherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Africa/Lagos")
        self.now = datetime(2026, 8, 6, 9, 0, tzinfo=self.tz)

    def test_retry_waits_until_next_retry_at(self):
        payload = {
            "checkpoints": {
                "task": {
                    "status": "retry_waiting",
                    "next_retry_at": (self.now + timedelta(minutes=5)).isoformat(),
                }
            }
        }
        self.assertFalse(watcher.checkpoint_ready(payload, "task", self.now))
        self.assertTrue(watcher.checkpoint_ready(payload, "task", self.now + timedelta(minutes=5)))

    @patch.object(watcher, "related_runner_active", return_value=[])
    def test_orphaned_running_checkpoint_becomes_resumable(self, _active):
        state = {
            "workflows": {
                "activity": {
                    "days": {
                        "2026-08-06": {
                            "checkpoints": {
                                "activity_check_9pm": {"status": "running", "attempt": 1}
                            }
                        }
                    }
                }
            }
        }
        released = watcher.close_stale_running_checkpoints(state, "2026-08-06", self.now)
        checkpoint = state["workflows"]["activity"]["days"]["2026-08-06"]["checkpoints"][
            "activity_check_9pm"
        ]
        self.assertEqual(released, [{"workflow": "activity", "checkpoint": "activity_check_9pm"}])
        self.assertEqual(checkpoint["status"], "resume_pending")
        self.assertTrue(
            watcher.checkpoint_ready({"checkpoints": {"task": checkpoint}}, "task", self.now)
        )

    def test_timeout_and_interruption_are_retryable_reasons(self):
        timeout_result = {"commands": [{"ok": False, "timed_out": True, "returncode": -15}]}
        interrupted_result = {"commands": [{"ok": False, "returncode": -15}]}
        self.assertEqual(
            watcher.checkpoint_failure_reason(timeout_result), "runtime_budget_exceeded"
        )
        self.assertEqual(
            watcher.checkpoint_failure_reason(interrupted_result), "runner_interrupted"
        )
        self.assertFalse(watcher.failure_requires_attention_immediately("runner_interrupted"))

    def test_authentication_failure_needs_immediate_attention(self):
        result = {
            "commands": [{"ok": False, "returncode": 1, "stderr_tail": "LinkedIn login required"}]
        }
        reason = watcher.checkpoint_failure_reason(result)
        self.assertEqual(reason, "linkedin_authentication_required")
        self.assertTrue(watcher.failure_requires_attention_immediately(reason))

    def test_long_workflows_receive_more_than_old_ninety_minute_budget(self):
        command = ["python3", "scripts/run_withdrawal_lanes.py", "--date", "2026-08-06"]
        self.assertGreater(watcher.command_timeout_seconds(command), 90 * 60)

    @patch.object(watcher, "withdrawal_config", return_value={"enabled": True})
    @patch.object(watcher.Path, "exists", return_value=True)
    def test_withdrawal_retry_resumes_same_session_without_repreparing(self, _exists, _config):
        payload = {
            "checkpoints": {
                "withdrawals_run_1": {
                    "status": "retry_waiting",
                    "next_retry_at": self.now.isoformat(),
                    "attempt": 1,
                }
            }
        }
        due = watcher.due_withdrawal_checkpoint(
            datetime(2026, 8, 6, 18, 30, tzinfo=self.tz), payload, "2026-08-06"
        )
        commands = [" ".join(command) for command in due["commands"]]
        self.assertEqual(len(commands), 1)
        self.assertIn("run_withdrawal_lanes.py", commands[0])
        self.assertIn("--resume", commands[0])
        self.assertNotIn("prepare_connection_withdrawals.py", commands[0])

    def test_obf_retry_can_resume_after_normal_window_cutoff(self):
        payload = {
            "checkpoints": {
                "obf_prepare": {"status": "completed"},
                "obf_execute": {
                    "status": "retry_waiting",
                    "next_retry_at": self.now.isoformat(),
                    "attempt": 1,
                },
            }
        }
        config = {**watcher.DEFAULT_OBF_CONFIG, "enabled": True}
        with patch.object(watcher, "read_json", return_value={"ready": True}):
            due = watcher.due_obf_checkpoint(
                datetime(2026, 8, 6, 11, 0, tzinfo=self.tz), payload, "2026-08-06", config
            )
        self.assertEqual(due["name"], "obf_execute")

    def test_obf_is_never_scheduled_on_weekends(self):
        config = {**watcher.DEFAULT_OBF_CONFIG, "enabled": True}
        for weekend_day in (8, 9):  # Saturday and Sunday in August 2026.
            due = watcher.due_obf_checkpoint(
                datetime(2026, 8, weekend_day, 9, 0, tzinfo=self.tz),
                {"checkpoints": {}},
                f"2026-08-{weekend_day:02d}",
                config,
            )
            self.assertIsNone(due)

    @patch.object(watcher.Path, "exists", return_value=True)
    def test_forced_activity_retry_is_rediscovered_and_reuses_session(self, _exists):
        payload = {
            "checkpoints": {
                "activity_check_forced": {
                    "status": "retry_waiting",
                    "next_retry_at": self.now.isoformat(),
                    "attempt": 1,
                }
            }
        }
        due = watcher.due_forced_activity_retry(self.now, payload, "2026-08-06")
        self.assertEqual(due["name"], "activity_check_forced")
        commands = [" ".join(command) for command in due["commands"]]
        self.assertEqual(len(commands), 1)
        self.assertIn("run_activity_lanes.py", commands[0])


if __name__ == "__main__":
    unittest.main()
