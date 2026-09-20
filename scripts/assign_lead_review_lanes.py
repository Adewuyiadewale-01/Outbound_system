#!/usr/bin/env python3
"""Assign balanced Automation/Design lanes to one Lead Review date group."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import gspread
from cache_lead_review_dashboard import DEFAULT_CACHE_FILE, build_dashboard_cache, write_cache
from lead_exec_research import (
    DEFAULT_CREDS,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    assign_primary_lanes,
    parse_review_group_date,
)
from sheets_helper import get_client, get_worksheet, open_sheet


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-row", type=int, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--run-file", required=True)
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

    expected_date = parse_review_group_date(args.date)
    run_file = Path(args.run_file)
    if args.group_row < 2 or not expected_date or not run_file.exists():
        raise SystemExit("valid group row, date, and source run file are required")

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, args.review_tab)
    values = worksheet.get_all_values()
    headers = values[0] if values else []
    for required in ("Date", "Primary Lane", "Run ID"):
        if required not in headers:
            raise SystemExit(f"{args.review_tab} is missing required column: {required}")

    date_index = headers.index("Date")
    lane_index = headers.index("Primary Lane")
    run_id_index = headers.index("Run ID")
    group = values[args.group_row - 1]
    live_date = parse_review_group_date(group[date_index] if date_index < len(group) else "")
    if live_date != expected_date:
        raise SystemExit("date-group identity changed; refresh before assigning lanes")

    group_rows = []
    for row_number in range(args.group_row + 1, len(values) + 1):
        row = values[row_number - 1]
        padded = row + [""] * (len(headers) - len(row))
        if parse_review_group_date(padded[date_index]):
            break
        run_id = str(padded[run_id_index] or "").strip()
        if run_id:
            group_rows.append({"row": row_number, "run_id": run_id})
    if not group_rows:
        raise SystemExit("the selected date group contains no lead rows")

    lane_counts = assign_primary_lanes(group_rows)
    start_row = group_rows[0]["row"]
    end_row = group_rows[-1]["row"]
    lane_column = gspread.utils.rowcol_to_a1(1, lane_index + 1).rstrip("1")
    worksheet.update(
        range_name=f"{lane_column}{start_row}:{lane_column}{end_row}",
        values=[[item["primary_lane"]] for item in group_rows],
        value_input_option="USER_ENTERED",
    )

    verified = worksheet.get_all_values()
    for item in group_rows:
        live = verified[item["row"] - 1]
        value = live[lane_index] if lane_index < len(live) else ""
        if value != item["primary_lane"]:
            raise SystemExit(f"lane verification failed at row {item['row']}")

    run = json.loads(run_file.read_text(encoding="utf-8"))
    lane_by_id = {item["run_id"]: item["primary_lane"] for item in group_rows}
    for lead in run.get("leads", []):
        lead_id = str(lead.get("id") or "")
        if lead_id in lane_by_id:
            lead["primary_lane"] = lane_by_id[lead_id]
    run.setdefault("source", {})["lane_counts"] = lane_counts
    write_json_atomic(run_file, run)

    cache = build_dashboard_cache(verified, sheet_url=args.sheet_url, review_tab=args.review_tab)
    write_cache(Path(args.cache_file), cache)
    print(
        json.dumps(
            {
                "ok": True,
                "group_row": args.group_row,
                "date": expected_date.isoformat(),
                "start_row": start_row,
                "end_row": end_row,
                "lane_counts": lane_counts,
                "run_file": str(run_file),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
