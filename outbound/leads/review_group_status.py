#!/usr/bin/env python3
"""Safely update the group-level Lead Review completion checkbox."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from outbound.leads.config import (
    DEFAULT_CREDS,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
)
from outbound.leads.dashboard_cache import DEFAULT_CACHE_FILE, build_dashboard_cache, write_cache
from outbound.leads.review_approval import parse_approved
from outbound.leads.reviewtab import parse_review_group_date
from outbound.shared.sheets import get_client, get_worksheet, open_sheet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-row", type=int, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--complete", type=parse_approved, required=True)
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS)),
    )
    parser.add_argument(
        "--sheet-url",
        default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_SHEET_URL),
    )
    parser.add_argument("--review-tab", default=DEFAULT_REVIEW_TAB)
    parser.add_argument("--cache-file", default=str(DEFAULT_CACHE_FILE))
    args = parser.parse_args()

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, args.review_tab)
    headers = worksheet.row_values(1)
    for required in ("Date", "Design Review Complete"):
        if required not in headers:
            raise SystemExit(f"{args.review_tab} is missing required column: {required}")

    date_column = headers.index("Date") + 1
    completion_column = headers.index("Design Review Complete") + 1
    live_date = parse_review_group_date(worksheet.cell(args.group_row, date_column).value)
    expected_date = parse_review_group_date(args.date)
    if not live_date or live_date != expected_date:
        raise SystemExit(
            f"Date-group identity changed at row {args.group_row}. "
            "Refresh the dashboard before trying again."
        )

    worksheet.update_cell(args.group_row, completion_column, args.complete)
    verified = str(worksheet.cell(args.group_row, completion_column).value or "").strip().lower()
    if verified not in ({"true", "1"} if args.complete else {"false", "0", ""}):
        raise SystemExit(f"Review completion verification failed: {verified!r}")

    payload = build_dashboard_cache(
        worksheet.get_all_values(),
        sheet_url=args.sheet_url,
        review_tab=args.review_tab,
    )
    write_cache(Path(args.cache_file), payload)
    print(
        json.dumps(
            {
                "ok": True,
                "group_row": args.group_row,
                "date": live_date.isoformat(),
                "review_complete": args.complete,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
