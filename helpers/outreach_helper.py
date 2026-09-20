#!/usr/bin/env python3
"""
Outreach helper — Google Sheets integration layer for LinkedIn automation.

Manages the OBF (Operation Brute Force) reporting sheet:
- Prospect queue loading and filtering
- Outreach status tracking
- Outreach event logging
- Message template rendering

Usage as library:
    from outreach_helper import load_prospect_queue, update_prospect_status, log_outreach_event

Usage as CLI:
    python3 outreach_helper.py load-queue --limit 30
    python3 outreach_helper.py update-status --prospect-id 001 --status "Connection Sent"
    python3 outreach_helper.py log-event --prospect-id 001 --action "Connection Request Sent"
    python3 outreach_helper.py load-templates --category "Connection Request"
    python3 outreach_helper.py render-template --template-id CR-01 --vars '{"first_name":"John","company":"Acme"}'
"""

import argparse
import json
import os
import random
import sys
from datetime import date, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from runtime_environment import load_repo_env
from sheets_helper import (
    append_row,
    get_client,
    get_worksheet,
    open_sheet,
    read_tab,
    update_row,
)

load_repo_env()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CREDS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "credentials", "google-sheets.json"
)
_OPENCLAW_CREDS_PATH = os.path.expanduser("~/.openclaw/credentials/google-sheets.json")
CREDS_PATH = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS",
    _DEFAULT_CREDS_PATH if os.path.exists(_DEFAULT_CREDS_PATH) else _OPENCLAW_CREDS_PATH,
)

# Operational sheet URLs are intentionally injected at runtime. See .env.example.
OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")
ENRICHMENT_SHEET_URL = os.environ.get("ENRICHMENT_SHEET_URL", "")

# Tab names
PROSPECTS_TAB = "Prospects"
OUTREACH_LOG_TAB = "Outreach Log"
PIPELINE_TAB = "Pipeline"
TEMPLATES_TAB = "Templates"
DAILY_METRICS_TAB = "Daily Metrics"

# Outreach status values
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

# Statuses that mean "don't send a connection request"
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


def _normalize_activity_value(raw_value: str) -> str:
    """Normalize activity input into the allowed dropdown values."""
    value = str(raw_value).strip()
    if value in ALLOWED_ACTIVITY_VALUES:
        return value

    lowered = value.lower()
    if "recent_items=" in lowered or "total_visible=" in lowered or "active=" in lowered:
        recent_count = 0
        total_visible = 0
        is_active = "active=true" in lowered
        for part in lowered.split(";"):
            key, _, raw = part.strip().partition("=")
            if key == "recent_items":
                try:
                    recent_count = int(float(raw))
                except ValueError:
                    recent_count = 0
            elif key == "total_visible":
                try:
                    total_visible = int(float(raw))
                except ValueError:
                    total_visible = 0
        if recent_count >= 5 or total_visible >= 20:
            return "Very active"
        if is_active or recent_count >= 1:
            return "Active"
        return "Not active"

    return "Not active"


def _normalize_log_action(action: str) -> str:
    value = str(action or "").strip()
    if not value:
        return ""
    return LOG_ACTION_MAP.get(value, value)


# ---------------------------------------------------------------------------
# Prospect queue
# ---------------------------------------------------------------------------


