"""Outreach Control sheet: the daily approval and progress contract.

The control tab is the source of truth for targets, approval, and progress.
This module owns its schema self-healing, row reading, auto-creation, and the
dashboard's configure/progress write paths.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path
from typing import Any

from gspread.exceptions import WorksheetNotFound
from sheets_helper import (
    format_sheet_date,
    get_client,
    get_worksheet,
    is_checked_value,
    open_sheet,
    safe_number,
    sheet_values_equal,
    update_row,
)

from outbound.outreach.lanes import _balanced_lane_targets
from outbound.outreach.paths import OBF_SHEET_URL
from outbound.outreach.policy import (
    DAILY_CONN_REQ_LIMIT,
    DEFAULT_LANE_SPLIT_MARKER,
    DEFAULT_TARGET,
    OUTREACH_CONTROL_HEADERS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    OUTREACH_CONTROL_TAB,
    OUTREACH_CONTROL_TERMINAL_STATUSES,
)
from outbound.shared.dates import _parse_date, sheet_date
from outbound.shared.sheetutils import _col_to_a1


def _extract_target(task_row: dict[str, Any], default: int = DEFAULT_TARGET) -> int:
    text = " ".join(
        str(task_row.get(key, ""))
        for key in ("Task Description", "Notes")
        if str(task_row.get(key, "")).strip()
    )
    match = re.search(r"\b(\d+)\b", text)
    if match:
        return int(match.group(1))
    return default


def _find_conn_req_row(task_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in task_rows:
        if str(row.get("Progress Key", "")).strip() == "conn_req":
            return row
    for row in task_rows:
        task = str(row.get("Task Description", "")).lower()
        if "connection request" in task:
            return row
    return None


def _ensure_outreach_control_tab(creds: str, obf_url: str) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    created = False
    try:
        worksheet = get_worksheet(spreadsheet, OUTREACH_CONTROL_TAB)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=OUTREACH_CONTROL_TAB,
            rows=200,
            cols=len(OUTREACH_CONTROL_HEADERS),
        )
        created = True

    headers = worksheet.row_values(1)
    if not headers:
        worksheet.update(
            range_name=f"A1:{_col_to_a1(len(OUTREACH_CONTROL_HEADERS))}1",
            values=[OUTREACH_CONTROL_HEADERS],
            value_input_option="USER_ENTERED",
        )
        headers = OUTREACH_CONTROL_HEADERS[:]
    missing = [header for header in OUTREACH_CONTROL_HEADERS if header not in headers]
    if missing:
        existing_len = len(headers)
        start_col = existing_len + 1
        end_col = existing_len + len(missing)
        worksheet.update(
            range_name=f"{_col_to_a1(start_col)}1:{_col_to_a1(end_col)}1",
            values=[missing],
            value_input_option="USER_ENTERED",
        )
        headers = headers + missing
    return {
        "ok": True,
        "spreadsheet_title": spreadsheet.title,
        "worksheet": OUTREACH_CONTROL_TAB,
        "created": created,
        "headers": headers,
    }


def _normalize_control_status(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _control_target(row: dict[str, Any]) -> int:
    effective = safe_number(row.get("Effective Target", ""), None)
    if effective is not None and int(effective) > 0:
        return int(effective)
    base = safe_number(row.get("Base Target", ""), None)
    if base is None or int(base) <= 0:
        raise ValueError("Outreach Control row must define Effective Target or Base Target.")
    rollover = int(safe_number(row.get("Rollover", ""), 0) or 0)
    return max(0, int(base) + rollover)


def _control_prospects_start_row(row: dict[str, Any]) -> int | None:
    value = safe_number(row.get(OUTREACH_CONTROL_PROSPECTS_START_ROW, ""), None)
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed >= 2 else None


def _control_lane_targets(row: dict[str, Any], remaining: int | None = None) -> dict[str, int]:
    """Every two-account prep receives an explicit, target-aware lane split.

    Older Outreach Control rows did not carry a lane marker. Deriving the split
    from the remaining target keeps those rows safe too, instead of allowing an
    all-Design or all-Automation execution merely because the old note is absent.
    """
    volume = _control_target(row) if remaining is None else max(0, int(remaining))
    return _balanced_lane_targets(volume)


def _latest_successful_control_row(
    rows: list[dict[str, Any]],
    date_value: str,
) -> dict[str, Any] | None:
    """Find the newest completed control row before ``date_value``.

    A row is a successful OBF reference only once it is marked Done and its
    progress reached its effective target.  Planned/partial rows must never
    silently become the source for a new autonomous send target.
    """
    target_day = _parse_date(date_value)
    candidates: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        try:
            row_day = _parse_date(str(row.get("Date", "")))
            target = _control_target(row)
        except (TypeError, ValueError):
            continue
        progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
        if (
            row_day < target_day
            and _normalize_control_status(row.get("Status", "")) == "done"
            and progress >= target
        ):
            candidates.append((row_day, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _append_auto_created_control_row(
    worksheet: Any,
    headers: list[str],
    rows: list[dict[str, Any]],
    date_value: str,
) -> dict[str, Any]:
    """Append an approved weekday control row when the daily row is missing."""
    source = _latest_successful_control_row(rows, date_value)
    source_target: int | None = None
    source_start_row: int | None = None
    source_date = ""
    if source:
        source_target = _control_target(source)
        source_start_row = _control_prospects_start_row(source)
        source_date = format_sheet_date(source.get("Date", ""))

    target = source_target if source_target and source_target > 0 else DEFAULT_TARGET
    lane_targets = _balanced_lane_targets(target)
    lane_note = f" {DEFAULT_LANE_SPLIT_MARKER}" if lane_targets else ""
    note = (
        f"Auto-created for {date_value} from completed Outreach Control row {source_date}; "
        f"target copied as {target}.{lane_note}"
        if source_date
        else f"Auto-created for {date_value}; no completed prior row found, using default target {target}.{lane_note}"
    )
    data: dict[str, Any] = {
        "Date": date_value,
        "Base Target": str(target),
        "Rollover": "0",
        "Effective Target": str(target),
        "Current Progress": "0",
        "Status": "Planned",
        "Approved": "TRUE",
        OUTREACH_CONTROL_PROSPECTS_START_ROW: str(source_start_row) if source_start_row else "",
        "Notes": note,
    }
    worksheet.append_row(
        [data.get(header, "") for header in headers],
        value_input_option="USER_ENTERED",
    )
    return {
        "created": True,
        "source_date": source_date or None,
        "target_source": "previous_completed_row" if source else "default",
        "target": target,
        "prospects_start_row": source_start_row,
        "lane_targets": lane_targets,
        "notes": note,
    }


def _read_outreach_control(
    creds: str,
    obf_url: str,
    date_value: str,
    mock_daily: dict[str, Any] | None = None,
    auto_create_missing: bool = False,
    auto_approve_existing: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    if mock_daily:
        # Compatibility shim for existing dry/unit fixtures.
        if "control_row" in mock_daily:
            row = mock_daily["control_row"]
            row.setdefault("_row_number", 2)
            status = _normalize_control_status(row.get("Status", ""))
            approved = (
                is_checked_value(row.get("Approved", ""))
                or status in OUTREACH_CONTROL_TERMINAL_STATUSES
            )
            target = _control_target(row)
            current_progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
            remaining = max(0, target - current_progress)
            prospects_start_row = _control_prospects_start_row(row)
            approval = {
                "worksheet": OUTREACH_CONTROL_TAB,
                "date": date_value,
                "approved": approved,
                "approval_value": row.get("Approved", ""),
                "status": row.get("Status", ""),
                "row_number": row.get("_row_number"),
                "prospects_start_row": prospects_start_row,
            }
            return approval, row, target, remaining
        approval = mock_daily.get("approval_state", {})
        task_rows = mock_daily.get("task_rows", [])
        conn_req_row = _find_conn_req_row(task_rows)
        if not conn_req_row:
            raise ValueError("No conn_req Daily Actions row found in mock data.")
        target = _extract_target(conn_req_row)
        current_progress = int(safe_number(conn_req_row.get("Current Progress", ""), 0))
        return approval, conn_req_row, target, max(0, target - current_progress)

    _ensure_outreach_control_tab(creds, obf_url)
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, OUTREACH_CONTROL_TAB)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{OUTREACH_CONTROL_TAB} worksheet is empty.")
    headers = values[0]
    missing = [header for header in OUTREACH_CONTROL_HEADERS if header not in headers]
    if missing:
        raise ValueError(
            f"{OUTREACH_CONTROL_TAB} is missing required columns: {', '.join(missing)}"
        )
    date_idx = headers.index("Date")
    matches: list[dict[str, Any]] = []
    for sheet_row in range(2, len(values) + 1):
        raw = values[sheet_row - 1]
        padded = raw + [""] * (len(headers) - len(raw))
        cell = padded[date_idx] if date_idx < len(padded) else ""
        if sheet_values_equal(cell, date_value):
            item = {headers[i]: padded[i] for i in range(len(headers))}
            item["_row_number"] = sheet_row
            matches.append(item)

    auto_created: dict[str, Any] | None = None
    if not matches and auto_create_missing:
        rows = []
        for sheet_row in range(2, len(values) + 1):
            raw = values[sheet_row - 1]
            padded = raw + [""] * (len(headers) - len(raw))
            item = {headers[i]: padded[i] for i in range(len(headers))}
            item["_row_number"] = sheet_row
            rows.append(item)
        auto_created = _append_auto_created_control_row(worksheet, headers, rows, date_value)

        # Re-read after the append so formatting and row numbers come from the
        # sheet rather than from a locally assumed insertion position.
        values = worksheet.get_all_values()
        matches = []
        for sheet_row in range(2, len(values) + 1):
            raw = values[sheet_row - 1]
            padded = raw + [""] * (len(headers) - len(raw))
            cell = padded[date_idx] if date_idx < len(padded) else ""
            if sheet_values_equal(cell, date_value):
                item = {headers[i]: padded[i] for i in range(len(headers))}
                item["_row_number"] = sheet_row
                matches.append(item)

    if not matches:
        raise ValueError(
            f"No {OUTREACH_CONTROL_TAB} row found for {date_value}. "
            "Create today's row, set target, and approve it before prep."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple {OUTREACH_CONTROL_TAB} rows found for {date_value}. "
            "Keep exactly one control row per date."
        )

    row = matches[0]
    status = _normalize_control_status(row.get("Status", ""))
    auto_approved: dict[str, Any] | None = None
    if (
        auto_approve_existing
        and not is_checked_value(row.get("Approved", ""))
        and status not in OUTREACH_CONTROL_TERMINAL_STATUSES
    ):
        update = update_row(
            creds,
            obf_url,
            OUTREACH_CONTROL_TAB,
            int(row["_row_number"]),
            {"Approved": "TRUE"},
        )
        row["Approved"] = "TRUE"
        auto_approved = {
            "row_number": row["_row_number"],
            "preserved_fields": [
                "Base Target",
                "Rollover",
                "Effective Target",
                OUTREACH_CONTROL_PROSPECTS_START_ROW,
            ],
            "update": update,
        }
    target = _control_target(row)
    current_progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
    remaining = max(0, target - current_progress)
    prospects_start_row = _control_prospects_start_row(row)
    approved = (
        is_checked_value(row.get("Approved", "")) or status in OUTREACH_CONTROL_TERMINAL_STATUSES
    )
    approval = {
        "spreadsheet_title": spreadsheet.title,
        "worksheet": OUTREACH_CONTROL_TAB,
        "date": format_sheet_date(row.get("Date", date_value)),
        "approved": approved,
        "approval_value": row.get("Approved", ""),
        "status": row.get("Status", ""),
        "row_number": row.get("_row_number"),
        "target": target,
        "current_progress": current_progress,
        "remaining": remaining,
        "prospects_start_row": prospects_start_row,
    }
    if auto_created:
        approval["auto_created"] = auto_created
    if auto_approved:
        approval["auto_approved"] = auto_approved
    return approval, row, target, remaining


def _update_outreach_control_progress(
    creds: str,
    obf_url: str,
    row_number: int,
    progress: int,
    target: int,
    notes: str,
) -> dict[str, Any]:
    status = "Done" if progress >= target else "Partial"
    fields = {
        "Current Progress": str(progress),
        "Status": status,
        "Notes": notes,
    }
    result = update_row(creds, obf_url, OUTREACH_CONTROL_TAB, row_number, fields)
    result["action"] = "complete" if status == "Done" else "partial"
    return result


def configure_outreach_control(args: argparse.Namespace) -> dict[str, Any]:
    """Update the small set of daily OBF controls exposed by the local dashboard."""
    from linkedin_outreach_session import _verify_sheet_url_identity

    creds = str(Path(args.creds).expanduser())
    date_value = sheet_date(args.date)
    _verify_sheet_url_identity(args.obf_url, OBF_SHEET_URL, "Operation Brute Force")
    approval, control_row, target, remaining = _read_outreach_control(
        creds,
        args.obf_url,
        date_value,
    )

    fields: dict[str, str] = {}
    if args.daily_volume is not None:
        daily_volume = int(args.daily_volume)
        if daily_volume < 1 or daily_volume > DAILY_CONN_REQ_LIMIT:
            raise ValueError(f"Daily volume must be between 1 and {DAILY_CONN_REQ_LIMIT}.")
        current_progress = int(safe_number(control_row.get("Current Progress", ""), 0) or 0)
        if daily_volume < current_progress:
            raise ValueError(f"Daily volume cannot be below current progress ({current_progress}).")
        # The dashboard's volume is the effective work ceiling for the day.
        fields["Base Target"] = str(daily_volume)
        fields["Rollover"] = "0"
        fields["Effective Target"] = str(daily_volume)

    if args.prospects_start_row is not None:
        start_row = int(args.prospects_start_row)
        if start_row < 2:
            raise ValueError("Prospects start row must be 2 or greater.")
        fields[OUTREACH_CONTROL_PROSPECTS_START_ROW] = str(start_row)

    if args.approved is not None:
        fields["Approved"] = "TRUE" if str(args.approved).lower() == "true" else "FALSE"

    if not fields:
        return {
            "ok": True,
            "status": "unchanged",
            "date": date_value,
            "row_number": control_row.get("_row_number"),
            "approval_state": approval,
            "target_total": target,
            "target_remaining": remaining,
        }

    update = update_row(
        creds,
        args.obf_url,
        OUTREACH_CONTROL_TAB,
        int(control_row["_row_number"]),
        fields,
    )
    updated_target = int(fields.get("Effective Target", target))
    current_progress = int(safe_number(control_row.get("Current Progress", ""), 0) or 0)
    return {
        "ok": True,
        "status": "updated",
        "date": date_value,
        "row_number": control_row.get("_row_number"),
        "fields": fields,
        "target_total": updated_target,
        "target_remaining": max(0, updated_target - current_progress),
        "update": update,
    }
