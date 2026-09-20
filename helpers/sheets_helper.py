#!/usr/bin/env python3
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

DAILY_ACTIONS_TAB = "Daily Actions"
WEEKLY_TARGETS_TAB = "Weekly Targets"
INBOX_TAB = "Inbox"
DAILY_APPROVAL_COLUMN = "Approval"
SPREADSHEET_SERIAL_EPOCH = datetime(1899, 12, 30)
APPROVAL_SIGNAL_PATTERNS = [
    r"\bapproved\b",
    r"\bapprove\b",
    r"\bgo ahead\b",
    r"\bgo-ahead\b",
    r"\bproceed\b",
]
APPROVAL_BLOCK_PATTERNS = [
    r"\bnot approved\b",
    r"\bdon't\b",
    r"\bdo not\b",
    r"\bhold on\b",
    r"\bhold off\b",
    r"\bwait\b",
    r"\bstop\b",
    r"\bskip\b",
    r"\bno\b",
]

# Inbox fields that map directly to the same-named Daily Actions column
INBOX_TO_DAILY_FIELDS = [
    "Task Description",  # handled separately with priority logic
    "Owner",
    "Task Behavior",
    "Priority",
    "Notes",
]

# Inbox fields that need renaming when copied into Daily Actions
# Note: Inbox "Due Date" is a calendar date, not a time-of-day — no clean Daily Actions target exists.
INBOX_TO_DAILY_REMAP: dict[str, str] = {}


def _maybe_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def parse_sheet_date(s: Any):
    """Try to parse a date string from the sheet. Returns datetime.date or None."""
    value = str(s).strip()
    if not value:
        return None
    numeric = _maybe_float(value)
    # Modern Google Sheets/Excel date serials are large positive numbers.
    if numeric is not None and numeric >= 20000:
        try:
            return (SPREADSHEET_SERIAL_EPOCH + timedelta(days=numeric)).date()
        except OverflowError:
            pass
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def safe_number(s, default=0):
    """Parse a string to a number, returning default if not parseable."""
    try:
        v = float(str(s).strip())
        return int(v) if v == int(v) else v
    except (ValueError, TypeError):
        return default


def parse_sheet_time(s: Any):
    """Parse a sheet time string into a datetime.time-like sortable value."""
    value = str(s).strip()
    if not value:
        return None
    numeric = _maybe_float(value)
    if numeric is not None and 0 <= numeric < 1:
        try:
            return (SPREADSHEET_SERIAL_EPOCH + timedelta(days=numeric)).time()
        except OverflowError:
            pass
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def format_sheet_date(value: Any) -> str:
    parsed = parse_sheet_date(value)
    if parsed is None:
        return str(value).strip()
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def format_sheet_time(value: Any) -> str:
    parsed = parse_sheet_time(value)
    if parsed is None:
        return str(value).strip()
    return parsed.strftime("%I:%M:%S %p").lstrip("0")


def normalize_owner_label(owner: str) -> str:
    value = str(owner).strip()
    if value.startswith("[") and value.endswith("]") and len(value) >= 2:
        return value[1:-1].strip()
    return value


def is_checked_value(value: Any) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"true", "yes", "y", "1", "checked"}


def sheet_values_equal(actual: Any, expected: Any) -> bool:
    actual_date = parse_sheet_date(actual)
    expected_date = parse_sheet_date(expected)
    if actual_date is not None and expected_date is not None:
        return actual_date == expected_date
    return str(actual).strip() == str(expected).strip()


def require_columns(headers: list[str], required: list[str], context: str) -> None:
    missing = [col for col in required if col not in headers]
    if missing:
        raise ValueError(f"{context} is missing required columns: {', '.join(missing)}")


def normalize_message_text(message: str) -> str:
    return re.sub(r"\s+", " ", str(message).strip().lower())


def detect_approval_intent(message: str) -> dict[str, Any]:
    normalized = normalize_message_text(message)
    if not normalized:
        return {"intent": "unknown", "matched_pattern": None, "normalized_message": normalized}

    for pattern in APPROVAL_BLOCK_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "intent": "blocked",
                "matched_pattern": pattern,
                "normalized_message": normalized,
            }

    for pattern in APPROVAL_SIGNAL_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "intent": "approved",
                "matched_pattern": pattern,
                "normalized_message": normalized,
            }

    return {"intent": "unknown", "matched_pattern": None, "normalized_message": normalized}


# ------------------------------------------------------------------
# Core access
# ------------------------------------------------------------------


def get_client(credentials_path: str) -> gspread.Client:
    if not str(credentials_path or "").strip():
        raise ValueError("Google Sheets credentials are required. Set GOOGLE_SHEETS_CREDENTIALS.")
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(client: gspread.Client, sheet_url: str):
    if not str(sheet_url or "").strip():
        raise ValueError(
            "Google Sheet URL is required. Set the relevant *_SHEET_URL variable or pass the CLI flag."
        )
    return client.open_by_url(sheet_url)


def get_worksheet(spreadsheet, worksheet_name: str):
    return spreadsheet.worksheet(worksheet_name)


def normalize_rows(values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    headers = values[0]
    rows = values[1:]
    result: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=2):
        padded = row + [""] * (len(headers) - len(row))
        item = {headers[i]: padded[i] for i in range(len(headers))}
        item["_row_number"] = idx
        result.append(item)
    return result