def load_prospect_queue(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
    limit: int = 30,
    shuffle: bool = True,
    status_filter: str | None = None,
    start_row: int | None = None,
    prospects_tab: str = PROSPECTS_TAB,
) -> dict[str, Any]:
    """Load prospects that need connection requests from OBF Prospects tab.

    Filters for rows where Outreach Status is empty or matches status_filter.
    Returns eligible prospects up to `limit`. When ``start_row`` is supplied,
    rows at or after it are prioritized; older unused rows remain an overflow
    source so a new bridge cannot waste an otherwise runnable day.

    Each prospect dict includes:
        - id, company, engaged_person
        - contact_name, contact_title, contact_linkedin, contact_email, contact_activity
        - source_tab, emp_count, touch_method
        - _row_number (for updating status later)
    """
    data = read_tab(credentials_path, sheet_url, prospects_tab)

    queue = []
    start_row_value = int(start_row or 0)
    for row in data["rows"]:
        outreach_status = str(row.get("Outreach Status", "")).strip()

        # Filter logic
        if status_filter:
            if outreach_status != status_filter:
                continue
        else:
            # Default: only prospects with no outreach status (fresh) or "Queued"
            if outreach_status and outreach_status not in ("", STATUS_QUEUED):
                continue

        # Determine which person to engage
        engaged = str(row.get("Engaged Person", "")).strip()
        if engaged == "Person 2":
            prefix = "P2"
        else:
            prefix = "P1"  # default to Person 1

        contact_linkedin = str(row.get(f"{prefix} LinkedIn", "")).strip()
        if not contact_linkedin:
            continue  # Can't send connection request without LinkedIn URL

        # Ensure full URL
        if contact_linkedin and not contact_linkedin.startswith("http"):
            contact_linkedin = "https://www." + contact_linkedin

        prospect = {
            "id": str(row.get("ID", "")).strip(),
            "company": str(row.get("Company", "")).strip(),
            "website": str(row.get("Website", "")).strip(),
            "company_linkedin": str(row.get("Company LinkedIn", "")).strip(),
            "emp_count": str(row.get("Emp Count", "")).strip(),
            "source_tab": str(row.get("Source Tab", "")).strip(),
            "primary_lane": str(row.get("Primary Lane", "")).strip(),
            "engaged_person": engaged or "Person 1",
            "contact_name": str(row.get(f"{prefix} Name", "")).strip(),
            "contact_title": str(row.get(f"{prefix} Title", "")).strip(),
            "contact_linkedin": contact_linkedin,
            "contact_email": str(row.get(f"{prefix} Email", "")).strip(),
            "contact_activity": str(row.get(f"{prefix} Activity", "")).strip(),
            "touch_method": str(row.get("Touch Method", "")).strip(),
            "outreach_status": outreach_status,
            "date_queued": str(row.get("Date Queued", "")).strip(),
            "notes": str(row.get("Notes", "")).strip(),
            "_row_number": row["_row_number"],
        }
        queue.append(prospect)

    if start_row_value:
        priority_rows = [
            prospect
            for prospect in queue
            if int(prospect.get("_row_number", 0) or 0) >= start_row_value
        ]
        overflow_rows = [
            prospect
            for prospect in queue
            if int(prospect.get("_row_number", 0) or 0) < start_row_value
        ]
        if shuffle:
            random.shuffle(priority_rows)
            random.shuffle(overflow_rows)
        queue = priority_rows + overflow_rows
    elif shuffle:
        random.shuffle(queue)

    if limit and len(queue) > limit:
        queue = queue[:limit]

    return {
        "queue": queue,
        "count": len(queue),
        "total_prospects": data["row_count"],
    }


def load_prospects_by_status(
    status: str,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, Any]]:
    """Load all prospects with a specific outreach status."""
    result = load_prospect_queue(
        credentials_path,
        sheet_url,
        limit=0,
        shuffle=False,
        status_filter=status,
    )
    return result["queue"]


# ---------------------------------------------------------------------------
# Status updates
# ---------------------------------------------------------------------------


