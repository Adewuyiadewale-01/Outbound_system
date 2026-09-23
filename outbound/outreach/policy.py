"""Sheet schema, targets, limits, and error taxonomy for outreach."""

from __future__ import annotations

OUTREACH_SEQUENCE_TAB = "Outreach Sequence"
OUTREACH_CONTROL_TAB = "Outreach Control"
OUTREACH_CONTROL_HEADERS = [
    "Date",
    "Base Target",
    "Rollover",
    "Effective Target",
    "Current Progress",
    "Status",
    "Approved",
    "Prospects Start Row",
    "Notes",
]
OUTREACH_CONTROL_PROSPECTS_START_ROW = "Prospects Start Row"
OUTREACH_CONTROL_APPROVED_STATUSES = {"approved", "in progress", "partial"}
OUTREACH_CONTROL_TERMINAL_STATUSES = {"done"}

DEFAULT_TARGET = 30
DEFAULT_LANE_TARGETS = {"Design": 15, "Automation": 15}
DEFAULT_LANE_SPLIT_MARKER = "Autonomous lane split: balanced Design / Automation."
QUEUE_BUFFER_LIMIT = 30
DAILY_CONN_REQ_LIMIT = 30
WEEKLY_CONN_REQ_LIMIT = 150
HARD_STOP_ERRORS = {
    "captcha",
    "restriction",
    "email_verify",
    "robot_check",
    "login",
    "daily_conn_req_limit",
    "weekly_conn_req_limit",
    "daily_profile_view_limit",
}
NON_FATAL_SEND_ERRORS = {
    "already_connected",
    "already_pending",
    "email_required_to_connect",
    "no_connect_button",
    "profile_unavailable",
    "unknown",
}
EMAIL_REQUIRED_TO_CONNECT = "email_required_to_connect"
OUTREACH_STATUS_REQUIRES_EMAIL = "Requires email"
MAX_SAME_SEND_ERROR_STREAK = 2
MAX_CONSECUTIVE_SEND_FAILURES = 3
MAX_SAME_BROWSER_ERROR_STREAK = 3
MAX_BROWSER_RECOVERY_ATTEMPTS = 2
CDP_HEALTH_EVAL_TIMEOUT_SEC = 5
FAILURE_BACKOFF_MIN_SEC = 20
FAILURE_BACKOFF_MAX_SEC = 45
NO_CONNECT_BUTTON_RETRIES = 3
NO_CONNECT_BUTTON_RETRY_MIN_SEC = 15
NO_CONNECT_BUTTON_RETRY_MAX_SEC = 35
PROFILE_UNKNOWN_RETRIES = 1
PROFILE_UNKNOWN_RETRY_MIN_SEC = 2
PROFILE_UNKNOWN_RETRY_MAX_SEC = 5
ACTIVITY_READ_TIMEOUT_SEC = 30
RETRYABLE_CONNECT_ERRORS = {
    "activity_load_timeout",
    "connect_page_not_ready",
    "no_connect_button",
    "connect_button_not_clickable",
    "profile_load_timeout",
    "send_modal_not_ready",
}
PROFILE_UI_GUARD_ERRORS = {
    "profile_load_timeout",
    "more_menu_unreadable",
    "connect_page_not_ready",
    "connect_button_not_clickable",
    "send_modal_not_ready",
    "send_without_note_not_ready",
}