def fill_grouped_rows(
    rows: list[dict[str, Any]],
    group_columns: list[str],
    content_column: str,
) -> list[dict[str, Any]]:
    current_values = {column: "" for column in group_columns}
    result = []
    for row in rows:
        for column in group_columns:
            raw_value = row.get(column, "")
            normalized_value = str(raw_value).strip()
            if column in {"Date", "Week Starting"} and normalized_value:
                normalized_value = format_sheet_date(raw_value)
            if normalized_value:
                current_values[column] = normalized_value
        for column in group_columns:
            if not str(row.get(column, "")).strip() and current_values[column]:
                row[column] = current_values[column]
        if str(row.get(content_column, "")).strip() == "":
            continue
        result.append(row)
    return result


# ------------------------------------------------------------------
# Tab access
# ------------------------------------------------------------------


def list_tabs(credentials_path: str, sheet_url: str) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    return {
        "title": spreadsheet.title,
        "tabs": [ws.title for ws in spreadsheet.worksheets()],
    }


def read_tab(
    credentials_path: str,
    sheet_url: str,
    worksheet_name: str,
    nonempty_column: str | None = None,
    equals_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, worksheet_name)
    values = worksheet.get_all_values()
    rows = normalize_rows(values)
    if worksheet_name == DAILY_ACTIONS_TAB:
        rows = fill_grouped_rows(rows, ["Date"], "Task Description")
    elif worksheet_name == WEEKLY_TARGETS_TAB:
        rows = fill_grouped_rows(rows, ["Week Starting", "Week #"], "Target Description")
    if nonempty_column:
        rows = [r for r in rows if str(r.get(nonempty_column, "")).strip() != ""]
    if equals_filters:
        for key, expected in equals_filters.items():
            rows = [r for r in rows if sheet_values_equal(r.get(key, ""), expected)]
    return {
        "spreadsheet_title": spreadsheet.title,
        "worksheet": worksheet_name,
        "row_count": len(rows),
        "rows": rows,
    }


def append_row(
    credentials_path: str,
    sheet_url: str,
    worksheet_name: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, worksheet_name)
    headers = worksheet.row_values(1)
    if not headers:
        raise ValueError("Worksheet has no header row.")
    row = [data.get(h, "") for h in headers]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    return {"ok": True, "worksheet": worksheet_name, "appended": data}


def update_row(
    credentials_path: str,
    sheet_url: str,
    worksheet_name: str,
    row_number: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, worksheet_name)
    headers = worksheet.row_values(1)
    if not headers:
        raise ValueError("Worksheet has no header row.")
    existing = worksheet.row_values(row_number)
    existing += [""] * (len(headers) - len(existing))
    updated = existing[:]
    header_index = {h: i for i, h in enumerate(headers)}
    for key, value in data.items():
        if key not in header_index:
            raise ValueError(f"Column not found: {key}")
        updated[header_index[key]] = value
    cell_range = f"A{row_number}:{gspread.utils.rowcol_to_a1(row_number, len(headers))}"
    worksheet.update(cell_range, [updated], value_input_option="USER_ENTERED")
    return {
        "ok": True,
        "worksheet": worksheet_name,
        "row_number": row_number,
        "updated_fields": data,
    }


# ------------------------------------------------------------------
# Group helpers
# ------------------------------------------------------------------


def row_exists(worksheet, column_name: str, value: str) -> bool:
    values = worksheet.get_all_values()
    if not values:
        return False
    headers = values[0]
    if column_name not in headers:
        raise ValueError(f"Column not found: {column_name}")
    idx = headers.index(column_name)
    for row in values[1:]:
        cell = row[idx] if idx < len(row) else ""
        if sheet_values_equal(cell, value):
            return True
    return False


def get_last_meaningful_row(worksheet) -> int:
    values = worksheet.get_all_values()
    last_used = 1
    for i, row in enumerate(values, start=1):
        if any(str(cell).strip() != "" for cell in row):
            last_used = i
    return last_used


def insert_rows_by_headers(worksheet, rows: list[dict[str, Any]], start_row: int) -> None:
    headers = worksheet.row_values(1)
    if not headers:
        raise ValueError("Worksheet has no header row.")
    out = []
    for data in rows:
        out.append([data.get(h, "") for h in headers])
    worksheet.insert_rows(out, row=start_row, value_input_option="USER_ENTERED")


def find_group_last_content_row(values: list[list[Any]], date_idx: int, start_row: int) -> int:
    last_content = start_row
    for sheet_row in range(start_row + 1, len(values) + 1):
        row = values[sheet_row - 1]
        cell = str(row[date_idx]).strip() if date_idx < len(row) else ""
        if cell:
            break
        if not any(str(c).strip() for c in row):
            break
        last_content = sheet_row
    return last_content


def find_date_group_bounds(
    values: list[list[Any]], date_value: str
) -> tuple[int | None, int | None]:
    """Find the start and last-content row of a date group in Daily Actions.
    Returns (start_row, last_content_row) as 1-based sheet row numbers, or (None, None)."""
    if not values or len(values) < 2:
        return None, None
    headers = values[0]
    if "Date" not in headers:
        return None, None
    date_idx = headers.index("Date")
    target = str(date_value).strip()

    start = None
    for sheet_row in range(2, len(values) + 1):
        cell = (
            str(values[sheet_row - 1][date_idx]).strip()
            if date_idx < len(values[sheet_row - 1])
            else ""
        )
        if sheet_values_equal(cell, target):
            start = sheet_row
            break

    if start is None:
        return None, None

    last_content = find_group_last_content_row(values, date_idx, start)
    return start, last_content


