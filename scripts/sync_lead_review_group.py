#!/usr/bin/env python3
"""Atomically sync a reviewed Lead Review group and mark its date row complete."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from cache_lead_review_dashboard import DEFAULT_CACHE_FILE, build_dashboard_cache, write_cache
from lead_exec_research import (
    DEFAULT_CREDS,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    parse_review_group_date,
)
from sheets_helper import get_client, get_worksheet, open_sheet
from update_lead_review_approval import parse_approved
from update_lead_review_use import parse_use


def normalized_live_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "checked"}


def parse_payload(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise argparse.ArgumentTypeError("payload must be an object with a rows array")
    return payload


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    seen_rows: set[int] = set()
    for item in rows:
        row = int(item.get("row") or 0)
        run_id = str(item.get("run_id") or "").strip()
        if row < 2 or not run_id or row in seen_rows:
            raise ValueError("each review row needs a unique Sheet row and Run ID")
        seen_rows.add(row)
        normalized.append(
            {
                "row": row,
                "run_id": run_id,
                "approved": parse_approved(str(item.get("approved", False))),
                "use": parse_use(str(item.get("use") or "")),
                "expected_approved": parse_approved(str(item.get("expected_approved", False))),
                "expected_use": parse_use(str(item.get("expected_use") or "")),
            }
        )
    return normalized


def build_value_updates(
    rows: list[dict[str, Any]], indexes: dict[str, int], group_row: int
) -> list[dict[str, Any]]:
    approved_col = chr(65 + indexes["Approved"])
    use_col = chr(65 + indexes["Use"])
    completion_col = chr(65 + indexes["Design Review Complete"])
    updates = [
        {
            "range": f"{approved_col}{item['row']}:{use_col}{item['row']}",
            "values": [[item["approved"], item["use"]]],
        }
        for item in rows
    ]
    updates.append({"range": f"{completion_col}{group_row}", "values": [[True]]})
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-json", type=parse_payload, required=True)
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

    payload = args.payload_json
    group_row = int(payload.get("group_row") or 0)
    expected_date = parse_review_group_date(str(payload.get("date") or ""))
    rows = normalize_rows(payload["rows"])
    if group_row < 2 or not expected_date or not rows:
        raise SystemExit("a valid date-group row, date, and at least one lead row are required")

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, args.review_tab)
    values = worksheet.get_all_values()
    if not values:
        raise SystemExit(f"{args.review_tab} is empty")
    headers = values[0]
    required = ("Date", "Run ID", "Approved", "Use", "Design Review Complete")
    missing = [header for header in required if header not in headers]
    if missing:
        raise SystemExit(f"{args.review_tab} is missing required columns: {', '.join(missing)}")

    indexes = {header: headers.index(header) for header in required}
    if group_row > len(values):
        raise SystemExit("the cached date-group row no longer exists")
    live_date = parse_review_group_date(values[group_row - 1][indexes["Date"]])
    if live_date != expected_date:
        raise SystemExit("date-group identity changed; refresh the dashboard before syncing")

    for item in rows:
        if item["row"] > len(values):
            raise SystemExit(f"Lead Review row {item['row']} no longer exists")
        live = values[item["row"] - 1]
        live_run_id = str(live[indexes["Run ID"]] or "").strip()
        live_approved = normalized_live_bool(live[indexes["Approved"]])
        live_use = parse_use(str(live[indexes["Use"]] or ""))
        if live_run_id != item["run_id"]:
            raise SystemExit(
                f"row {item['row']} identity changed; refresh the dashboard before syncing"
            )
        if live_approved != item["expected_approved"] or live_use != item["expected_use"]:
            raise SystemExit(
                f"row {item['row']} changed in the Sheet; refresh before overwriting it"
            )
    updates = build_value_updates(rows, indexes, group_row)
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")

    verified_values = worksheet.get_all_values()
    for item in rows:
        live = verified_values[item["row"] - 1]
        if (
            normalized_live_bool(live[indexes["Approved"]]) != item["approved"]
            or parse_use(str(live[indexes["Use"]] or "")) != item["use"]
        ):
            raise SystemExit(f"sync verification failed at Lead Review row {item['row']}")
    if not normalized_live_bool(verified_values[group_row - 1][indexes["Design Review Complete"]]):
        raise SystemExit("Design Review Complete verification failed")

    cache = build_dashboard_cache(
        verified_values, sheet_url=args.sheet_url, review_tab=args.review_tab
    )
    write_cache(Path(args.cache_file), cache)
    print(
        json.dumps(
            {
                "ok": True,
                "group_row": group_row,
                "date": expected_date.isoformat(),
                "row_count": len(rows),
                "review_complete": True,
                "cache_file": args.cache_file,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
