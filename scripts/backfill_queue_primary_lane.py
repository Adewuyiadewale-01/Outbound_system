#!/usr/bin/env python3
"""Backfill Primary Lane for one queued cohort and matching source rows."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for path in (HELPERS, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from prefinal_queue import load_batch, save_batch  # noqa: E402
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import get_client, normalize_rows, open_sheet  # noqa: E402

load_repo_env()

LEADS_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS


def matching_rows(credentials: Path, sheet_url: str, tab: str, ids: set) -> dict[str, Any]:
    worksheet = open_sheet(get_client(str(credentials)), sheet_url).worksheet(tab)
    values = worksheet.get_all_values()
    headers = values[0] if values else []
    id_header = "ID" if "ID" in headers else "Run ID" if "Run ID" in headers else ""
    if not id_header or "Primary Lane" not in headers:
        raise ValueError(f"{tab} must contain an ID/Run ID column and Primary Lane.")
    rows = normalize_rows(values)
    matches = {
        str(row.get(id_header) or "").strip(): int(row["_row_number"])
        for row in rows
        if str(row.get(id_header) or "").strip() in ids
    }
    return {
        "worksheet": worksheet,
        "lane_column": headers.index("Primary Lane") + 1,
        "matches": matches,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-fingerprint", required=True)
    lane_group = parser.add_mutually_exclusive_group(required=True)
    lane_group.add_argument("--lane", choices=("Automation", "Design"))
    lane_group.add_argument(
        "--alternate-lanes",
        action="store_true",
        help="Alternate Automation/Design by queue order, starting with Automation.",
    )
    parser.add_argument("--credentials", default=str(DEFAULT_CREDS))
    parser.add_argument("--sheet-url", default=LEADS_SHEET_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    batch = load_batch(args.queue_fingerprint)
    ids = {
        str(row.get("ID") or "").strip()
        for row in batch.get("rows", [])
        if str(row.get("ID") or "").strip()
    }
    if not ids:
        raise SystemExit("Queue batch has no lead IDs.")
    credentials = Path(args.credentials)
    prefinal = matching_rows(credentials, args.sheet_url, "Pre-final", ids)
    review = matching_rows(credentials, args.sheet_url, "Lead Review", ids)
    missing_prefinal = sorted(ids - set(prefinal["matches"]))
    missing_review = sorted(ids - set(review["matches"]))
    if missing_prefinal or missing_review:
        raise SystemExit(
            "Refusing partial backfill; missing from "
            f"Pre-final: {', '.join(missing_prefinal) or 'none'}; "
            f"Lead Review: {', '.join(missing_review) or 'none'}"
        )
    lane_by_id = {
        str(row.get("ID") or "").strip(): (
            args.lane if args.lane else ("Automation" if index % 2 == 0 else "Design")
        )
        for index, row in enumerate(batch["rows"])
    }
    if not args.dry_run:
        for row in batch["rows"]:
            row["Primary Lane"] = lane_by_id[str(row.get("ID") or "").strip()]
        save_batch(batch)
        for target in (prefinal, review):
            updates: list[dict[str, Any]] = []
            for lead_id, row_number in target["matches"].items():
                cell = gspread.utils.rowcol_to_a1(row_number, target["lane_column"])
                updates.append({"range": cell, "values": [[lane_by_id[lead_id]]]})
            target["worksheet"].batch_update(updates, value_input_option="USER_ENTERED")
    print(
        {
            "ok": True,
            "dry_run": args.dry_run,
            "queue_fingerprint": args.queue_fingerprint,
            "lanes": {
                lane: sum(1 for value in lane_by_id.values() if value == lane)
                for lane in ("Automation", "Design")
            },
            "queue_rows": len(ids),
            "prefinal_rows": len(prefinal["matches"]),
            "review_rows": len(review["matches"]),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
