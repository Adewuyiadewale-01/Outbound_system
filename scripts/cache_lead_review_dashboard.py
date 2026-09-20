#!/usr/bin/env python3
"""Build the read-only Lead Review cache consumed by the local dashboard."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from lead_exec_research import (
    DEFAULT_CREDS,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    parse_review_group_date,
)
from sheets_helper import get_client, get_worksheet, open_sheet

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_FILE = ROOT / "state" / "lead_exec_research" / "dashboard_cache.json"


def clean(value: Any) -> str:
    return str(value or "").strip()


def checkbox_truthy(value: Any) -> bool:
    return clean(value).lower() in {"true", "yes", "1", "checked", "x"}


def normalized_use(value: Any) -> str:
    return " ".join(clean(value).lower().split())


def row_value(row: list[Any], indexes: dict[str, int], column: str) -> str:
    index = indexes.get(column)
    if index is None or index >= len(row):
        return ""
    return clean(row[index])


def build_dashboard_cache(
    values: list[list[Any]],
    *,
    now: datetime | None = None,
    sheet_url: str = DEFAULT_SHEET_URL,
    review_tab: str = DEFAULT_REVIEW_TAB,
) -> dict[str, Any]:
    now = now or datetime.now()
    headers = [clean(value) for value in (values[0] if values else [])]
    indexes = {header: index for index, header in enumerate(headers) if header}
    required = {"Date", "Run ID", "Company Name", "Approved", "Use"}
    missing = sorted(required - set(indexes))
    if missing:
        raise ValueError(f"{review_tab} is missing required columns: {', '.join(missing)}")

    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish_group() -> None:
        nonlocal current
        if not current:
            return
        leads = current["leads"]
        current.update(
            {
                "prepared_count": len(leads),
                "approved_count": sum(1 for lead in leads if lead["approved"]),
                "case_study_worthy_count": sum(
                    1 for lead in leads if normalized_use(lead["use"]) == "case study worthy"
                ),
                "archive_match_count": sum(
                    1 for lead in leads if lead["overlap_status"] == "Archive Match"
                ),
                "conflict_count": sum(
                    1 for lead in leads if lead["overlap_status"] == "Possible Match"
                ),
                "fresh_count": sum(1 for lead in leads if lead["overlap_status"] == "Fresh"),
            }
        )
        groups.append(current)
        current = None

    for row_number, raw_row in enumerate(values[1:], start=2):
        row = list(raw_row)
        date_value = row_value(row, indexes, "Date")
        parsed_date = parse_review_group_date(date_value)
        run_id = row_value(row, indexes, "Run ID")
        if parsed_date:
            finish_group()
            current = {
                "date": parsed_date.isoformat(),
                "display_date": date_value,
                "group_row": row_number,
                "status": row_value(row, indexes, "Status"),
                "notes": row_value(row, indexes, "Notes"),
                "review_complete": checkbox_truthy(
                    row_value(row, indexes, "Design Review Complete")
                ),
                "leads": [],
            }
            continue
        if not current or not run_id:
            continue
        current["leads"].append(
            {
                "row": row_number,
                "run_id": run_id,
                "primary_lane": row_value(row, indexes, "Primary Lane"),
                "company": row_value(row, indexes, "Company Name"),
                "website": row_value(row, indexes, "Company Website"),
                "employee_count": row_value(row, indexes, "Emp Count"),
                "approved": checkbox_truthy(row_value(row, indexes, "Approved")),
                "use": row_value(row, indexes, "Use"),
                "status": row_value(row, indexes, "Status"),
                "notes": row_value(row, indexes, "Notes"),
                "prep_wave": row_value(row, indexes, "Prep Wave") or "Base",
                "overlap_status": row_value(row, indexes, "Overlap Status") or "Fresh",
                "archive_entry_id": row_value(row, indexes, "Archive Entry ID"),
            }
        )
    finish_group()

    all_leads = [lead for group in groups for lead in group["leads"]]
    today_key = now.date().isoformat()
    today_group = next((group for group in reversed(groups) if group["date"] == today_key), None)
    history = [
        {key: value for key, value in group.items() if key != "leads"} for group in reversed(groups)
    ]
    return {
        "schema_version": 1,
        "cached_at": now.isoformat(timespec="seconds"),
        "cache_date": today_key,
        "source": {
            "sheet_url": sheet_url,
            "tab": review_tab,
            "rows_scanned": len(values),
            "columns_scanned": len(headers),
        },
        "summary": {
            "total_leads": len(all_leads),
            "total_case_study_worthy": sum(
                1 for lead in all_leads if normalized_use(lead["use"]) == "case study worthy"
            ),
            "total_approved": sum(1 for lead in all_leads if lead["approved"]),
            "date_groups": len(groups),
            "reviewed_date_groups": sum(1 for group in groups if group["review_complete"]),
        },
        "today": today_group
        or {
            "date": today_key,
            "display_date": "",
            "group_row": None,
            "status": "",
            "notes": "",
            "review_complete": False,
            "prepared_count": 0,
            "approved_count": 0,
            "case_study_worthy_count": 0,
            "archive_match_count": 0,
            "conflict_count": 0,
            "fresh_count": 0,
            "leads": [],
        },
        "history": history,
    }


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument(
        "--target-date",
        help="Optional YYYY-MM-DD review date to cache instead of the local calendar date.",
    )
    args = parser.parse_args()

    cache_now = None
    if args.target_date:
        try:
            cache_now = datetime.fromisoformat(f"{args.target_date}T12:00:00")
        except ValueError:
            parser.error("--target-date must use YYYY-MM-DD.")

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, args.review_tab)
    payload = build_dashboard_cache(
        worksheet.get_all_values(),
        now=cache_now,
        sheet_url=args.sheet_url,
        review_tab=args.review_tab,
    )
    cache_file = Path(args.cache_file)
    write_cache(cache_file, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "cache_file": str(cache_file),
                "cached_at": payload["cached_at"],
                "summary": payload["summary"],
                "today": {key: value for key, value in payload["today"].items() if key != "leads"},
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
