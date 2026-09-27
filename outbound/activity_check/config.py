"""Configuration constants for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S1). Pure move.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


DEFAULT_LEADS_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")


DEFAULT_PREFINAL_TAB = "Pre-final"


DEFAULT_FINAL_TAB = "Final"


DEFAULT_ACTIVITY_SEQUENCE_TAB = "Activity Sequence"


REPO_CREDS = ROOT / "credentials" / "google-sheets.json"


OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"


DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS


STATE_DIR = ROOT / "state" / "activity_sessions"


JOURNAL_DIR = ROOT / "state" / "activity_journal"


BASE_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "Use",
]


PERSON_COLUMNS = ["Name", "Title", "LinkedIn", "Email"]
P1_COLUMNS = [f"P1 {column}" for column in PERSON_COLUMNS]
P2_COLUMNS = [f"P2 {column}" for column in PERSON_COLUMNS]


ACTIVITY_COLUMNS = ["P1 Activity", "P2 Activity"]


FINAL_REQUIRED_COLUMNS = (
    BASE_COLUMNS
    + P1_COLUMNS
    + ACTIVITY_COLUMNS[:1]
    + P2_COLUMNS
    + ACTIVITY_COLUMNS[1:]
    + ["Category"]
)


CATEGORY_PRIORITY = {"Hyper": 0, "High": 1, "Alpha-medium": 2, "Medium": 3, "Low": 4}


ACTIVITY_SCORE = {"Very active": 2, "Active": 1, "Not active": 0, "": 0}


ACTIVITY_VALUES = {"Very active", "Active", "Not active", ""}


DIVERSION_OPTIONS = [
    ("none", 35),
    ("feed_scroll", 23),
    ("engagement_trail", 14),
    ("profile_drill", 10),
    ("company_page_browse", 9),
    ("recent_post_read", 9),
]


NAVIGATION_TYPE_OPTIONS = [
    ("Direct Url", 55),
    ("Selector-based", 45),
]


BLOCKING_DANGER_PATTERNS = (
    "captcha",
    "restriction",
    "login",
    "logged out",
    "email_verify",
    "email verify",
    "email verification",
    "robot_check",
    "robot check",
    "checkpoint",
    "security challenge",
    "danger detected",
    "unknown_danger",
    "chrome cdp not responding",
    "cdp connection failed",
)


HARD_READ_DANGERS = {"activity_read_timeout"}


HARD_READ_REASONS = {
    "navigation_failed_or_timed_out",
    "activity_page_not_ready",
    "activity_feed_not_hydrated",
    "activity_extract_failed_or_timed_out",
    "insufficient_budget_before_tab",
}


DEFER_ACTIVITY_REASONS = {
    "activity_feed_not_hydrated",
    "activity_classification_uncertain",
    "activity_blank_page_not_ready",
    "activity_fresh_tab_retry_failed",
}


SKIP_ACTIVITY_REASONS = {"invalid_profile_or_404"}
PENDING_404_STATUS = "retry_pending_404"


DEFAULT_MAX_TARGET_ATTEMPTS = 4
