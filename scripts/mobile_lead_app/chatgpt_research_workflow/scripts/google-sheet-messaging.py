#!/usr/bin/env python3

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parents[4]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from runtime_environment import load_repo_env

load_repo_env()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

DEFAULT_CREDS = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS", "~/.openclaw/credentials/google-sheets.json"
)
DEFAULT_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")
DEFAULT_TAB = "Messaging"

REQUIRED_COLUMNS = [
    "Prospect ID",
    "Company",
    "Company Website",
    "Contact Name",
    "Contact Linkedin",
    "Approval",
    "ChatGPT Conversation URL",
    "Research summary",
    "Message draft",
]

OPTIONAL_RESULT_COLUMNS = [
    "Google Doc ID",
    "Google Doc URL",
    "Google Doc Tab ID",
    "Google Doc Tab Title",
]


def get_client(credentials_path: str) -> gspread.Client:
    creds = Credentials.from_service_account_file(
        str(Path(credentials_path).expanduser()), scopes=SCOPES
    )
    return gspread.authorize(creds)


def get_worksheet(credentials_path: str, sheet_url: str, tab: str):
    client = get_client(credentials_path)
    spreadsheet = client.open_by_url(sheet_url)
    return spreadsheet.worksheet(tab)


def normalize_rows(values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    headers = [str(value).strip() for value in values[0]]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ValueError(f"Missing required columns in Messaging tab: {', '.join(missing)}")

    rows = []
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        item = {
            headers[index]: padded[index] if index < len(padded) else ""
            for index in range(len(headers))
        }
        item["_row_number"] = row_number
        rows.append(item)
    return rows


def is_checked(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "y", "1", "checked", "x", "approved"}


def fetch_approved(args) -> None:
    worksheet = get_worksheet(args.credentials, args.sheet_url, args.tab)
    rows = normalize_rows(worksheet.get_all_values())
    approved = []

    for row in rows:
        if not is_checked(row.get("Approval")):
            continue
        if not args.include_completed and (
            str(row.get("ChatGPT Conversation URL", "")).strip()
            or str(row.get("Research summary", "")).strip()
        ):
            continue
        approved.append(row)

    print(json.dumps({"ok": True, "count": len(approved), "rows": approved}, ensure_ascii=False))


def update_result(args) -> None:
    worksheet = get_worksheet(args.credentials, args.sheet_url, args.tab)
    headers = [str(value).strip() for value in worksheet.row_values(1)]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ValueError(f"Missing required columns in Messaging tab: {', '.join(missing)}")
    headers = ensure_optional_columns(worksheet, headers, OPTIONAL_RESULT_COLUMNS)

    chat_url_col = headers.index("ChatGPT Conversation URL") + 1
    research_col = headers.index("Research summary") + 1

    updates = [
        {
            "range": gspread.utils.rowcol_to_a1(args.row_number, chat_url_col),
            "values": [[args.chatgpt_url]],
        },
        {
            "range": gspread.utils.rowcol_to_a1(args.row_number, research_col),
            "values": [[args.research_summary]],
        },
    ]
    optional_values = {
        "Google Doc ID": args.google_doc_id,
        "Google Doc URL": args.google_doc_url,
        "Google Doc Tab ID": args.google_doc_tab_id,
        "Google Doc Tab Title": args.google_doc_tab_title,
    }
    for header, value in optional_values.items():
        if value:
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(args.row_number, headers.index(header) + 1),
                    "values": [[value]],
                }
            )
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    print(json.dumps({"ok": True, "row_number": args.row_number}, ensure_ascii=False))


def ensure_optional_columns(worksheet, headers: list[str], columns: list[str]) -> list[str]:
    next_col = len(headers) + 1
    for column in columns:
        if column in headers:
            continue
        worksheet.update_cell(1, next_col, column)
        headers.append(column)
        next_col += 1
    return headers


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read/write Messaging research rows in the OBF Google Sheet."
    )
    parser.add_argument("--credentials", default=DEFAULT_CREDS)
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    parser.add_argument("--tab", default=DEFAULT_TAB)

    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch-approved")
    fetch_parser.add_argument("--include-completed", action="store_true")

    update_parser = subparsers.add_parser("update-result")
    update_parser.add_argument("--row-number", type=int, required=True)
    update_parser.add_argument("--chatgpt-url", required=True)
    update_parser.add_argument("--research-summary", default="")
    update_parser.add_argument("--research-summary-file")
    update_parser.add_argument("--google-doc-id", default="")
    update_parser.add_argument("--google-doc-url", default="")
    update_parser.add_argument("--google-doc-tab-id", default="")
    update_parser.add_argument("--google-doc-tab-title", default="")

    args = parser.parse_args()
    if args.command == "fetch-approved":
        fetch_approved(args)
    elif args.command == "update-result":
        if args.research_summary_file:
            args.research_summary = Path(args.research_summary_file).read_text()
        update_result(args)


if __name__ == "__main__":
    main()
