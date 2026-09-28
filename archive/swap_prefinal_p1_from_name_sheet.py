#!/usr/bin/env python3
"""Swap/move Pre-final rows when P1 appears in an external name list."""

import argparse
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import get_client, get_worksheet, normalize_rows, open_sheet  # noqa: E402

load_repo_env()


DEFAULT_LEADS_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
DEFAULT_NAME_SHEET_URL = os.environ.get("COMPARISON_SHEET_URL", "")
DEFAULT_PREFINAL_TAB = "Pre-final"
DEFAULT_COPY_TAB = "Copy of Pre-final"
REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS

P1_COLUMNS = ["P1 Name", "P1 Title", "P1 LinkedIn", "P1 Email"]
P2_COLUMNS = ["P2 Name", "P2 Title", "P2 LinkedIn", "P2 Email"]
NAME_COLUMN_CANDIDATES = [
    "Name",
    "Full Name",
    "Person Name",
    "P1 Name",
    "Lead Name",
    "First Name Last Name",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return clean_text(text)


def parse_gid(sheet_url: str) -> str:
    match = re.search(r"[#&?]gid=(\d+)", sheet_url)
    return match.group(1) if match else ""


def get_worksheet_by_gid_or_first(spreadsheet, sheet_url: str):
    gid = parse_gid(sheet_url)
    if gid:
        for worksheet in spreadsheet.worksheets():
            if str(worksheet.id) == gid:
                return worksheet
    return spreadsheet.get_worksheet(0)


def require_columns(headers: Sequence[str], columns: Sequence[str], tab_name: str) -> None:
    missing = [column for column in columns if column not in headers]
    if missing:
        raise ValueError(f"{tab_name} is missing required columns: {', '.join(missing)}")


def first_existing_column(headers: Sequence[str], candidates: Sequence[str]) -> str:
    for candidate in candidates:
        if candidate in headers:
            return candidate
    lowered = {clean_text(header).lower(): header for header in headers}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found:
            return found
    raise ValueError(
        f"Could not find a name column in the comparison sheet. Tried: {', '.join(candidates)}"
    )


def read_name_set(client, sheet_url: str, name_column: str = "") -> tuple[set, str, str, int]:
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet_by_gid_or_first(spreadsheet, sheet_url)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Comparison sheet is empty.")
    headers = values[0]
    column = name_column or first_existing_column(headers, NAME_COLUMN_CANDIDATES)
    rows = normalize_rows(values)
    names = {normalize_name(row.get(column)) for row in rows if normalize_name(row.get(column))}
    return names, worksheet.title, column, len(names)


def meaningful_prefinal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if any(
            clean_text(row.get(column))
            for column in ("ID", "Company", "P1 Name", "P1 LinkedIn", "P2 Name", "P2 LinkedIn")
        )
    ]


