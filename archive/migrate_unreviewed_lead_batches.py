#!/usr/bin/env python3
"""Safely move historical unreviewed research into the reusable archive."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from lead_exec_research import (
    COMPUTATIONS_DIR,
    DEFAULT_CREDS,
    DEFAULT_OBF_SHEET_URL,
    DEFAULT_SHEET_URL,
    STATE_DIR,
    load_run,
    save_run,
)
from lead_research_archive import ResearchArchive, has_reusable_research
from prefinal_queue import list_batches, update_batch_status
from sheets_helper import get_client, open_sheet

DEFAULT_DATES = (
    "2026-07-16",
    "2026-07-17",
    "2026-07-20",
    "2026-07-21",
    "2026-07-22",
    "2026-07-23",
    "2026-07-24",
)
MIGRATIONS_DIR = STATE_DIR / "migrations"
ARCHIVE_INDEX = STATE_DIR / "research_archive" / "index.json"


def clean(value: Any) -> str:
    return str(value or "").strip()


def tab_ids(spreadsheet, tab: str) -> set[str]:
    values = spreadsheet.worksheet(tab).get_all_values()
    if not values or "ID" not in values[0]:
        return set()
    index = values[0].index("ID")
    return {clean(row[index]) for row in values[1:] if len(row) > index and clean(row[index])}


def target_computations(dates: list[str]) -> list[Path]:
    paths = []
    for date in dates:
        prefix = date.replace("-", "")
        matches = sorted(COMPUTATIONS_DIR.glob(f"{prefix}_*_computation.json"))
        if len(matches) != 1:
            raise ValueError(f"Expected one computation for {date}; found {len(matches)}.")
        paths.append(matches[0])
    return paths


def review_groups(worksheet, target_dates: set[str]) -> list[dict[str, Any]]:
    values = worksheet.get_all_values()
    headers = values[0]
    date_index = headers.index("Date")
    run_index = headers.index("Run ID")
    complete_index = headers.index("Design Review Complete")
    groups = []
    current = None
    for row_number, raw in enumerate(values[1:], start=2):
        row = raw + [""] * (len(headers) - len(raw))
        date_value = clean(row[date_index])
        run_id = clean(row[run_index])
        if date_value:
            if current:
                groups.append(current)
            current = {
                "date": date_value,
                "start_row": row_number,
                "end_row": row_number,
                "review_complete": clean(row[complete_index]),
                "rows": [row],
                "lead_ids": [],
            }
        elif current and run_id:
            current["end_row"] = row_number
            current["rows"].append(row)
            current["lead_ids"].append(run_id)
    if current:
        groups.append(current)
    return [group for group in groups if group["date"] in target_dates]


def iso_to_sheet_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def build_audit(dates: list[str]) -> dict[str, Any]:
    client = get_client(str(DEFAULT_CREDS))
    leads_sheet = open_sheet(client, DEFAULT_SHEET_URL)
    obf_sheet = open_sheet(client, DEFAULT_OBF_SHEET_URL)
    final_ids = tab_ids(leads_sheet, "Final")
    prefinal_ids = tab_ids(leads_sheet, "Pre-final")
    prospect_ids = tab_ids(obf_sheet, "Prospects")
    computation_paths = target_computations(dates)
    computations = [(path, load_run(path)) for path in computation_paths]
    target_ids = {
        clean(lead.get("lead_id"))
        for _path, computation in computations
        for lead in computation.get("leads", [])
        if clean(lead.get("lead_id"))
    }
    reusable_ids = {
        clean(lead.get("lead_id"))
        for _path, computation in computations
        for lead in computation.get("leads", [])
        if clean(lead.get("lead_id")) and has_reusable_research(lead)
    }
    target_sheet_dates = {iso_to_sheet_date(date) for date in dates}
    groups = review_groups(leads_sheet.worksheet("Lead Review"), target_sheet_dates)
    group_ids = {lead_id for group in groups for lead_id in group["lead_ids"]}
    return {
        "dates": dates,
        "computation_files": [str(path) for path in computation_paths],
        "target_lead_count": len(target_ids),
        "reusable_research_count": len(reusable_ids),
        "incomplete_research_count": len(target_ids - reusable_ids),
        "already_in_final": sorted(target_ids & final_ids),
        "already_in_prospects": sorted(target_ids & prospect_ids),
        "prefinal_target_ids": sorted(target_ids & prefinal_ids),
        "prefinal_non_target_ids": sorted(prefinal_ids - target_ids),
        "review_group_count": len(groups),
        "review_group_ids_missing_from_computations": sorted(group_ids - target_ids),
        "computation_ids_missing_from_review_groups": sorted(target_ids - group_ids),
        "groups": groups,
    }


def apply_migration(audit: dict[str, Any]) -> dict[str, Any]:
    if audit["already_in_final"] or audit["already_in_prospects"]:
        raise ValueError(
            "Refusing migration because target leads already reached an outreach destination."
        )
    if audit["prefinal_non_target_ids"]:
        raise ValueError("Refusing to clear Pre-final because it contains non-target leads.")
    if (
        audit["review_group_ids_missing_from_computations"]
        or audit["computation_ids_missing_from_review_groups"]
    ):
        raise ValueError("Refusing migration because review groups and computations do not match.")
    if audit["review_group_count"] != len(audit["dates"]):
        raise ValueError("Refusing migration because one or more target review groups are missing.")
    reviewed = [
        group
        for group in audit["groups"]
        if clean(group.get("review_complete")).lower() in {"true", "yes", "1", "checked"}
    ]
    if reviewed:
        raise ValueError("Refusing migration because at least one target group is marked reviewed.")

    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    migration_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = MIGRATIONS_DIR / f"{migration_id}_unreviewed_archive_migration.json"
    manifest = {
        "migration_id": migration_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "backup_created",
        "audit": audit,
        "actions": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    archive = ResearchArchive(ARCHIVE_INDEX)
    for computation_file_value in audit["computation_files"]:
        computation_file = Path(computation_file_value)
        computation = load_run(computation_file)
        result = archive.archive_computation(
            computation,
            computation_file=str(computation_file),
            reason="historical_unreviewed_migration",
        )
        computation["status"] = "archived_unreviewed_migration"
        computation["archive_migration"] = {
            "migration_id": migration_id,
            "manifest_file": str(manifest_path),
            **result,
        }
        save_run(computation, computation_file)
        run_file_value = clean(computation.get("source_run_file"))
        if run_file_value and Path(run_file_value).exists():
            run = load_run(Path(run_file_value))
            run["status"] = "archived_unreviewed_migration"
            run["archive_migration"] = computation["archive_migration"]
            save_run(run, Path(run_file_value))
        manifest["actions"].append(
            {"action": "archive_computation", "file": str(computation_file), **result}
        )

    target_computation_files = set(audit["computation_files"])
    for batch in list_batches():
        if clean(batch.get("source_computation_file")) not in target_computation_files:
            continue
        updated = update_batch_status(
            batch["fingerprint"],
            "archived_unreviewed",
            archive_migration_id=migration_id,
            archive_manifest_file=str(manifest_path),
        )
        manifest["actions"].append(
            {"action": "retire_prefinal_queue", "fingerprint": updated["fingerprint"]}
        )

    client = get_client(str(DEFAULT_CREDS))
    leads_sheet = open_sheet(client, DEFAULT_SHEET_URL)
    prefinal = leads_sheet.worksheet("Pre-final")
    if audit["prefinal_target_ids"]:
        prefinal.batch_clear([f"A2:W{prefinal.row_count}"])
        manifest["actions"].append(
            {"action": "clear_prefinal", "lead_count": len(audit["prefinal_target_ids"])}
        )

    review = leads_sheet.worksheet("Lead Review")
    for group in sorted(audit["groups"], key=lambda item: item["start_row"], reverse=True):
        review.delete_rows(group["start_row"], group["end_row"])
        manifest["actions"].append(
            {
                "action": "delete_review_group",
                "date": group["date"],
                "start_row": group["start_row"],
                "end_row": group["end_row"],
                "lead_count": len(group["lead_ids"]),
            }
        )

    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["archive_stats"] = archive.stats()
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return {"manifest_file": str(manifest_path), **manifest["archive_stats"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the audited migration.")
    parser.add_argument("--dates", nargs="+", default=list(DEFAULT_DATES))
    args = parser.parse_args()
    audit = build_audit(args.dates)
    if not args.apply:
        printable = {key: value for key, value in audit.items() if key != "groups"}
        printable["groups"] = [
            {
                "date": group["date"],
                "start_row": group["start_row"],
                "end_row": group["end_row"],
                "review_complete": group["review_complete"],
                "lead_count": len(group["lead_ids"]),
            }
            for group in audit["groups"]
        ]
        print(json.dumps(printable, indent=2, ensure_ascii=False))
        return 0
    print(json.dumps(apply_migration(audit), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
