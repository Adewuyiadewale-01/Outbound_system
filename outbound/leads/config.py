"""Configuration for the leads research workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S1). Pure move.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


REPO_CREDS = ROOT / "credentials" / "google-sheets.json"


OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"


DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS


STATE_DIR = ROOT / "state" / "lead_exec_research"


RUNS_DIR = STATE_DIR / "runs"


SNAPSHOTS_DIR = STATE_DIR / "snapshots"


COMPUTATIONS_DIR = STATE_DIR / "computations"


PROMPTS_DIR = STATE_DIR / "prompts"


SEARCH_RESULTS_DIR = STATE_DIR / "search_results"


SEARCH_TASKS_DIR = STATE_DIR / "search_tasks"


BRIDGES_DIR = STATE_DIR / "bridges"


RESEARCH_ARCHIVE_DIR = STATE_DIR / "research_archive"


DEFAULT_RESEARCH_ARCHIVE_INDEX = RESEARCH_ARCHIVE_DIR / "index.json"


LEAD_PREP_CONFIG_PATH = ROOT / "state" / "lead_prep_orchestration_config.json"


SOURCE_COLUMNS = [
    "ID",
    "Company Name",
    "Company Website",
    "Company Linkedin",
    "Person Name",
    "Person Role",
    "Person Email",
    "Person Linkedin",
    "Class",
]


SOURCE_ALIASES = {
    "ID": ["ID", "id"],
    "Company Name": ["Company Name", "Company", "name"],
    "Company Website": ["Company Website", "Website", "website"],
    "Company Linkedin": ["Company Linkedin", "Company LinkedIn", "LinkedIn", "linkedin"],
    "Company Employee Count": [
        "Company Employee Count",
        "LinkedIn employees",
        "Emp Count",
        "Employee Count",
    ],
    "Person Name": ["Person Name", "Name"],
    "Person Role": ["Person Role", "Role", "Title"],
    "Person Email": ["Person Email", "Email"],
    "Person Linkedin": ["Person Linkedin", "Person LinkedIn", "LinkedIn URL", "Profile URL"],
    "Class": ["Class", "Source Tab"],
}


DESTINATION_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
]


OPTIONAL_DESTINATION_COLUMNS = [
    "Use",
]


FINAL_BRIDGE_COLUMNS = [
    "P1 Activity",
    "P2 Activity",
    "Category",
]


PROSPECTS_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P1 Activity",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
    "P2 Activity",
    "Engaged Person",
    "Touch Method",
    "Outreach Status",
    "Outcome",
    "Date Queued",
    "Notes",
]


DEFAULT_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")


DEFAULT_SOURCE_TAB = "Employee_db"


DEFAULT_DESTINATION_TAB = "Pre-final"


DEFAULT_FINAL_TAB = "Final"


DEFAULT_REVIEW_TAB = "Lead Review"


DEFAULT_NOTIFY_EMAIL = "fixmypresencenl1@gmail.com"


DEFAULT_NOTIFICATION_QUEUE_TAB = "Notification Queue"


DEFAULT_OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")


DEFAULT_PROSPECTS_TAB = "Prospects"


DEFAULT_OUTREACH_CONTROL_TAB = "Outreach Control"


OUTREACH_CONTROL_PROSPECTS_START_ROW = "Prospects Start Row"


REVIEW_COLUMNS = [
    "Run ID",
    "Company Name",
    "Company Website",
    "Emp Count",
    "Approved",
    "Use",
]


REVIEW_OVERLAP_COLUMNS = [
    "Primary Lane",
    "Prep Wave",
    "Overlap Status",
    "Archive Entry ID",
]


PRIMARY_LANES = ("Automation", "Design")


NOTIFICATION_QUEUE_COLUMNS = [
    "Created At",
    "Status",
    "To",
    "Subject",
    "Body",
    "Run ID",
    "Run File",
    "Sent At",
    "Error",
]


EXEC_TITLE_PATTERNS = [
    ("Founder", 96),
    ("Co-Founder", 96),
    ("Chief Executive Officer", 100),
    ("CEO", 100),
    ("Managing Director", 95),
    ("President", 92),
    ("Owner", 90),
    ("Partner", 84),
    ("Chief Technology Officer", 82),
    ("CTO", 82),
    ("Chief Operating Officer", 82),
    ("COO", 82),
    ("Chief Financial Officer", 78),
    ("CFO", 78),
    ("Head of", 72),
    ("Director", 66),
]


ROLE_KEYWORDS = [
    "ceo",
    "chief executive",
    "founder",
    "co-founder",
    "managing director",
    "president",
    "owner",
    "partner",
    "cto",
    "chief technology",
    "coo",
    "chief operating",
    "cfo",
    "chief financial",
    "head of",
    "director",
]
