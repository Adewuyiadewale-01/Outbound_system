"""Filesystem layout for the engagement workflow's state."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state" / "post_engagement"


CAMPAIGNS_DIR = STATE_DIR / "campaigns"
HIGH_SIGNAL_PATH = STATE_DIR / "high_signal_follows.json"
HISTORY_PATH = STATE_DIR / "history.jsonl"
CONTROL_PATH = STATE_DIR / "control.json"
ARCHIVE_DIR = STATE_DIR / "archive"
LEDGER_PATH = STATE_DIR / "daily_action_ledger.json"
PENDING_ACTIONS_PATH = STATE_DIR / "pending_final_actions.json"
RUNNER_LOCK_PATH = STATE_DIR / "runner.lock"
