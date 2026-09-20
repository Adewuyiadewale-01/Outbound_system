#!/usr/bin/env python3
"""Add the Primary Lane header to the activity pipeline sheets if missing."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from lead_exec_research import (  # noqa: E402
    DEFAULT_OBF_SHEET_URL,
    DEFAULT_PROSPECTS_TAB,
    DEFAULT_SHEET_URL,
)
from sheets_helper import get_client, open_sheet  # noqa: E402

REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS


def ensure_header(credentials: Path, sheet_url: str, tab: str, dry_run: bool) -> dict[str, object]:
    worksheet = open_sheet(get_client(str(credentials)), sheet_url).worksheet(tab)
    headers = worksheet.row_values(1)
    if "Primary Lane" in headers:
        return {
            "sheet": sheet_url,
            "tab": tab,
            "action": "already_present",
            "column": headers.index("Primary Lane") + 1,
        }
    column = len(headers) + 1
    if not dry_run:
        if worksheet.col_count < column:
            worksheet.add_cols(column - worksheet.col_count)
        worksheet.update(
            range_name=gspread.utils.rowcol_to_a1(1, column),
            values=[["Primary Lane"]],
            value_input_option="USER_ENTERED",
        )
    return {
        "sheet": sheet_url,
        "tab": tab,
        "action": "would_add" if dry_run else "added",
        "column": column,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    credentials = Path(args.credentials)
    if not credentials.exists():
        raise SystemExit(f"Credentials file not found: {credentials}")
    targets = [
        (DEFAULT_SHEET_URL, "Pre-final"),
        (DEFAULT_SHEET_URL, "Final"),
        (DEFAULT_OBF_SHEET_URL, DEFAULT_PROSPECTS_TAB),
    ]
    for sheet_url, tab in targets:
        print(ensure_header(credentials, sheet_url, tab, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
