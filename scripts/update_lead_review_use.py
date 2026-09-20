#!/usr/bin/env python3
"""Safely update one Lead Review Use value and refresh its dashboard cache."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cache_lead_review_dashboard import DEFAULT_CACHE_FILE, build_dashboard_cache, write_cache
from lead_exec_research import DEFAULT_CREDS, DEFAULT_REVIEW_TAB, DEFAULT_SHEET_URL
from sheets_helper import get_client, get_worksheet, open_sheet

ALLOWED_USE_VALUES = {
    "": "",
    "potential leads": "Potential leads",
    "case study worthy": "Case study worthy",
}


def parse_use(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if normalized not in ALLOWED_USE_VALUES:
        allowed = ", ".join(repr(item) for item in ALLOWED_USE_VALUES.values() if item)
        raise argparse.ArgumentTypeError(f"use must be blank, {allowed}")
    return ALLOWED_USE_VALUES[normalized]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--use", type=parse_use, required=True)
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

    if args.row < 2:
        raise SystemExit("Lead Review row must be 2 or greater.")

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, args.review_tab)
    headers = worksheet.row_values(1)
    for required in ("Run ID", "Use"):
        if required not in headers:
            raise SystemExit(f"{args.review_tab} is missing required column: {required}")

    run_id_column = headers.index("Run ID") + 1
    use_column = headers.index("Use") + 1
    live_run_id = str(worksheet.cell(args.row, run_id_column).value or "").strip()
    if live_run_id != args.run_id.strip():
        raise SystemExit(
            f"Row identity changed: expected {args.run_id!r}, found {live_run_id!r}. "
            "Refresh the dashboard before trying again."
        )

    worksheet.update_cell(args.row, use_column, args.use)
    verified = str(worksheet.cell(args.row, use_column).value or "").strip()
    if verified != args.use:
        raise SystemExit(f"Use verification failed at row {args.row}: {verified!r}")

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
                "row": args.row,
                "run_id": args.run_id,
                "use": args.use,
                "cache_file": args.cache_file,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