def swap_p1_p2(row: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    p1_values = {column: updated.get(column, "") for column in P1_COLUMNS}
    p2_values = {column: updated.get(column, "") for column in P2_COLUMNS}
    for p1_column, p2_column in zip(P1_COLUMNS, P2_COLUMNS):
        updated[p1_column] = p2_values.get(p2_column, "")
        updated[p2_column] = p1_values.get(p1_column, "")
    return updated


def row_has_p2(row: dict[str, Any]) -> bool:
    return any(clean_text(row.get(column)) for column in P2_COLUMNS)


def next_append_row(rows: list[dict[str, Any]]) -> int:
    last = 1
    for row in rows:
        if any(clean_text(row.get(column)) for column in row.keys() if not column.startswith("_")):
            last = max(last, int(row.get("_row_number", 1)))
    return last + 1


def rows_to_payload(headers: Sequence[str], rows: Sequence[dict[str, Any]]) -> list[list[Any]]:
    return [[row.get(header, "") for header in headers] for row in rows]


def clear_body_values(worksheet, headers: Sequence[str], start_row: int = 2) -> None:
    if worksheet.row_count < start_row:
        return
    end_cell = gspread.utils.rowcol_to_a1(worksheet.row_count, len(headers))
    worksheet.batch_clear([f"A{start_row}:{end_cell}"])


def update_prefinal_values(
    worksheet, headers: Sequence[str], rows: list[dict[str, Any]], dry_run: bool
) -> None:
    if dry_run:
        return
    clear_body_values(worksheet, headers, start_row=2)
    if not rows:
        return
    payload = rows_to_payload(headers, rows)
    end_cell = gspread.utils.rowcol_to_a1(1 + len(payload), len(headers))
    worksheet.update(range_name=f"A2:{end_cell}", values=payload, value_input_option="USER_ENTERED")


def append_copy_rows(
    worksheet, headers: Sequence[str], rows: list[dict[str, Any]], dry_run: bool
) -> int:
    if not rows:
        return 0
    existing_values = worksheet.get_all_values()
    if not existing_values:
        raise ValueError(f"{worksheet.title} is empty and must already contain headers.")
    copy_headers = existing_values[0]
    require_columns(copy_headers, headers, worksheet.title)
    start_row = next_append_row(normalize_rows(existing_values))
    if dry_run:
        return start_row
    payload = rows_to_payload(copy_headers, rows)
    end_cell = gspread.utils.rowcol_to_a1(start_row + len(payload) - 1, len(copy_headers))
    worksheet.update(
        range_name=f"A{start_row}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return start_row


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = get_client(str(Path(args.credentials)))
    blocked_names, blocked_tab, blocked_column, blocked_count = read_name_set(
        client,
        args.name_sheet_url,
        name_column=args.name_column,
    )

    leads_spreadsheet = open_sheet(client, args.leads_sheet_url)
    prefinal_ws = get_worksheet(leads_spreadsheet, args.prefinal_tab)
    copy_ws = get_worksheet(leads_spreadsheet, args.copy_tab)

    prefinal_values = prefinal_ws.get_all_values()
    if not prefinal_values:
        raise ValueError(f"{args.prefinal_tab} is empty.")
    headers = prefinal_values[0]
    require_columns(headers, ["ID", "Company", *P1_COLUMNS, *P2_COLUMNS], args.prefinal_tab)
    prefinal_rows = meaningful_prefinal_rows(normalize_rows(prefinal_values))

    kept_rows: list[dict[str, Any]] = []
    moved_rows: list[dict[str, Any]] = []
    swapped_rows: list[dict[str, Any]] = []
    untouched_rows: list[dict[str, Any]] = []

    for row in prefinal_rows:
        p1_key = normalize_name(row.get("P1 Name"))
        if p1_key and p1_key in blocked_names:
            if row_has_p2(row):
                swapped = swap_p1_p2(row)
                swapped_rows.append(swapped)
                kept_rows.append(swapped)
            else:
                moved = dict(row)
                moved["Notes"] = clean_text(
                    f"{moved.get('Notes', '')} moved_from_prefinal_p1_match={datetime.now().isoformat(timespec='seconds')}"
                )
                moved_rows.append(moved)
        else:
            untouched_rows.append(row)
            kept_rows.append(row)

    copy_start_row = append_copy_rows(copy_ws, headers, moved_rows, dry_run=args.dry_run)
    update_prefinal_values(prefinal_ws, headers, kept_rows, dry_run=args.dry_run)

    return {
        "dry_run": bool(args.dry_run),
        "comparison_tab": blocked_tab,
        "comparison_name_column": blocked_column,
        "comparison_names_loaded": blocked_count,
        "prefinal_rows_seen": len(prefinal_rows),
        "rows_swapped": len(swapped_rows),
        "rows_moved_to_copy": len(moved_rows),
        "copy_start_row": copy_start_row if moved_rows else None,
        "prefinal_rows_after": len(kept_rows),
        "swapped": [
            {
                "id": row.get("ID", ""),
                "company": row.get("Company", ""),
                "new_p1": row.get("P1 Name", ""),
                "new_p2": row.get("P2 Name", ""),
            }
            for row in swapped_rows
        ],
        "moved": [
            {
                "id": row.get("ID", ""),
                "company": row.get("Company", ""),
                "p1_name": row.get("P1 Name", ""),
            }
            for row in moved_rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Swap Pre-final P1/P2 when P1 appears in an external name sheet."
    )
    parser.add_argument("--leads-sheet-url", default=DEFAULT_LEADS_SHEET_URL)
    parser.add_argument("--name-sheet-url", default=DEFAULT_NAME_SHEET_URL)
    parser.add_argument("--prefinal-tab", default=DEFAULT_PREFINAL_TAB)
    parser.add_argument("--copy-tab", default=DEFAULT_COPY_TAB)
    parser.add_argument(
        "--name-column", default="", help="Override the comparison sheet name column."
    )
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    result = run(args)
    print("Pre-final P1 swap summary")
    print(f"Dry run: {result['dry_run']}")
    print(f"Comparison tab: {result['comparison_tab']}")
    print(f"Name column: {result['comparison_name_column']}")
    print(f"Names loaded: {result['comparison_names_loaded']}")
    print(f"Pre-final rows seen: {result['prefinal_rows_seen']}")
    print(f"Rows swapped P1/P2: {result['rows_swapped']}")
    print(f"Rows moved to {DEFAULT_COPY_TAB}: {result['rows_moved_to_copy']}")
    print(f"Pre-final rows after: {result['prefinal_rows_after']}")
    if result["swapped"]:
        print("\nSwapped:")
        for item in result["swapped"]:
            print(f"- {item['id']} {item['company']}: P1={item['new_p1']} / P2={item['new_p2']}")
    if result["moved"]:
        print("\nMoved:")
        for item in result["moved"]:
            print(f"- {item['id']} {item['company']}: P1={item['p1_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
