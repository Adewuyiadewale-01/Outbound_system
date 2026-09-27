"""LinkedIn quota/ledger state: persistence, counters, and hard limits.

Extracted from helpers/linkedin_helper.py during the linkedin_helper carve
(see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S2). Pure move except the path
anchor rewrite required by architecture §8 — resolved paths are unchanged.
"""

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


STATE_DIR = str(ROOT / "state")


STATE_FILE = str(ROOT / "state" / "linkedin_state.json")


DIAGNOSTIC_DIR = str(ROOT / "state" / "linkedin_debug")


MAX_CONN_REQ_PER_DAY = 30


MAX_CONN_REQ_PER_WEEK = 150


MAX_PROFILE_VIEWS_PER_DAY = 100


MAX_MESSAGES_PER_DAY = 50


MAX_WITHDRAWALS_PER_DAY = 5


MAX_ACTIONS_PER_MINUTE = 4


WARMUP_SCHEDULE = {1: 20, 2: 25}  # week 3+ defaults to MAX_CONN_REQ_PER_DAY


ACCEPTANCE_RATE_WARN = 0.20


ACCEPTANCE_RATE_CRITICAL = 0.15


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _ensure_diagnostic_dir():
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return cleaned.strip("-") or "unknown"


def load_state() -> dict[str, Any]:
    """Load persistent state from disk."""
    _ensure_state_dir()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict[str, Any]):
    """Save persistent state to disk."""
    _ensure_state_dir()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_today_key() -> str:
    return date.today().isoformat()


def get_week_key() -> str:
    """ISO week key like '2026-W11'."""
    d = date.today()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def increment_counter(state: dict, category: str, amount: int = 1) -> int:
    """Increment a daily counter. Returns new value."""
    today = get_today_key()
    if "counters" not in state:
        state["counters"] = {}
    if today not in state["counters"]:
        state["counters"][today] = {}
    current = state["counters"][today].get(category, 0)
    state["counters"][today][category] = current + amount
    save_state(state)
    return current + amount


def get_counter(state: dict, category: str) -> int:
    """Get today's count for a category."""
    today = get_today_key()
    return state.get("counters", {}).get(today, {}).get(category, 0)


def get_weekly_counter(state: dict, category: str) -> int:
    """Sum this week's count for a category (Mon-Sun)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    total = 0
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        total += state.get("counters", {}).get(day, {}).get(category, 0)
    return total
