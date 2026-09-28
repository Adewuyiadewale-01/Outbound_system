#!/usr/bin/env python3
"""Create isolated Final - Test and Prospects - Test tabs from live headers."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helpers"))
sys.path.insert(0, str(ROOT / "scripts"))

from lead_exec_research import DEFAULT_OBF_SHEET_URL, DEFAULT_SHEET_URL  # noqa: E402
from sheets_helper import get_client, open_sheet  # noqa: E402

CREDS = ROOT / "credentials" / "google-sheets.json"
if not CREDS.exists():
    CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"


def ensure_tab(sheet_url: str, source: str, target: str) -> str:
    spreadsheet = open_sheet(get_client(str(CREDS)), sheet_url)
    try:
        spreadsheet.worksheet(target)
        return "already_present"
    except Exception:
        headers = spreadsheet.worksheet(source).row_values(1)
        worksheet = spreadsheet.add_worksheet(title=target, rows=1000, cols=max(26, len(headers)))
        worksheet.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
        return "created"


if __name__ == "__main__":
    print({"Final - Test": ensure_tab(DEFAULT_SHEET_URL, "Final", "Final - Test")})
    print({"Prospects - Test": ensure_tab(DEFAULT_OBF_SHEET_URL, "Prospects", "Prospects - Test")})