def update_prospect_status(
    prospect_row: int,
    status: str,
    outcome: str | None = None,
    notes: str | None = None,
    date_queued: str | None = None,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Update a prospect's Outreach Status (and optionally Outcome, Notes, Date Queued)."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    ws = get_worksheet(spreadsheet, PROSPECTS_TAB)

    headers = ws.row_values(1)
    header_index = {h: i + 1 for i, h in enumerate(headers)}  # 1-based for gspread

    updates = {"Outreach Status": status}
    if outcome is not None:
        updates["Outcome"] = outcome
    if notes is not None:
        updates["Notes"] = notes
    if date_queued is not None:
        updates["Date Queued"] = date_queued

    for field, value in updates.items():
        if field in header_index:
            ws.update_cell(prospect_row, header_index[field], value)

    return {
        "updated": True,
        "row": prospect_row,
        "fields": updates,
    }


def _activity_column_name(prospect: dict[str, Any]) -> str:
    engaged_person = str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1"
    return "P2 Activity" if engaged_person == "Person 2" else "P1 Activity"


def build_prospect_activity_fields(
    prospect: dict[str, Any],
    activity_value: str,
) -> dict[str, Any]:
    """Build the exact Prospects activity field payload for a lead."""
    return {
        _activity_column_name(prospect): _normalize_activity_value(activity_value),
    }


def apply_prospect_fields(
    prospect_row: int,
    fields: dict[str, Any],
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Apply an exact field payload to a Prospects row."""
    return update_row(credentials_path, sheet_url, PROSPECTS_TAB, prospect_row, fields)


def apply_outreach_log_fields(
    log_row: int,
    fields: dict[str, Any],
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Apply an exact field payload to an Outreach Log row."""
    return update_row(credentials_path, sheet_url, OUTREACH_LOG_TAB, log_row, fields)


def write_prospect_activity(
    prospect: dict[str, Any],
    activity_value: str,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Write normalized dropdown-compatible activity value to the engaged column."""
    row_number = int(prospect["_row_number"])
    fields = build_prospect_activity_fields(prospect, activity_value)
    apply_prospect_fields(
        prospect_row=row_number,
        fields=fields,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )
    field_name, normalized_value = next(iter(fields.items()))
    return {
        "updated": True,
        "row": row_number,
        "field": field_name,
        "value": normalized_value,
    }


def build_connection_sent_fields(
    prospect: dict[str, Any],
    notes: str | None = None,
    touch_method: str = "LinkedIn",
    today_value: str | None = None,
) -> dict[str, Any]:
    """Build the exact Prospects post-send payload from the local prospect snapshot."""
    engaged_person = str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1"
    today = today_value or date.today().strftime("%Y-%m-%d")
    existing_notes = str(prospect.get("notes", "")).strip()
    note_fragment = notes or f"8:30 connection request sent {today}"
    merged_notes = existing_notes
    if note_fragment:
        merged_notes = f"{existing_notes} | {note_fragment}" if existing_notes else note_fragment

    return {
        "Engaged Person": engaged_person,
        "Touch Method": touch_method,
        "Outreach Status": STATUS_CONN_SENT,
        "Outcome": "Pending",
        "Date Queued": today,
        "Notes": merged_notes,
    }


def record_connection_sent(
    prospect: dict[str, Any],
    notes: str | None = None,
    touch_method: str = "LinkedIn",
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Write the post-send reporting fields for a successful connection request.

    This preserves upstream prospect identity fields and fills the operational
    reporting columns that the morning outreach block owns.
    """
    row_number = int(prospect["_row_number"])
    updates = build_connection_sent_fields(
        prospect=prospect,
        notes=notes,
        touch_method=touch_method,
    )
    apply_prospect_fields(
        prospect_row=row_number,
        fields=updates,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )

    return {
        "updated": True,
        "row": row_number,
        "fields": updates,
    }


def mark_connection_sent(
    prospect_row: int,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Mark a prospect as having received a connection request."""
    today = date.today().strftime("%Y-%m-%d")
    return update_prospect_status(
        prospect_row,
        status=STATUS_CONN_SENT,
        outcome="Pending",
        date_queued=today,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )


def mark_connected(
    prospect_row: int,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Mark a prospect as connected without changing the response outcome."""
    return update_prospect_status(
        prospect_row,
        status=STATUS_CONNECTED,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )


# ---------------------------------------------------------------------------
# Outreach event logging
# ---------------------------------------------------------------------------


def log_outreach_event(
    prospect_id: str,
    company: str,
    person_engaged: str,
    contact_name: str,
    action: str,
    touch_method: str = "",
    outcome: str = "Pending",
    notes: str = "",
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Insert a row near the top of the Outreach Log tab."""
    row_data = build_outreach_log_row(
        prospect_id=prospect_id,
        company=company,
        person_engaged=person_engaged,
        contact_name=contact_name,
        action=action,
        touch_method=touch_method,
        outcome=outcome,
        notes=notes,
    )
    insert_outreach_log_row(
        row_data=row_data,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )

    return {
        "logged": True,
        "prospect_id": prospect_id,
        "action": action,
        "date": row_data["Last Action Date"],
    }


def build_outreach_log_row(
    prospect_id: str,
    company: str,
    person_engaged: str,
    contact_name: str,
    action: str,
    touch_method: str = "",
    outcome: str = "Pending",
    notes: str = "",
    action_date: str | None = None,
) -> dict[str, Any]:
    """Build the exact Outreach Log row payload."""
    today = action_date or date.today().strftime("%Y-%m-%d")
    return {
        "Prospect ID": prospect_id,
        "Company": company,
        "Person Engaged": person_engaged,
        "Contact Name": contact_name,
        "Current Progress": _normalize_log_action(action),
        "Touch Method": touch_method,
        "Outcome": outcome,
        "Last Action Date": today,
        "Notes": notes,
    }


def insert_outreach_log_row(
    row_data: dict[str, Any],
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Insert an exact Outreach Log row payload below the header."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    ws = get_worksheet(spreadsheet, OUTREACH_LOG_TAB)
    headers = ws.row_values(1)
    new_row = [row_data.get(h, "") for h in headers]
    ws.insert_row(new_row, index=2, value_input_option="USER_ENTERED")
    return {"logged": True, "row": 2, "fields": row_data}


def log_outreach_event_from_prospect(
    prospect: dict[str, Any],
    action: str,
    touch_method: str = "LinkedIn",
    outcome: str = "Pending",
    notes: str = "",
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Append an Outreach Log row by copying overlapping fields from a prospect payload."""
    return log_outreach_event(
        prospect_id=str(prospect.get("id", "")).strip(),
        company=str(prospect.get("company", "")).strip(),
        person_engaged=str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1",
        contact_name=str(prospect.get("contact_name", "")).strip(),
        action=action,
        touch_method=touch_method,
        outcome=outcome,
        notes=notes,
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )


def log_activity_review(
    prospect: dict[str, Any],
    activity_summary: str,
    timing: str,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Prepare activity-review metadata without writing an invalid Outreach Log row."""
    notes = f"activity_timing={timing}; {activity_summary}"
    return {
        "prepared": True,
        "prospect_id": str(prospect.get("id", "")).strip(),
        "notes": notes,
        "timing": timing,
    }


def _normalize_profile_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _raw_prospects_by_id(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, dict[str, Any]]:
    data = read_tab(credentials_path, sheet_url, PROSPECTS_TAB)
    return {
        str(row.get("ID", "")).strip(): row
        for row in data["rows"]
        if str(row.get("ID", "")).strip()
    }


def _prospect_from_log_row(
    log_row: dict[str, Any],
    raw_prospect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the acceptance-monitoring prospect payload from Outreach Log + optional Prospects identity."""
    raw_prospect = raw_prospect or {}
    prospect_id = str(log_row.get("Prospect ID", "") or raw_prospect.get("ID", "")).strip()
    engaged_person = (
        str(
            log_row.get("Person Engaged", "")
            or raw_prospect.get("Engaged Person", "")
            or "Person 1"
        ).strip()
        or "Person 1"
    )
    prefix = "P2" if engaged_person == "Person 2" else "P1"
    contact_linkedin = _normalize_profile_url(raw_prospect.get(f"{prefix} LinkedIn", ""))
    if contact_linkedin and not contact_linkedin.startswith("http"):
        contact_linkedin = "https://www." + contact_linkedin

    return {
        "id": prospect_id,
        "company": str(log_row.get("Company", "") or raw_prospect.get("Company", "")).strip(),
        "website": str(raw_prospect.get("Website", "")).strip(),
        "company_linkedin": str(raw_prospect.get("Company LinkedIn", "")).strip(),
        "emp_count": str(raw_prospect.get("Emp Count", "")).strip(),
        "source_tab": str(raw_prospect.get("Source Tab", "")).strip(),
        "engaged_person": engaged_person,
        "contact_name": str(
            log_row.get("Contact Name", "") or raw_prospect.get(f"{prefix} Name", "")
        ).strip(),
        "contact_title": str(raw_prospect.get(f"{prefix} Title", "")).strip(),
        "contact_linkedin": contact_linkedin,
        "contact_email": str(raw_prospect.get(f"{prefix} Email", "")).strip(),
        "contact_activity": str(raw_prospect.get(f"{prefix} Activity", "")).strip(),
        "touch_method": str(
            log_row.get("Touch Method", "") or raw_prospect.get("Touch Method", "")
        ).strip(),
        "outreach_status": str(log_row.get("Current Progress", "")).strip(),
        "outcome": str(log_row.get("Outcome", "")).strip(),
        "date_queued": str(
            log_row.get("Last Action Date", "") or raw_prospect.get("Date Queued", "")
        ).strip(),
        "notes": str(log_row.get("Notes", "")).strip(),
        "_outreach_log_row_number": log_row.get("_row_number"),
        "_row_number": log_row.get("_row_number"),
        "_raw_outreach_log": log_row,
        "_raw_prospect": raw_prospect,
    }


def load_pending_outreach_log_connections(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, Any]]:
    """Load pending connection-request records from Outreach Log for acceptance checks."""
    log_data = read_tab(credentials_path, sheet_url, OUTREACH_LOG_TAB)
    prospects_by_id = _raw_prospects_by_id(credentials_path, sheet_url)
    pending: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    connected_ids = {
        str(row.get("Prospect ID", "")).strip()
        for row in log_data["rows"]
        if str(row.get("Prospect ID", "")).strip()
        and str(row.get("Current Progress", "")).strip() == STATUS_CONNECTED
    }

    for row in log_data["rows"]:
        prospect_id = str(row.get("Prospect ID", "")).strip()
        progress = str(row.get("Current Progress", "")).strip()
        if progress != "Conn Request":
            continue
        if prospect_id and prospect_id in connected_ids:
            continue
        if prospect_id and prospect_id in seen_ids:
            continue
        raw_prospect = prospects_by_id.get(prospect_id, {})
        pending.append(_prospect_from_log_row(row, raw_prospect))
        if prospect_id:
            seen_ids.add(prospect_id)

    return pending


def mark_outreach_log_connected(
    log_row: int,
    notes: str = "",
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Mark the existing Outreach Log connection-request row as connected."""
    today = date.today().strftime("%Y-%m-%d")
    fields: dict[str, Any] = {
        "Current Progress": STATUS_CONNECTED,
        "Last Action Date": today,
    }
    if notes:
        fields["Notes"] = notes
    return apply_outreach_log_fields(log_row, fields, credentials_path, sheet_url)


def _pipeline_row_from_acceptance(prospect: dict[str, Any], notes: str = "") -> dict[str, Any]:
    raw_prospect = prospect.get("_raw_prospect") or {}
    raw_log = prospect.get("_raw_outreach_log") or {}
    today = date.today().strftime("%Y-%m-%d")
    engaged_person = str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1"
    prefix = "P2" if engaged_person == "Person 2" else "P1"
    return {
        "ID": prospect.get("id", ""),
        "Prospect ID": prospect.get("id", ""),
        "Company": prospect.get("company", ""),
        "Website": prospect.get("website", "") or raw_prospect.get("Website", ""),
        "Company LinkedIn": prospect.get("company_linkedin", "")
        or raw_prospect.get("Company LinkedIn", ""),
        "Emp Count": prospect.get("emp_count", "") or raw_prospect.get("Emp Count", ""),
        "Source Tab": prospect.get("source_tab", "") or raw_prospect.get("Source Tab", ""),
        "Person Engaged": engaged_person,
        "Engaged Person": engaged_person,
        "Contact Name": prospect.get("contact_name", ""),
        "Contact Title": prospect.get("contact_title", "")
        or raw_prospect.get(f"{prefix} Title", ""),
        "Contact LinkedIn": prospect.get("contact_linkedin", "")
        or raw_prospect.get(f"{prefix} LinkedIn", ""),
        "Contact Email": prospect.get("contact_email", "")
        or raw_prospect.get(f"{prefix} Email", ""),
        "Current Progress": STATUS_CONNECTED,
        "Outreach Status": STATUS_CONNECTED,
        "Touch Method": prospect.get("touch_method", "") or "LinkedIn",
        "Outcome": raw_log.get("Outcome", prospect.get("outcome", "")),
        "Date Queued": prospect.get("date_queued", "") or raw_prospect.get("Date Queued", ""),
        "Connected Date": today,
        "Last Action Date": today,
        "Notes": notes or raw_log.get("Notes", ""),
    }


def append_pipeline_row_for_acceptance(
    prospect: dict[str, Any],
    notes: str = "",
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Append an accepted/connected lead to Pipeline, skipping duplicate Prospect IDs."""
    row_data = _pipeline_row_from_acceptance(prospect, notes=notes)
    existing = read_tab(credentials_path, sheet_url, PIPELINE_TAB)
    prospect_id = str(row_data.get("Prospect ID") or row_data.get("ID") or "").strip()
    if prospect_id:
        for row in existing["rows"]:
            if str(row.get("Prospect ID") or row.get("ID") or "").strip() == prospect_id:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_in_pipeline",
                    "prospect_id": prospect_id,
                }
    appended = append_row(credentials_path, sheet_url, PIPELINE_TAB, row_data)
    return {"ok": True, "skipped": False, "prospect_id": prospect_id, "appended": appended}


def count_pipeline_connected_leads(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Count unique accepted/connected leads from the Pipeline tab.

    Pipeline is the source of truth for accepted LinkedIn connections. Any
    populated Pipeline lead row is treated as connected because only accepted
    leads belong here, even if the row has advanced beyond "Connected".
    """
    data = read_tab(credentials_path, sheet_url, PIPELINE_TAB)
    connected_ids: set[str] = set()
    connected_rows = 0

    for row in data["rows"]:
        prospect_id = str(row.get("Prospect ID") or row.get("ID") or "").strip()
        contact_linkedin = str(row.get("Contact LinkedIn") or "").strip()
        contact_name = str(row.get("Contact Name") or "").strip()
        identifier = prospect_id or contact_linkedin or contact_name
        if not identifier:
            continue

        connected_rows += 1
        connected_ids.add(identifier)

    return {
        "ok": True,
        "source": "op_bruteforce_pipeline",
        "connected": len(connected_ids),
        "connected_rows": connected_rows,
        "unique_ids": len(connected_ids),
    }


def count_outreach_log_connection_requests(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Count unique LinkedIn connection requests from Outreach Log.

    Outreach Log is the source of truth for sent connection requests. Accepted
    requests can later be updated from "Conn Request" to "Connected", so both
    statuses are counted as original sent requests.
    """
    data = read_tab(credentials_path, sheet_url, OUTREACH_LOG_TAB)
    sent_ids: set[str] = set()
    sent_rows = 0
    sent_statuses = {"Conn Request", STATUS_CONNECTED}

    for row in data["rows"]:
        touch_method = str(row.get("Touch Method", "")).strip()
        progress = str(row.get("Current Progress", "")).strip()
        if touch_method != "LinkedIn" or progress not in sent_statuses:
            continue

        prospect_id = str(row.get("Prospect ID") or row.get("ID") or "").strip()
        contact_name = str(row.get("Contact Name") or "").strip()
        company = str(row.get("Company") or "").strip()
        identifier = prospect_id or f"{company}|{contact_name}".strip("|")
        if not identifier:
            continue

        sent_rows += 1
        sent_ids.add(identifier)

    return {
        "ok": True,
        "source": "op_bruteforce_outreach_log",
        "sent": len(sent_ids),
        "sent_rows": sent_rows,
        "unique_ids": len(sent_ids),
    }


# ---------------------------------------------------------------------------
# Template management
# ---------------------------------------------------------------------------

_CATEGORY_ALIASES = {
    "CR": "Connection Request",
    "FM": "First Message",
    "FU": "Follow-Up",
    "EM": "Email",
}


def load_templates(
    category: str | None = None,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, str]]:
    """Load message templates from the Templates tab.

    category can be a full name ("Connection Request", "First Message", "Follow-Up")
    or a short prefix ("CR", "FM", "FU"). Prefix matching on template_id is used
    as a fallback so that --category CR matches CR-01, CR-02, etc.
    """
    resolved_category = _CATEGORY_ALIASES.get(category, category) if category else None

    data = read_tab(credentials_path, sheet_url, TEMPLATES_TAB)
    templates = []
    for row in data["rows"]:
        t = {
            "template_id": str(row.get("Template ID", "")).strip(),
            "category": str(row.get("Category", "")).strip(),
            "template_name": str(row.get("Template Name", "")).strip(),
            "subject": str(row.get("Subject/Note", "")).strip(),
            "body": str(row.get("Message Body", "")).strip(),
            "placeholders": str(row.get("Placeholders", "")).strip(),
            "usage_rules": str(row.get("Usage Rules", "")).strip(),
        }
        if not t["template_id"]:
            continue
        if resolved_category:
            category_match = t["category"].lower() == resolved_category.lower()
            prefix_match = category and t["template_id"].upper().startswith(category.upper() + "-")
            if not category_match and not prefix_match:
                continue
        templates.append(t)
    return templates


def pick_connection_template(
    prospect: dict[str, Any],
    templates: list[dict] | None = None,
    credentials_path: str = CREDS_PATH,
) -> dict[str, str] | None:
    """Pick the best connection request template for a prospect.

    Returns the template dict or None if no note should be sent.

    Selection logic:
    - CR-02 (Mutual Interest) if prospect is active
    - CR-03 (Direct Observation) if company has weak website indicators
    - CR-01 (Value Hook) as default
    """
    if templates is None:
        templates = load_templates("Connection Request", credentials_path)

    cr_templates = {t["template_id"]: t for t in templates}

    activity = prospect.get("contact_activity", "").lower()

    # Selection logic per Usage Rules
    if "active" in activity or "very active" in activity:
        chosen = cr_templates.get("CR-02")
    else:
        chosen = cr_templates.get("CR-01")

    # CR-03 could be chosen if website analysis suggests weakness
    # (requires external input — agent decides this)

    return chosen


def render_template(
    template: dict[str, str],
    variables: dict[str, str],
) -> str:
    """Render a template body by substituting {placeholder} values."""
    body = template["body"]
    for key, value in variables.items():
        body = body.replace(f"{{{key}}}", value)
    return body


def build_template_variables(prospect: dict[str, Any]) -> dict[str, str]:
    """Build the standard template variable dict from a prospect."""
    first_name = prospect.get("contact_name", "").split()[0] if prospect.get("contact_name") else ""
    return {
        "first_name": first_name,
        "company": prospect.get("company", ""),
        "industry": "financial services",  # default ICP industry
        "staff_range": prospect.get("emp_count", ""),
    }


# ---------------------------------------------------------------------------
# Acceptance tracking
# ---------------------------------------------------------------------------


def get_pending_connections(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, Any]]:
    """Get all prospects with 'Connection Sent' status (awaiting acceptance)."""
    return load_prospects_by_status(STATUS_CONN_SENT, credentials_path, sheet_url)


def get_connected_needing_first_message(
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, Any]]:
    """Get prospects who accepted but haven't been sent a first message."""
    return load_prospects_by_status(STATUS_CONNECTED, credentials_path, sheet_url)


def get_stale_pending_connections(
    min_days: int = 9,
    max_days: int = 11,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> list[dict[str, Any]]:
    """Get prospects with 'Connection Sent' status where Date Queued is 9-11 days ago.

    These are candidates for withdrawal — the connection was not accepted
    within the expected timeframe.

    Returns list of prospect dicts with _row_number for updates.
    """
    pending = load_prospects_by_status(STATUS_CONN_SENT, credentials_path, sheet_url)
    today = date.today()
    stale = []

    for prospect in pending:
        date_queued_str = prospect.get("date_queued", "").strip()
        if not date_queued_str:
            continue

        # Parse Date Queued — support common formats
        queued_date = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y"):
            try:
                queued_date = datetime.strptime(date_queued_str, fmt).date()
                break
            except ValueError:
                continue

        if not queued_date:
            continue

        days_pending = (today - queued_date).days
        if min_days <= days_pending <= max_days:
            prospect["days_pending"] = days_pending
            stale.append(prospect)

    return stale


# ---------------------------------------------------------------------------
# Withdrawal tracking
# ---------------------------------------------------------------------------

STATUS_WITHDRAWN = "Withdrawn"


def mark_withdrawn(
    prospect_row: int,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Mark a prospect as withdrawn (connection request pulled back)."""
    today = date.today().strftime("%Y-%m-%d")
    return update_prospect_status(
        prospect_row,
        status=STATUS_WITHDRAWN,
        outcome="Withdrawn",
        notes=f"Auto-withdrawn {today}",
        credentials_path=credentials_path,
        sheet_url=sheet_url,
    )


# ---------------------------------------------------------------------------
# Daily metrics
# ---------------------------------------------------------------------------


def log_daily_metrics(
    conn_req_sent: int = 0,
    conn_acc: int = 0,
    first_msgs: int = 0,
    follow_ups: int = 0,
    profile_views: int = 0,
    engagement_opps: int = 0,
    credentials_path: str = CREDS_PATH,
    sheet_url: str = OBF_SHEET_URL,
) -> dict[str, Any]:
    """Append today's metrics to the Daily Metrics tab."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    ws = get_worksheet(spreadsheet, DAILY_METRICS_TAB)

    headers = ws.row_values(1)
    today = date.today().strftime("%Y-%m-%d")

    row_data = {
        "Date": today,
        "Conn Req Sent": str(conn_req_sent),
        "Conn Accepted": str(conn_acc),
        "First Messages": str(first_msgs),
        "Follow-Ups": str(follow_ups),
        "Profile Views": str(profile_views),
        "Engagement Opps": str(engagement_opps),
    }

    new_row = []
    for h in headers:
        new_row.append(row_data.get(h, ""))

    ws.append_row(new_row, value_input_option="USER_ENTERED")

    return {"logged": True, "date": today, "metrics": row_data}


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Outreach helper — OBF sheet integration")
    sub = parser.add_subparsers(dest="command")

    # load-queue
    lq = sub.add_parser("load-queue", help="Load prospect queue for connection requests")
    lq.add_argument("--limit", type=int, default=30, help="Max prospects to load")
    lq.add_argument("--no-shuffle", action="store_true", help="Don't shuffle the queue")

    # update-status
    us = sub.add_parser("update-status", help="Update prospect outreach status")
    us.add_argument("--row", type=int, required=True, help="Prospect row number")
    us.add_argument("--status", required=True, help="New outreach status")
    us.add_argument("--outcome", help="Outcome value")
    us.add_argument("--notes", help="Notes to add")

    # mark-sent
    ms = sub.add_parser("mark-sent", help="Mark prospect as connection sent")
    ms.add_argument("--row", type=int, required=True, help="Prospect row number")

    # mark-connected
    mc = sub.add_parser("mark-connected", help="Mark prospect as connected/accepted")
    mc.add_argument("--row", type=int, required=True, help="Prospect row number")

    # log-event
    le = sub.add_parser("log-event", help="Log an outreach event")
    le.add_argument("--prospect-id", required=True)
    le.add_argument("--company", required=True)
    le.add_argument("--person", default="Person 1")
    le.add_argument("--contact", required=True)
    le.add_argument("--action", required=True)
    le.add_argument("--method", default="")
    le.add_argument("--outcome", default="Pending")
    le.add_argument("--notes", default="")

    # log-activity
    la = sub.add_parser("log-activity", help="Log a LinkedIn activity review")
    la.add_argument("--prospect-json", required=True, help="Prospect JSON object")
    la.add_argument("--summary", required=True, help="Short activity summary")
    la.add_argument("--timing", required=True, choices=["instant", "post_conn"])

    # load-templates
    lt = sub.add_parser("load-templates", help="Load message templates")
    lt.add_argument("--category", help="Filter by category")

    # render-template
    rt = sub.add_parser("render-template", help="Render a template with variables")
    rt.add_argument("--template-id", required=True)
    rt.add_argument("--vars", required=True, help="JSON dict of template variables")

    # pending-connections
    sub.add_parser("pending-connections", help="List prospects awaiting acceptance")

    # needs-first-message
    sub.add_parser("needs-first-message", help="List connected prospects needing first message")

    # stale-pending
    sp = sub.add_parser("stale-pending", help="List pending connections ready for withdrawal")
    sp.add_argument("--min-days", type=int, default=9, help="Min days pending (default 9)")
    sp.add_argument("--max-days", type=int, default=11, help="Max days pending (default 11)")

    # mark-withdrawn
    mw = sub.add_parser("mark-withdrawn", help="Mark prospect as withdrawn")
    mw.add_argument("--row", type=int, required=True, help="Prospect row number")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "load-queue":
        result = load_prospect_queue(
            limit=args.limit,
            shuffle=not args.no_shuffle,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "update-status":
        result = update_prospect_status(
            args.row,
            args.status,
            outcome=args.outcome,
            notes=args.notes,
        )
        print(json.dumps(result, indent=2))

    elif args.command == "mark-sent":
        result = mark_connection_sent(args.row)
        print(json.dumps(result, indent=2))

    elif args.command == "mark-connected":
        result = mark_connected(args.row)
        print(json.dumps(result, indent=2))

    elif args.command == "log-event":
        result = log_outreach_event(
            args.prospect_id,
            args.company,
            args.person,
            args.contact,
            args.action,
            args.method,
            args.outcome,
            args.notes,
        )
        print(json.dumps(result, indent=2))

    elif args.command == "log-activity":
        result = log_activity_review(
            json.loads(args.prospect_json),
            args.summary,
            args.timing,
        )
        print(json.dumps(result, indent=2))

    elif args.command == "load-templates":
        templates = load_templates(category=args.category)
        print(json.dumps(templates, indent=2, ensure_ascii=False))

    elif args.command == "render-template":
        templates = load_templates()
        tmpl = None
        for t in templates:
            if t["template_id"] == args.template_id:
                tmpl = t
                break
        if not tmpl:
            print(json.dumps({"error": f"Template {args.template_id} not found"}))
            sys.exit(1)
        variables = json.loads(args.vars)
        rendered = render_template(tmpl, variables)
        print(
            json.dumps(
                {"template_id": args.template_id, "rendered": rendered},
                indent=2,
                ensure_ascii=False,
            )
        )

    elif args.command == "pending-connections":
        prospects = get_pending_connections()
        print(
            json.dumps(
                {"count": len(prospects), "prospects": prospects}, indent=2, ensure_ascii=False
            )
        )

    elif args.command == "needs-first-message":
        prospects = get_connected_needing_first_message()
        print(
            json.dumps(
                {"count": len(prospects), "prospects": prospects}, indent=2, ensure_ascii=False
            )
        )

    elif args.command == "stale-pending":
        stale = get_stale_pending_connections(
            min_days=args.min_days,
            max_days=args.max_days,
        )
        print(json.dumps({"count": len(stale), "prospects": stale}, indent=2, ensure_ascii=False))

    elif args.command == "mark-withdrawn":
        result = mark_withdrawn(args.row)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