def get_daily_group_data(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Daily Actions worksheet is empty.")

    headers = values[0]
    require_columns(
        headers,
        ["Date", "Task Description", "Owner", "Task Behavior", "Start Time", DAILY_APPROVAL_COLUMN],
        DAILY_ACTIONS_TAB,
    )

    date_idx = headers.index("Date")
    target = str(date_value).strip()
    group_headers = []
    for sheet_row in range(2, len(values) + 1):
        row = values[sheet_row - 1]
        cell = str(row[date_idx]).strip() if date_idx < len(row) else ""
        if sheet_values_equal(cell, target):
            group_headers.append(sheet_row)

    if not group_headers:
        raise ValueError(f"Daily Actions group not found for date {date_value}")
    if len(group_headers) == 1:
        start = group_headers[0]
    else:
        approval_idx = headers.index(DAILY_APPROVAL_COLUMN)
        approved_headers = []
        for header_row in group_headers:
            row = values[header_row - 1]
            approval_value = row[approval_idx] if approval_idx < len(row) else ""
            if is_checked_value(approval_value):
                approved_headers.append(header_row)
        if len(approved_headers) == 1:
            start = approved_headers[0]
        else:
            raise ValueError(
                f"Multiple Daily Actions groups found for date {date_value}. "
                "Approval cannot be resolved safely until duplicates are fixed."
            )

    last_content = find_group_last_content_row(values, date_idx, start)

    group_row = values[start - 1] + [""] * (len(headers) - len(values[start - 1]))
    group_header = {headers[i]: group_row[i] for i in range(len(headers))}
    group_header["_row_number"] = start

    rows: list[dict[str, Any]] = []
    for sheet_row in range(start + 1, last_content + 1):
        row = values[sheet_row - 1] + [""] * (len(headers) - len(values[sheet_row - 1]))
        item = {headers[i]: row[i] for i in range(len(headers))}
        item["_row_number"] = sheet_row
        if str(item.get("Task Description", "")).strip() == "":
            continue
        rows.append(item)

    return {
        "spreadsheet_title": spreadsheet.title,
        "worksheet": DAILY_ACTIONS_TAB,
        "headers": headers,
        "group_header": group_header,
        "task_rows": rows,
        "group_start_row": start,
        "group_last_content_row": last_content,
    }


def daily_approval_state(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
) -> dict[str, Any]:
    group = get_daily_group_data(credentials_path, sheet_url, date_value)
    header = group["group_header"]
    approved = is_checked_value(header.get(DAILY_APPROVAL_COLUMN, ""))
    return {
        "spreadsheet_title": group["spreadsheet_title"],
        "worksheet": group["worksheet"],
        "date": date_value,
        "group_row_number": header["_row_number"],
        "approved": approved,
        "approval_value": header.get(DAILY_APPROVAL_COLUMN, ""),
        "task_count": len(group["task_rows"]),
    }


def build_daily_approval_packet(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
) -> dict[str, Any]:
    group = get_daily_group_data(credentials_path, sheet_url, date_value)
    header = group["group_header"]
    tasks = group["task_rows"]

    def sort_key(row: dict[str, Any]):
        parsed = parse_sheet_time(row.get("Start Time", ""))
        if parsed is None:
            return (1, 0, row["_row_number"])
        seconds = parsed.hour * 3600 + parsed.minute * 60 + parsed.second
        return (0, seconds, row["_row_number"])

    ordered = sorted(tasks, key=sort_key)

    lines = [f"{date_value} daily todo:", ""]
    packet_tasks = []
    for index, row in enumerate(ordered, start=1):
        task = str(row.get("Task Description", "")).strip()
        owner = normalize_owner_label(str(row.get("Owner", "")).strip())
        behavior = str(row.get("Task Behavior", "")).strip()
        start_time = format_sheet_time(row.get("Start Time", ""))
        lines.append(f"Task: {task}")
        lines.append(f"Task behaviour: {behavior}")
        lines.append(f"Owner: {owner}")
        if index != len(ordered):
            lines.append("")
        packet_tasks.append(
            {
                "index": index,
                "row_number": row["_row_number"],
                "task": task,
                "owner": owner,
                "task_behavior": behavior,
                "start_time": start_time,
            }
        )
    if packet_tasks:
        lines.extend(["", "Execution is blocked until approval is given."])

    return {
        "spreadsheet_title": group["spreadsheet_title"],
        "worksheet": group["worksheet"],
        "date": date_value,
        "group_row_number": header["_row_number"],
        "approved": is_checked_value(header.get(DAILY_APPROVAL_COLUMN, "")),
        "approval_value": header.get(DAILY_APPROVAL_COLUMN, ""),
        "task_count": len(packet_tasks),
        "tasks": packet_tasks,
        "message": "\n".join(lines),
    }


def set_daily_group_approval(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
    approved: bool = True,
) -> dict[str, Any]:
    state = daily_approval_state(credentials_path, sheet_url, date_value)
    result = update_row(
        credentials_path,
        sheet_url,
        DAILY_ACTIONS_TAB,
        state["group_row_number"],
        {DAILY_APPROVAL_COLUMN: "TRUE" if approved else "FALSE"},
    )
    result["date"] = date_value
    result["approved"] = approved
    result["action"] = "set_daily_group_approval"
    return result


def process_daily_approval_reply(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
    message_text: str,
    sender: str | None = None,
    allowed_senders: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = daily_approval_state(credentials_path, sheet_url, date_value)
    detection = detect_approval_intent(message_text)

    sender_allowed = True
    normalized_sender = str(sender).strip() if sender else None
    if allowed_senders:
        allowed = {str(item).strip() for item in allowed_senders if str(item).strip()}
        sender_allowed = normalized_sender in allowed if normalized_sender else False

    result: dict[str, Any] = {
        "ok": True,
        "action": "process_daily_approval_reply",
        "date": date_value,
        "sender": normalized_sender,
        "sender_allowed": sender_allowed,
        "intent": detection["intent"],
        "matched_pattern": detection["matched_pattern"],
        "normalized_message": detection["normalized_message"],
        "already_approved": state["approved"],
        "group_row_number": state["group_row_number"],
        "dry_run": dry_run,
    }

    if not sender_allowed:
        result["status"] = "ignored_sender"
        result["updated_sheet"] = False
        return result

    if detection["intent"] != "approved":
        result["status"] = "ignored_message"
        result["updated_sheet"] = False
        return result

    if state["approved"]:
        result["status"] = "already_approved"
        result["updated_sheet"] = False
        return result

    if dry_run:
        result["status"] = "would_approve"
        result["updated_sheet"] = False
        return result

    update_result = set_daily_group_approval(credentials_path, sheet_url, date_value, approved=True)
    result["status"] = "approved"
    result["updated_sheet"] = True
    result["sheet_update"] = update_result
    return result


def find_week_group_bounds(
    values: list[list[Any]], week_start: str
) -> tuple[int | None, int | None]:
    """Find the start and last-content row of a week group in Weekly Targets.
    Returns (start_row, last_content_row) as 1-based sheet row numbers, or (None, None)."""
    if not values or len(values) < 2:
        return None, None
    headers = values[0]
    if "Week Starting" not in headers:
        return None, None
    ws_idx = headers.index("Week Starting")
    target = str(week_start).strip()

    start = None
    for sheet_row in range(2, len(values) + 1):
        cell = (
            str(values[sheet_row - 1][ws_idx]).strip()
            if ws_idx < len(values[sheet_row - 1])
            else ""
        )
        if sheet_values_equal(cell, target):
            start = sheet_row
            break

    if start is None:
        return None, None

    last_content = start
    for sheet_row in range(start + 1, len(values) + 1):
        row = values[sheet_row - 1]
        cell = str(row[ws_idx]).strip() if ws_idx < len(row) else ""
        if cell:
            break
        if not any(str(c).strip() for c in row):
            break
        last_content = sheet_row

    return start, last_content


# ------------------------------------------------------------------
# Group creation
# ------------------------------------------------------------------


def create_daily_group(
    credentials_path: str,
    sheet_url: str,
    date_value: str,
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)

    if row_exists(worksheet, "Date", date_value):
        return {"ok": True, "exists": True, "worksheet": DAILY_ACTIONS_TAB, "date": date_value}

    rows = [
        {"Date": date_value},
        {
            "Task Description": "Send 20 connection requests (warmup batch)",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "On a go",
            "Priority": "High",
            "Start Time": "8:00 AM",
            "Due Time": "8:00 AM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "conn_req",
            "Status": "Pending",
            "Notes": "Morning + afternoon batches",
        },
        {
            "Task Description": "Check for new connection acceptances",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "Randomized timing",
            "Priority": "High",
            "Start Time": "10:00 AM",
            "Due Time": "10:00 AM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "conn_acc",
            "Status": "Pending",
            "Notes": "Draft first messages for approval",
        },
        {
            "Task Description": "Run engagement scan, send 10 opps to WhatsApp",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "Randomized timing",
            "Priority": "Medium",
            "Start Time": "1:00 PM",
            "Due Time": "1:00 PM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "eng_scan",
            "Status": "Pending",
            "Notes": "Surface only the strongest opportunities",
        },
        {
            "Task Description": "Review and pick 5 engagement opportunities",
            "Owner": "[AA]",
            "Task Behavior": "Recurring",
            "Execution Mode": "On a go",
            "Priority": "Medium",
            "Start Time": "1:30 PM",
            "Due Time": "1:30 PM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "eng_pick",
            "Status": "Pending",
            "Notes": "Tony reviews and chooses the best opportunities",
        },
        {
            "Task Description": "Send 4 multi-touch emails",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "On a go",
            "Priority": "Medium",
            "Start Time": "9:00 AM",
            "Due Time": "9:00 AM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "mt_email",
            "Status": "Pending",
            "Notes": "Secondary channel where valid email exists",
        },
        {
            "Task Description": "Check for pending request withdrawals",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "On a go",
            "Priority": "Low",
            "Start Time": "5:00 PM",
            "Due Time": "5:00 PM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "req_withdraw",
            "Status": "Pending",
            "Notes": "Withdraw if request age meets the rule",
        },
        {
            "Task Description": "Compile and send daily report to WhatsApp",
            "Owner": "[OC]",
            "Task Behavior": "Recurring",
            "Execution Mode": "Multiple timing",
            "Priority": "High",
            "Start Time": "6:00 PM",
            "Due Time": "6:00 PM",
            "Origin Date": date_value,
            "Current Progress": "",
            "Progress Key": "daily_report",
            "Status": "Pending",
            "Notes": "End-of-day operational summary",
        },
        {},
    ]

    last_used_row = get_last_meaningful_row(worksheet)
    insert_at = last_used_row + 1
    if insert_at >= worksheet.row_count:
        headers = worksheet.row_values(1)
        if not headers:
            raise ValueError("Worksheet has no header row.")
        out = [[row.get(h, "") for h in headers] for row in rows]
        worksheet.append_rows(out, value_input_option="USER_ENTERED")
    else:
        insert_rows_by_headers(worksheet, rows, insert_at)

    return {
        "ok": True,
        "exists": False,
        "worksheet": DAILY_ACTIONS_TAB,
        "date": date_value,
        "created_rows": len(rows),
    }


def create_weekly_group(
    credentials_path: str,
    sheet_url: str,
    week_start: str,
    week_number: int,
) -> dict[str, Any]:
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, WEEKLY_TARGETS_TAB)

    if row_exists(worksheet, "Week Starting", week_start):
        return {
            "ok": True,
            "exists": True,
            "worksheet": WEEKLY_TARGETS_TAB,
            "week_start": week_start,
        }

    rows = [
        {"Week Starting": week_start, "Week #": str(week_number)},
        {
            "Target Description": "Send 100 connection requests",
            "Task Behavior": "Recurring",
            "Status": "Pending",
            "Target Value": "100",
            "Current Progress": "",
            "Progress Key": "conn_req",
            "Notes": "Derived from daily connection request execution",
        },
        {
            "Target Description": "Send 20 multi-touch emails",
            "Task Behavior": "Recurring",
            "Status": "Pending",
            "Target Value": "20",
            "Current Progress": "",
            "Progress Key": "mt_email",
            "Notes": "Derived from daily email execution",
        },
        {
            "Target Description": "Complete daily engagement scan rhythm",
            "Task Behavior": "Recurring",
            "Status": "Pending",
            "Target Value": "5",
            "Current Progress": "",
            "Progress Key": "eng_scan",
            "Notes": "One expected scan block per working day",
        },
        {
            "Target Description": "Complete daily reporting rhythm",
            "Task Behavior": "Recurring",
            "Status": "Pending",
            "Target Value": "5",
            "Current Progress": "",
            "Progress Key": "daily_report",
            "Notes": "One expected daily report per working day",
        },
        {
            "Target Description": "Maintain request withdrawal hygiene",
            "Task Behavior": "Recurring",
            "Status": "Pending",
            "Target Value": "5",
            "Current Progress": "",
            "Progress Key": "req_withdraw",
            "Notes": "One expected withdrawal hygiene check per working day",
        },
        {},
    ]

    last_used_row = get_last_meaningful_row(worksheet)
    insert_at = last_used_row + 1
    if insert_at >= worksheet.row_count:
        headers = worksheet.row_values(1)
        if not headers:
            raise ValueError("Worksheet has no header row.")
        out = [[row.get(h, "") for h in headers] for row in rows]
        worksheet.append_rows(out, value_input_option="USER_ENTERED")
    else:
        insert_rows_by_headers(worksheet, rows, insert_at)

    return {
        "ok": True,
        "exists": False,
        "worksheet": WEEKLY_TARGETS_TAB,
        "week_start": week_start,
        "week_number": week_number,
        "created_rows": len(rows),
    }


# ------------------------------------------------------------------
# Execution update functions
# ------------------------------------------------------------------


def _update_daily_status(
    credentials_path: str,
    sheet_url: str,
    row_number: int,
    status: str,
    progress: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Internal: update a Daily Actions row's Status, Current Progress, and optionally Notes."""
    data: dict[str, Any] = {"Status": status}
    if progress is not None:
        data["Current Progress"] = progress
    if notes is not None:
        data["Notes"] = notes
    return update_row(credentials_path, sheet_url, DAILY_ACTIONS_TAB, row_number, data)


def complete_daily_row(
    credentials_path: str,
    sheet_url: str,
    row_number: int,
    progress: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a Daily Actions row as Done with the completed progress amount."""
    result = _update_daily_status(credentials_path, sheet_url, row_number, "Done", progress, notes)
    result["action"] = "complete"
    return result


def skip_daily_row(
    credentials_path: str,
    sheet_url: str,
    row_number: int,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a Daily Actions row as Skipped."""
    result = _update_daily_status(credentials_path, sheet_url, row_number, "Skipped", "0", notes)
    result["action"] = "skip"
    return result


def partial_daily_row(
    credentials_path: str,
    sheet_url: str,
    row_number: int,
    progress: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a Daily Actions row as Partial with the amount completed so far."""
    result = _update_daily_status(
        credentials_path, sheet_url, row_number, "Partial", progress, notes
    )
    result["action"] = "partial"
    return result


def roll_daily_row(
    credentials_path: str,
    sheet_url: str,
    row_number: int,
    next_date: str,
    remaining: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a Daily Actions row as Rolled Over and create a continuation row in the next day's group."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)

    # Read all values first so we can resolve the group date by scanning upward
    all_values = worksheet.get_all_values()
    headers = all_values[0] if all_values else []
    if not headers:
        raise ValueError("Worksheet has no header row.")
    header_index = {h: i for i, h in enumerate(headers)}
    original = all_values[row_number - 1] if row_number <= len(all_values) else []
    original += [""] * (len(headers) - len(original))
    original_data = {h: original[i] for h, i in header_index.items()}

    # Resolve the group date by scanning upward from row_number to the group header
    date_col_idx = header_index.get("Date")
    orig_date_str = str(original_data.get("Date", "")).strip()
    if not orig_date_str and date_col_idx is not None:
        for r in range(row_number - 1, 0, -1):  # scan upward (r is 1-based)
            cell = (
                str(all_values[r - 1][date_col_idx]).strip()
                if date_col_idx < len(all_values[r - 1])
                else ""
            )
            if cell:
                orig_date_str = cell
                break

    # Update original row: Status = Rolled Over
    update_fields: dict[str, Any] = {"Status": "Rolled Over"}
    if notes:
        update_fields["Notes"] = notes
    updated = original[:]
    for key, value in update_fields.items():
        if key in header_index:
            updated[header_index[key]] = value
    cell_range = f"A{row_number}:{gspread.utils.rowcol_to_a1(row_number, len(headers))}"
    worksheet.update(cell_range, [updated], value_input_option="USER_ENTERED")

    # Ensure next-date group exists
    if not row_exists(worksheet, "Date", next_date):
        create_daily_group(credentials_path, sheet_url, next_date)

    # Re-read worksheet values (group creation may have shifted rows)
    worksheet = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)
    values = worksheet.get_all_values()

    # Preserve Origin Date from original row (fall back to resolved group date)
    origin_date = str(original_data.get("Origin Date", "")).strip()
    if not origin_date:
        origin_date = orig_date_str

    # Build note for the rolled row
    rolled_note = f"Remaining from {orig_date_str}: {remaining}"

    new_row_data = {
        "Task Description": original_data.get("Task Description", ""),
        "Owner": original_data.get("Owner", ""),
        "Task Behavior": original_data.get("Task Behavior", ""),
        "Execution Mode": original_data.get("Execution Mode", ""),
        "Priority": original_data.get("Priority", ""),
        "Start Time": original_data.get("Start Time", ""),
        "Due Time": original_data.get("Due Time", ""),
        "Origin Date": origin_date,
        "Current Progress": "",
        "Progress Key": original_data.get("Progress Key", ""),
        "Status": "Pending",
        "Notes": rolled_note,
    }

    # Find insert point in next-date group
    start, last_content = find_date_group_bounds(values, next_date)
    if start is None:
        raise ValueError(f"Could not find date group for {next_date} after creation")
    insert_at = last_content + 1

    insert_rows_by_headers(worksheet, [new_row_data], insert_at)

    return {
        "ok": True,
        "action": "roll",
        "original_row": row_number,
        "next_date": next_date,
        "remaining": remaining,
        "origin_date": origin_date,
        "inserted_at": insert_at,
    }


def promote_inbox_row(
    credentials_path: str,
    sheet_url: str,
    inbox_row_number: int,
    target_date: str,
) -> dict[str, Any]:
    """Promote an Inbox row into the target date's Daily Actions group."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)

    # Read Inbox row
    inbox_ws = get_worksheet(spreadsheet, INBOX_TAB)
    inbox_headers = inbox_ws.row_values(1)
    if not inbox_headers:
        raise ValueError("Inbox worksheet has no header row.")
    inbox_row = inbox_ws.row_values(inbox_row_number)
    inbox_row += [""] * (len(inbox_headers) - len(inbox_row))
    inbox_data = {inbox_headers[i]: inbox_row[i] for i in range(len(inbox_headers))}

    # Resolve Task Description: Parsed Task > Task Description > Raw Message
    task_description = (
        str(inbox_data.get("Parsed Task", "")).strip()
        or str(inbox_data.get("Task Description", "")).strip()
        or str(inbox_data.get("Raw Message", "")).strip()
    )
    if not task_description:
        raise ValueError(
            f"Inbox row {inbox_row_number} has no usable task text. "
            "This may be a date/group header row rather than a task row. "
            "Target a row that has a value in Parsed Task or Raw Message."
        )

    # Build Daily Actions row from Inbox data
    daily_data: dict[str, Any] = {
        "Task Description": task_description,
        "Origin Date": target_date,
        "Status": "Pending",
        "Current Progress": "",
    }
    for field in INBOX_TO_DAILY_FIELDS:
        if field == "Task Description":
            continue  # already resolved above
        if field in inbox_data and str(inbox_data[field]).strip():
            daily_data[field] = inbox_data[field]
    for inbox_field, daily_field in INBOX_TO_DAILY_REMAP.items():
        if inbox_field in inbox_data and str(inbox_data[inbox_field]).strip():
            daily_data[daily_field] = inbox_data[inbox_field]

    # Ensure target date group exists
    daily_ws = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)
    if not row_exists(daily_ws, "Date", target_date):
        create_daily_group(credentials_path, sheet_url, target_date)
        daily_ws = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)

    # Find insert point in target date group
    values = daily_ws.get_all_values()
    start, last_content = find_date_group_bounds(values, target_date)
    if start is None:
        raise ValueError(f"Could not find date group for {target_date} after creation")
    insert_at = last_content + 1

    insert_rows_by_headers(daily_ws, [daily_data], insert_at)

    # Update Inbox row status if a Status column exists
    inbox_status_updated = False
    if "Status" in inbox_headers:
        col_num = inbox_headers.index("Status") + 1
        inbox_ws.update_cell(inbox_row_number, col_num, "Promoted")
        inbox_status_updated = True

    return {
        "ok": True,
        "action": "promote",
        "inbox_row": inbox_row_number,
        "target_date": target_date,
        "inserted_at": insert_at,
        "inbox_status_updated": inbox_status_updated,
        "promoted_fields": daily_data,
    }


def recompute_weekly_progress(
    credentials_path: str,
    sheet_url: str,
    week_start: str,
) -> dict[str, Any]:
    """Recompute Weekly Targets Current Progress from Daily Actions for a given week."""
    client = get_client(credentials_path)
    spreadsheet = open_sheet(client, sheet_url)

    # Parse week date range
    ws_date = parse_sheet_date(week_start)
    if ws_date is None:
        raise ValueError(f"Cannot parse week start date: {week_start}")
    week_end = ws_date + timedelta(days=6)

    # Read Daily Actions with grouped date fill
    daily_ws = get_worksheet(spreadsheet, DAILY_ACTIONS_TAB)
    daily_values = daily_ws.get_all_values()
    daily_rows = normalize_rows(daily_values)
    daily_rows = fill_grouped_rows(daily_rows, ["Date"], "Task Description")

    # Sum Current Progress by Progress Key for rows within the week
    progress_sums: dict[str, float] = {}
    for row in daily_rows:
        row_date = parse_sheet_date(str(row.get("Date", "")))
        if row_date is None:
            continue
        if not (ws_date <= row_date <= week_end):
            continue
        key = str(row.get("Progress Key", "")).strip()
        if not key:
            continue
        val = safe_number(row.get("Current Progress", ""), 0)
        progress_sums[key] = progress_sums.get(key, 0) + val

    # Read Weekly Targets and find the week group
    weekly_ws = get_worksheet(spreadsheet, WEEKLY_TARGETS_TAB)
    weekly_values = weekly_ws.get_all_values()
    weekly_headers = weekly_values[0] if weekly_values else []

    if "Progress Key" not in weekly_headers or "Current Progress" not in weekly_headers:
        raise ValueError("Weekly Targets missing Progress Key or Current Progress column")

    start, last_content = find_week_group_bounds(weekly_values, week_start)
    if start is None:
        raise ValueError(f"Weekly group not found for week starting {week_start}")

    pk_idx = weekly_headers.index("Progress Key")
    cp_idx = weekly_headers.index("Current Progress")

    # Update matching rows
    updates = []
    for sheet_row in range(start, last_content + 1):
        row = weekly_values[sheet_row - 1]
        key = str(row[pk_idx]).strip() if pk_idx < len(row) else ""
        if not key or key not in progress_sums:
            continue
        new_val = progress_sums[key]
        display = str(int(new_val)) if new_val == int(new_val) else str(new_val)
        weekly_ws.update_cell(sheet_row, cp_idx + 1, display)
        updates.append({"row": sheet_row, "progress_key": key, "value": display})

    # Format sums for output
    formatted_sums = {}
    for k, v in progress_sums.items():
        formatted_sums[k] = int(v) if v == int(v) else v

    return {
        "ok": True,
        "action": "recompute_weekly",
        "week_start": week_start,
        "date_range": f"{ws_date} to {week_end}",
        "daily_sums": formatted_sums,
        "updates": updates,
    }


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Google Sheets helper for Sentinel")
    parser.add_argument(
        "--creds",
        default="~/.openclaw/credentials/google-sheets.json",
        help="Path to service account JSON",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- Existing commands ---

    p_tabs = subparsers.add_parser("list-tabs")
    p_tabs.add_argument("--sheet-url", required=True)

    p_read = subparsers.add_parser("read-tab")
    p_read.add_argument("--sheet-url", required=True)
    p_read.add_argument("--tab", required=True)
    p_read.add_argument("--nonempty-column")
    p_read.add_argument(
        "--equals",
        action="append",
        default=[],
        help='Exact-match filter in the form "Column=Value". Repeatable.',
    )

    p_append = subparsers.add_parser("append-row")
    p_append.add_argument("--sheet-url", required=True)
    p_append.add_argument("--tab", required=True)
    p_append.add_argument("--data", required=True, help="JSON object string")

    p_update = subparsers.add_parser("update-row")
    p_update.add_argument("--sheet-url", required=True)
    p_update.add_argument("--tab", required=True)
    p_update.add_argument("--row-number", type=int, required=True)
    p_update.add_argument("--data", required=True, help="JSON object string")

    p_daily = subparsers.add_parser("create-daily-group")
    p_daily.add_argument("--sheet-url", required=True)
    p_daily.add_argument("--date", required=True)

    p_daily_packet = subparsers.add_parser("daily-approval-packet")
    p_daily_packet.add_argument("--sheet-url", required=True)
    p_daily_packet.add_argument("--date", required=True)

    p_daily_state = subparsers.add_parser("daily-approval-state")
    p_daily_state.add_argument("--sheet-url", required=True)
    p_daily_state.add_argument("--date", required=True)

    p_set_daily_approval = subparsers.add_parser("set-daily-group-approval")
    p_set_daily_approval.add_argument("--sheet-url", required=True)
    p_set_daily_approval.add_argument("--date", required=True)
    p_set_daily_approval.add_argument(
        "--clear",
        action="store_true",
        help="Clear approval instead of marking the date group approved.",
    )

    p_process_approval = subparsers.add_parser("process-daily-approval-reply")
    p_process_approval.add_argument("--sheet-url", required=True)
    p_process_approval.add_argument("--date", required=True)
    p_process_approval.add_argument(
        "--message", required=True, help="Inbound WhatsApp message text"
    )
    p_process_approval.add_argument(
        "--sender", help="Phone number or sender id for the inbound message"
    )
    p_process_approval.add_argument(
        "--allowed-sender",
        action="append",
        default=[],
        help="Allowlisted sender id. Repeatable.",
    )
    p_process_approval.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate the reply without updating the sheet.",
    )

    p_weekly = subparsers.add_parser("create-weekly-group")
    p_weekly.add_argument("--sheet-url", required=True)
    p_weekly.add_argument("--week-start", required=True)
    p_weekly.add_argument("--week-number", type=int, required=True)

    # --- Execution update commands ---

    p_complete = subparsers.add_parser("complete-daily-row")
    p_complete.add_argument("--sheet-url", required=True)
    p_complete.add_argument("--row-number", type=int, required=True)
    p_complete.add_argument("--progress", required=True)
    p_complete.add_argument("--notes")

    p_skip = subparsers.add_parser("skip-daily-row")
    p_skip.add_argument("--sheet-url", required=True)
    p_skip.add_argument("--row-number", type=int, required=True)
    p_skip.add_argument("--notes")

    p_partial = subparsers.add_parser("partial-daily-row")
    p_partial.add_argument("--sheet-url", required=True)
    p_partial.add_argument("--row-number", type=int, required=True)
    p_partial.add_argument("--progress", required=True)
    p_partial.add_argument("--notes")

    p_roll = subparsers.add_parser("roll-daily-row")
    p_roll.add_argument("--sheet-url", required=True)
    p_roll.add_argument("--row-number", type=int, required=True)
    p_roll.add_argument("--next-date", required=True)
    p_roll.add_argument("--remaining", required=True)
    p_roll.add_argument("--notes")

    p_promote = subparsers.add_parser("promote-inbox-row")
    p_promote.add_argument("--sheet-url", required=True)
    p_promote.add_argument("--inbox-row-number", type=int, required=True)
    p_promote.add_argument("--target-date", required=True)

    p_recompute = subparsers.add_parser("recompute-weekly-progress")
    p_recompute.add_argument("--sheet-url", required=True)
    p_recompute.add_argument("--week-start", required=True)

    args = parser.parse_args()
    creds = str(Path(args.creds).expanduser())

    try:
        if args.command == "list-tabs":
            result = list_tabs(creds, args.sheet_url)

        elif args.command == "read-tab":
            equals_filters = {}
            for item in args.equals:
                if "=" not in item:
                    raise ValueError(f"Invalid --equals filter: {item}")
                k, v = item.split("=", 1)
                equals_filters[k] = v
            result = read_tab(
                creds,
                args.sheet_url,
                args.tab,
                nonempty_column=args.nonempty_column,
                equals_filters=equals_filters or None,
            )

        elif args.command == "append-row":
            data = json.loads(args.data)
            result = append_row(creds, args.sheet_url, args.tab, data)

        elif args.command == "update-row":
            data = json.loads(args.data)
            result = update_row(creds, args.sheet_url, args.tab, args.row_number, data)

        elif args.command == "create-daily-group":
            result = create_daily_group(creds, args.sheet_url, args.date)

        elif args.command == "daily-approval-packet":
            result = build_daily_approval_packet(creds, args.sheet_url, args.date)

        elif args.command == "daily-approval-state":
            result = daily_approval_state(creds, args.sheet_url, args.date)

        elif args.command == "set-daily-group-approval":
            result = set_daily_group_approval(
                creds,
                args.sheet_url,
                args.date,
                approved=not args.clear,
            )

        elif args.command == "process-daily-approval-reply":
            result = process_daily_approval_reply(
                creds,
                args.sheet_url,
                args.date,
                args.message,
                sender=args.sender,
                allowed_senders=args.allowed_sender or None,
                dry_run=args.dry_run,
            )

        elif args.command == "create-weekly-group":
            result = create_weekly_group(creds, args.sheet_url, args.week_start, args.week_number)

        elif args.command == "complete-daily-row":
            result = complete_daily_row(
                creds, args.sheet_url, args.row_number, args.progress, args.notes
            )

        elif args.command == "skip-daily-row":
            result = skip_daily_row(creds, args.sheet_url, args.row_number, args.notes)

        elif args.command == "partial-daily-row":
            result = partial_daily_row(
                creds, args.sheet_url, args.row_number, args.progress, args.notes
            )

        elif args.command == "roll-daily-row":
            result = roll_daily_row(
                creds, args.sheet_url, args.row_number, args.next_date, args.remaining, args.notes
            )

        elif args.command == "promote-inbox-row":
            result = promote_inbox_row(
                creds, args.sheet_url, args.inbox_row_number, args.target_date
            )

        elif args.command == "recompute-weekly-progress":
            result = recompute_weekly_progress(creds, args.sheet_url, args.week_start)

        else:
            raise ValueError("Unknown command")

        print(json.dumps(result, indent=2, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
