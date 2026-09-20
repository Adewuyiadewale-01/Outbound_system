#!/usr/bin/env python3
"""Backfill Outreach Log Sent At / Days Left for pending connection requests.

This recreates the mobile app's withdrawal countdown source:
- Only Outreach Log rows with Current Progress = Conn Request / Connection Request.
- Connected and withdrawn rows are left alone.
- Missing Sent At can be backfilled from Prospects.Date Queued by Prospect ID.
- Missing Days Left is calculated as the numeric value 14 - days since Sent At.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from outreach_helper import CREDS_PATH, OBF_SHEET_URL, OUTREACH_LOG_TAB, PROSPECTS_TAB  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

SPREADSHEET_SERIAL_EPOCH = datetime(1899, 12, 30)
DEFAULT_TIMEZONE = "Africa/Lagos"
WITHDRAWAL_WINDOW_DAYS = 14


def normalize_header(value: Any) -> str:
    return str(value or "").strip()


def normalize_progress(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"conn request", "connection request"}:
        return "conn_request"
    if text == "connected":
        return "connected"
    if text in {"withdrawn", "request withdrawn"}:
        return "withdrawn"
    return text.replace(" ", "_") if text else ""


def parse_sheet_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None and numeric >= 20000:
        try:
            return (SPREADSHEET_SERIAL_EPOCH + timedelta(days=numeric)).date()
        except OverflowError:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def calculate_days_left(sent_at: date, today: date) -> int:
    elapsed = (today - sent_at).days
    return WITHDRAWAL_WINDOW_DAYS - elapsed


def header_index(headers: Iterable[Any]) -> dict[str, int]:
    return {normalize_header(header): i + 1 for i, header in enumerate(headers)}


def require_columns(index: dict[str, int], columns: Iterable[str], tab_name: str) -> None:
    missing = [column for column in columns if column not in index]
    if missing:
        raise ValueError(f"{tab_name} is missing required columns: {', '.join(missing)}")


def row_value(row: list[Any], index: dict[str, int], column: str) -> str:
    position = index.get(column)
    if not position:
        return ""
    if position - 1 >= len(row):
        return ""
    return str(row[position - 1] or "").strip()


def read_rows(worksheet: Any) -> tuple[list[str], list[list[Any]]]:
    values = worksheet.get_all_values()
    if not values:
        return [], []
    headers = [normalize_header(header) for header in values[0]]
    width = len(headers)
    rows = []
    for row in values[1:]:
        rows.append(row + [""] * max(0, width - len(row)))
    return headers, rows


def build_prospect_date_queued(
    prospects_rows: list[list[Any]], prospects_index: dict[str, int]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in prospects_rows:
        prospect_id = row_value(row, prospects_index, "ID")
        date_queued = row_value(row, prospects_index, "Date Queued")
        if prospect_id and date_queued and prospect_id not in result:
            result[prospect_id] = date_queued
    return result


def a1(row_number: int, col_number: int) -> str:
    letters = ""
    n = col_number
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_number}"


def batch_update_cells(worksheet: Any, updates: list[tuple[int, int, Any]]) -> None:
    if not updates:
        return
    payload = [
        {"range": a1(row_number, col_number), "values": [[value]]}
        for row_number, col_number, value in updates
    ]
    worksheet.batch_update(payload, value_input_option="USER_ENTERED")


def append_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def build_backfill(
    outreach_rows: list[list[Any]],
    outreach_index: dict[str, int],
    prospect_date_queued: dict[str, str],
    today: date,
    allow_last_action_fallback: bool,
) -> tuple[list[tuple[int, int, Any]], list[dict[str, Any]], dict[str, Any]]:
    updates: list[tuple[int, int, Any]] = []
    events: list[dict[str, Any]] = []
    summary = {
        "scanned": len(outreach_rows),
        "conn_request_rows": 0,
        "skipped_non_conn_request": 0,
        "sent_at_backfilled_from_prospects": 0,
        "sent_at_backfilled_from_last_action": 0,
        "days_left_backfilled": 0,
        "days_left_recalculated": 0,
        "already_had_sent_at": 0,
        "already_had_days_left": 0,
        "blocked_missing_sent_at": 0,
        "invalid_sent_at": 0,
        "due_or_overdue": 0,
        "upcoming": 0,
    }

    sent_at_col = outreach_index["Sent At"]
    days_left_col = outreach_index["Days Left"]

    for offset, row in enumerate(outreach_rows, start=2):
        progress = row_value(row, outreach_index, "Current Progress")
        progress_key = normalize_progress(progress)
        prospect_id = row_value(row, outreach_index, "Prospect ID")
        company = row_value(row, outreach_index, "Company")
        contact_name = row_value(row, outreach_index, "Contact Name")

        if progress_key != "conn_request":
            summary["skipped_non_conn_request"] += 1
            continue

        summary["conn_request_rows"] += 1
        sent_at_raw = row_value(row, outreach_index, "Sent At")
        days_left_raw = row_value(row, outreach_index, "Days Left")
        source = "existing_sent_at" if sent_at_raw else ""

        if sent_at_raw:
            summary["already_had_sent_at"] += 1
        else:
            prospects_date = prospect_date_queued.get(prospect_id, "")
            if prospects_date:
                sent_at_raw = prospects_date
                source = "prospects_date_queued"
                updates.append(
                    (offset, sent_at_col, format_date(parse_sheet_date(prospects_date) or today))
                )
                summary["sent_at_backfilled_from_prospects"] += 1
            elif allow_last_action_fallback:
                last_action = row_value(row, outreach_index, "Last Action Date")
                if last_action:
                    sent_at_raw = last_action
                    source = "last_action_date"
                    updates.append(
                        (offset, sent_at_col, format_date(parse_sheet_date(last_action) or today))
                    )
                    summary["sent_at_backfilled_from_last_action"] += 1

        sent_at = parse_sheet_date(sent_at_raw)
        if not sent_at:
            summary["blocked_missing_sent_at" if not sent_at_raw else "invalid_sent_at"] += 1
            events.append(
                {
                    "event": "blocked_missing_sent_at" if not sent_at_raw else "invalid_sent_at",
                    "row_number": offset,
                    "prospect_id": prospect_id,
                    "company": company,
                    "contact_name": contact_name,
                    "sent_at_raw": sent_at_raw,
                }
            )
            continue

        days_left = calculate_days_left(sent_at, today)
        if days_left <= 0:
            summary["due_or_overdue"] += 1
        else:
            summary["upcoming"] += 1

        try:
            existing_days_left = (
                int(str(days_left_raw).strip()) if str(days_left_raw).strip() else None
            )
        except ValueError:
            existing_days_left = None
        if days_left_raw:
            summary["already_had_days_left"] += 1
            if existing_days_left != days_left:
                updates.append((offset, days_left_col, days_left))
                summary["days_left_recalculated"] += 1
        else:
            updates.append((offset, days_left_col, days_left))
            summary["days_left_backfilled"] += 1

        events.append(
            {
                "event": "countdown_backfill_evaluated",
                "row_number": offset,
                "prospect_id": prospect_id,
                "company": company,
                "contact_name": contact_name,
                "sent_at": format_date(sent_at),
                "sent_at_source": source,
                "days_left": days_left,
                "days_left_was_blank": not bool(days_left_raw),
            }
        )

    summary["cell_updates"] = len(updates)
    return updates, events, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill withdrawal countdown fields in OBF Outreach Log."
    )
    parser.add_argument("--credentials", default=CREDS_PATH)
    parser.add_argument("--sheet-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--dry-run", action="store_true", help="Calculate and journal without writing sheet cells."
    )
    parser.add_argument(
        "--allow-last-action-fallback",
        action="store_true",
        help="Use Outreach Log Last Action Date when Sent At and Prospects Date Queued are both blank.",
    )
    parser.add_argument("--journal", default="", help="Override JSONL journal path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = datetime.now(ZoneInfo(args.timezone)).date()
    journal = (
        Path(args.journal)
        if args.journal
        else ROOT / "state" / "withdrawal_journal" / f"{today.isoformat()}-backfill.jsonl"
    )

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    outreach_ws = get_worksheet(spreadsheet, OUTREACH_LOG_TAB)
    prospects_ws = get_worksheet(spreadsheet, PROSPECTS_TAB)

    outreach_headers, outreach_rows = read_rows(outreach_ws)
    prospects_headers, prospects_rows = read_rows(prospects_ws)
    outreach_index = header_index(outreach_headers)
    prospects_index = header_index(prospects_headers)
    require_columns(
        outreach_index,
        [
            "Prospect ID",
            "Company",
            "Contact Name",
            "Current Progress",
            "Last Action Date",
            "Sent At",
            "Days Left",
        ],
        OUTREACH_LOG_TAB,
    )
    require_columns(prospects_index, ["ID", "Date Queued"], PROSPECTS_TAB)

    prospect_date_queued = build_prospect_date_queued(prospects_rows, prospects_index)
    updates, events, summary = build_backfill(
        outreach_rows,
        outreach_index,
        prospect_date_queued,
        today,
        args.allow_last_action_fallback,
    )

    run_event = {
        "event": "withdrawal_countdown_backfill_summary",
        "date": today.isoformat(),
        "dry_run": bool(args.dry_run),
        "allow_last_action_fallback": bool(args.allow_last_action_fallback),
        "summary": summary,
    }
    append_jsonl(journal, [run_event, *events])

    if not args.dry_run:
        batch_update_cells(outreach_ws, updates)

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": bool(args.dry_run),
                "journal": str(journal),
                "summary": summary,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
