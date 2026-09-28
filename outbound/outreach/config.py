"""Configuration constants for the outreach workflow (OBF sheet integration).

Extracted from helpers/outreach_helper.py during the outreach_helper carve.
Pure move except the credentials path anchor rewrite (resolved path unchanged).
"""

import os
from pathlib import Path

from outbound.shared.env import load_repo_env

load_repo_env()

ROOT_DIR = Path(__file__).resolve().parents[2]


_DEFAULT_CREDS_PATH = os.path.join(str(ROOT_DIR), "credentials", "google-sheets.json")


_OPENCLAW_CREDS_PATH = os.path.expanduser("~/.openclaw/credentials/google-sheets.json")


CREDS_PATH = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS",
    _DEFAULT_CREDS_PATH if os.path.exists(_DEFAULT_CREDS_PATH) else _OPENCLAW_CREDS_PATH,
)


OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")


ENRICHMENT_SHEET_URL = os.environ.get("ENRICHMENT_SHEET_URL", "")


PROSPECTS_TAB = "Prospects"


OUTREACH_LOG_TAB = "Outreach Log"


PIPELINE_TAB = "Pipeline"


TEMPLATES_TAB = "Templates"


DAILY_METRICS_TAB = "Daily Metrics"


STATUS_QUEUED = "Queued"


STATUS_CONN_SENT = "Connection Sent"


STATUS_REQUIRES_EMAIL = "Requires email"


STATUS_CONNECTED = "Connected"


STATUS_FIRST_MSG = "First Message Sent"


STATUS_FOLLOWING_UP = "Following Up"


STATUS_REPLIED = "Replied"


STATUS_MEETING = "Meeting Set"


STATUS_NOT_INTERESTED = "Not Interested"


STATUS_NO_RESPONSE = "No Response"


SKIP_CONN_REQ_STATUSES = {
    STATUS_CONN_SENT,
    STATUS_REQUIRES_EMAIL,
    STATUS_CONNECTED,
    STATUS_FIRST_MSG,
    STATUS_FOLLOWING_UP,
    STATUS_REPLIED,
    STATUS_MEETING,
    STATUS_NOT_INTERESTED,
    STATUS_NO_RESPONSE,
}


ALLOWED_ACTIVITY_VALUES = {"Active", "Very active", "Not active"}


LOG_ACTION_MAP = {
    "Connection Request": "Conn Request",
    "Connection Accepted": "Connected",
    "Connected": "Connected",
    "First Message": "First Message",
    "First Message Sent": "First Message",
}


STATUS_WITHDRAWN = "Withdrawn"
