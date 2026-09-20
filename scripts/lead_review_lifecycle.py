#!/usr/bin/env python3
"""Lifecycle and routing rules for prepared, researched, and archived leads.

This module deliberately keeps review decisions separate from executive research.
The research workflow can process a complete date group; these helpers decide
whether each completed lead is published to Pre-final or retained in Lead Archive.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from gspread.utils import ValidationConditionType

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from lead_exec_research import (  # noqa: E402
    DEFAULT_CREDS,
    DEFAULT_DESTINATION_TAB,
    DEFAULT_RESEARCH_ARCHIVE_INDEX,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    ResearchArchive,
    build_destination_row_from_computation,
    clean_text,
    filter_ready_prefinal_rows,
    load_run,
    save_run,
    verify_destination_rows,
    write_destination_rows,
)
from prefinal_queue import enqueue_batch, record_prefinal_publish  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

DEFAULT_ARCHIVE_TAB = "Lead Archive"
DEFAULT_THRESHOLD = 20
SLICE_STATE_PATH = ROOT / "state" / "lead_exec_research" / "lifecycle_slices.json"
TERMINAL_REVIEW_STATUSES = {
    "Processed — Pre-final",
    "Processed — Archived",
    "Processed — Mixed",
    "Bridged from Archive",
}

ARCHIVE_COLUMNS = [
    "Archive Entry ID",
    "Archive Batch ID",
    "Archived At",
    "Source Group Date",
    "Source Run ID",
    "Status",
    "Approved",
    "Bridged At",
    "Original Lane",
    "Primary Lane",
    "Use",
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
    "P3 Name",
    "P3 Title",
    "P3 LinkedIn",
    "P3 Email",
    "Notes",
]


def checked(value: Any) -> bool:
    return clean_text(value).lower() in {"true", "yes", "y", "1", "checked", "x"}


def parse_review_slice(value: Any) -> dict[str, int] | None:
    raw = clean_text(value).lower()
    if not raw or raw in {"all", "none"}:
        return None
    if "/" not in raw:
        raise ValueError("review_slice must use N/M format, for example 1/3.")
    index_raw, total_raw = raw.split("/", 1)
    try:
        index = int(index_raw)
        total = int(total_raw)
    except ValueError as exc:
        raise ValueError("review_slice must use numeric N/M format.") from exc
    if total < 1 or index < 1 or index > total:
        raise ValueError("review_slice requires 1 <= N <= M.")
    return {"index": index, "total": total}


def normalized_lane(value: Any) -> str:
    lane = clean_text(value).lower()
    if lane == "design":
        return "Design"
    if lane == "automation":
        return "Automation"
    return clean_text(value)


def classify_review_rows(
    rows: Sequence[dict[str, Any]],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    checkpoint: str = "first",
    lane_scope: str = "all",
    review_slice: str = "",
    approval_required: bool = True,
) -> dict[str, Any]:
    """Return the group action and the exact per-lead destination.

    A non-empty Use value means the Design row was reviewed, including values
    such as Exclude. Such an unapproved Design row is eligible for Automation.
    """

    if checkpoint not in {"first", "fallback"}:
        raise ValueError("checkpoint must be 'first' or 'fallback'")
    normalized_scope = clean_text(lane_scope).lower()
    if normalized_scope not in {"all", "design", "automation"}:
        raise ValueError("lane_scope must be 'all', 'design', or 'automation'")
    slice_info = parse_review_slice(review_slice)
    normalized = [dict(row) for row in rows if clean_text(row.get("Run ID") or row.get("ID"))]
    approved_design = [
        row
        for row in normalized
        if normalized_lane(row.get("Primary Lane")) == "Design" and checked(row.get("Approved"))
    ]
    if not approval_required:
        routed_rows = normalized
        if normalized_scope in {"design", "automation"}:
            routed_rows = [
                row
                for row in normalized
                if normalized_lane(row.get("Primary Lane")).lower() == normalized_scope
            ]
        prefinal = []
        for row in routed_rows:
            routed = dict(row)
            lane = normalized_lane(row.get("Primary Lane"))
            routed["Original Lane"] = lane
            routed["Primary Lane"] = lane
            prefinal.append(routed)
        return {
            "action": "process_all_lanes",
            "threshold": threshold,
            "approved_design_count": len(approved_design),
            "threshold_met": True,
            "prefinal": prefinal,
            "archive": [],
            "total": len(normalized),
        }
    if checkpoint == "fallback" and normalized_scope == "automation":
        prefinal = []
        for row in normalized:
            if normalized_lane(row.get("Primary Lane")) != "Automation":
                continue
            routed = dict(row)
            routed["Original Lane"] = "Automation"
            routed["Primary Lane"] = "Automation"
            prefinal.append(routed)
        return {
            "action": "process_remaining_automation",
            "threshold": threshold,
            "approved_design_count": len(approved_design),
            "threshold_met": True,
            "prefinal": prefinal,
            "archive": [],
            "total": len(normalized),
        }
    threshold_met = len(approved_design) >= threshold or bool(slice_info)
    if not threshold_met and checkpoint == "first":
        return {
            "action": "skip_waiting_fallback",
            "threshold": threshold,
            "approved_design_count": len(approved_design),
            "threshold_met": False,
            "prefinal": [],
            "archive": [],
            "total": len(normalized),
        }
    if not threshold_met:
        return {
            "action": "process_archive_all",
            "threshold": threshold,
            "approved_design_count": len(approved_design),
            "threshold_met": False,
            "prefinal": [],
            "archive": normalized,
            "total": len(normalized),
        }

    prefinal: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []
    routed_rows = normalized
    if normalized_scope in {"design", "automation"}:
        routed_rows = [
            row
            for row in normalized
            if normalized_lane(row.get("Primary Lane")).lower() == normalized_scope
        ]
    elif checkpoint == "first" and not slice_info:
        routed_rows = [
            row for row in normalized if normalized_lane(row.get("Primary Lane")) == "Design"
        ]

    for row in routed_rows:
        lane = normalized_lane(row.get("Primary Lane"))
        approved = checked(row.get("Approved"))
        reviewed_use = bool(clean_text(row.get("Use")))
        routed = dict(row)
        routed["Original Lane"] = lane
        if lane == "Automation":
            routed["Primary Lane"] = "Automation"
            prefinal.append(routed)
        elif approved:
            routed["Primary Lane"] = "Design"
            prefinal.append(routed)
        elif reviewed_use:
            routed["Primary Lane"] = "Automation"
            prefinal.append(routed)
        else:
            archive.append(routed)
    return {
        "action": "process_mixed",
        "threshold": threshold,
        "approved_design_count": len(approved_design),
        "threshold_met": True,
        "prefinal": prefinal,
        "archive": archive,
        "total": len(normalized),
    }


def classify_computation_leads(
    leads: Sequence[dict[str, Any]],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    checkpoint: str = "first",
    lane_scope: str = "all",
    review_slice: str = "",
    approval_required: bool = True,
) -> dict[str, Any]:
    review_rows = []
    by_id = {}
    for lead in leads:
        lead_id = clean_text(lead.get("lead_id") or lead.get("id"))
        if not lead_id:
            continue
        by_id[lead_id] = lead
        review_rows.append(
            {
                "Run ID": lead_id,
                "Primary Lane": lead.get("primary_lane", ""),
                "Approved": lead.get("review_approved", False),
                "Use": lead.get("use", ""),
            }
        )
    plan = classify_review_rows(
        review_rows,
        threshold=threshold,
        checkpoint=checkpoint,
        lane_scope=lane_scope,
        review_slice=review_slice,
        approval_required=approval_required,
    )
    for key in ("prefinal", "archive"):
        selected = []
        for row in plan[key]:
            lead = deepcopy(by_id[clean_text(row.get("Run ID"))])
            lead["original_lane"] = normalized_lane(lead.get("primary_lane"))
            if key == "prefinal":
                lead["primary_lane"] = row.get("Primary Lane", lead.get("primary_lane", ""))
            selected.append(lead)
        plan[key] = selected
    return plan


def archive_promotion_plan(
    rows: Sequence[dict[str, Any]],
    *,
    design_threshold: int = DEFAULT_THRESHOLD,
    automation_limit: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    available = [row for row in rows if clean_text(row.get("Status") or "Available") == "Available"]
    approved_design = [
        row
        for row in available
        if normalized_lane(row.get("Original Lane") or row.get("Primary Lane")) == "Design"
        and checked(row.get("Approved"))
    ]
    automation_available = [
        row
        for row in available
        if normalized_lane(row.get("Original Lane") or row.get("Primary Lane")) == "Automation"
    ]
    if len(approved_design) < design_threshold or len(automation_available) < automation_limit:
        return {
            "ready": False,
            "approved_design_count": len(approved_design),
            "automation_available_count": len(automation_available),
            "design": [],
            "automation": [],
        }
    automation = automation_available[:automation_limit]
    return {
        "ready": True,
        "approved_design_count": len(approved_design),
        "automation_available_count": len(automation_available),
        "design": approved_design[:design_threshold],
        "automation": automation,
    }


def computation_next_action(computation: dict[str, Any]) -> dict[str, Any]:
    leads = computation.get("leads", [])
    research_pending = [
        lead
        for lead in leads
        if lead.get("status") == "research_pending" and not lead.get("executives")
    ]
    reconciliation_pending = [
        lead
        for lead in leads
        if lead.get("executives") and lead.get("status") == "reconciliation_pending"
    ]
    pending_search = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    conflicts = [lead for lead in leads if lead.get("status") == "archive_conflict"]
    terminal = clean_text(computation.get("status")).startswith("processed_")
    if terminal:
        action = "skip_processed"
    elif research_pending:
        action = "research_batch"
    elif reconciliation_pending or (
        computation.get("status") == "reconciliation_pending"
        and not computation.get("search_tasks")
    ):
        action = "reconcile_existing"
    elif pending_search:
        action = "run_playwright_search"
    elif conflicts:
        action = "resolve_archive_conflicts"
    else:
        action = "finalize_lifecycle"
    return {
        "next_action": action,
        "resumable": action not in {"skip_processed", "resolve_archive_conflicts"},
        "counts": {
            "lead_count": len(leads),
            "research_pending": len(research_pending),
            "reconciliation_pending": len(reconciliation_pending),
            "search_tasks_pending": len(pending_search),
            "archive_conflicts": len(conflicts),
        },
    }


def ensure_archive_worksheet(spreadsheet, tab_name: str = DEFAULT_ARCHIVE_TAB):
    try:
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=len(ARCHIVE_COLUMNS))
        worksheet.update(
            range_name="A1", values=[ARCHIVE_COLUMNS], value_input_option="USER_ENTERED"
        )
        worksheet.freeze(rows=1)
        worksheet.format(
            f"A1:{gspread.utils.rowcol_to_a1(1, len(ARCHIVE_COLUMNS))}",
            {
                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                "textFormat": {"bold": True},
            },
        )
        worksheet.columns_auto_resize(0, len(ARCHIVE_COLUMNS))
        worksheet.set_basic_filter(
            f"A1:{gspread.utils.rowcol_to_a1(worksheet.row_count, len(ARCHIVE_COLUMNS))}"
        )
    headers = worksheet.row_values(1)
    missing = [column for column in ARCHIVE_COLUMNS if column not in headers]
    if missing:
        if worksheet.col_count < len(headers) + len(missing):
            worksheet.add_cols(len(headers) + len(missing) - worksheet.col_count)
        start = len(headers) + 1
        worksheet.update(
            range_name=gspread.utils.rowcol_to_a1(1, start),
            values=[missing],
            value_input_option="USER_ENTERED",
        )
        headers.extend(missing)
    approved_column = headers.index("Approved") + 1
    worksheet.add_validation(
        f"{gspread.utils.rowcol_to_a1(2, approved_column)}:{gspread.utils.rowcol_to_a1(worksheet.row_count, approved_column)}",
        ValidationConditionType.boolean,
        [],
    )
    return worksheet


def archive_sheet_row(
    lead: dict[str, Any],
    *,
    archive_entry_id: str,
    batch_id: str,
    group_date: str,
    archived_at: str,
) -> dict[str, Any]:
    destination = build_destination_row_from_computation(lead)
    return {
        "Archive Entry ID": archive_entry_id,
        "Archive Batch ID": batch_id,
        "Archived At": archived_at,
        "Source Group Date": group_date,
        "Source Run ID": clean_text(lead.get("lead_id")),
        "Status": "Available",
        "Approved": bool(lead.get("review_approved")),
        "Bridged At": "",
        "Original Lane": normalized_lane(lead.get("original_lane") or lead.get("primary_lane")),
        "Primary Lane": normalized_lane(lead.get("primary_lane")),
        "Use": clean_text(lead.get("use")),
        **destination,
        "Notes": " | ".join(clean_text(note) for note in lead.get("notes", []) if clean_text(note)),
    }


def sync_archive_rows(
    spreadsheet,
    leads: Sequence[dict[str, Any]],
    *,
    archive: ResearchArchive,
    computation_file: Path,
    source_run_file: str,
    group_date: str,
    tab_name: str = DEFAULT_ARCHIVE_TAB,
) -> dict[str, Any]:
    worksheet = ensure_archive_worksheet(spreadsheet, tab_name)
    headers = worksheet.row_values(1)
    existing = worksheet.get_all_records(default_blank="")
    row_by_entry = {
        clean_text(row.get("Archive Entry ID")): index
        for index, row in enumerate(existing, start=2)
        if clean_text(row.get("Archive Entry ID"))
    }
    archived_at = datetime.now().isoformat(timespec="seconds")
    batch_id = computation_file.stem
    appended = 0
    updated = 0
    entry_ids = []
    for lead in leads:
        entry = archive.upsert_computation_lead(
            lead,
            source_computation_file=str(computation_file),
            source_run_file=source_run_file,
            reason="lead_review_lifecycle",
        )
        entry_id = entry["archive_entry_id"]
        entry_ids.append(entry_id)
        row = archive_sheet_row(
            lead,
            archive_entry_id=entry_id,
            batch_id=batch_id,
            group_date=group_date,
            archived_at=archived_at,
        )
        payload = [row.get(header, "") for header in headers]
        if entry_id in row_by_entry:
            worksheet.update(
                f"A{row_by_entry[entry_id]}", [payload], value_input_option="USER_ENTERED"
            )
            updated += 1
        else:
            worksheet.append_row(payload, value_input_option="USER_ENTERED")
            appended += 1
    archive.save()
    return {"entry_ids": entry_ids, "appended": appended, "updated": updated, "tab": tab_name}


def publish_prefinal_subset(
    spreadsheet,
    leads: Sequence[dict[str, Any]],
    *,
    computation_file: Path,
    destination_tab: str = DEFAULT_DESTINATION_TAB,
    credentials: Path = DEFAULT_CREDS,
    sheet_url: str = DEFAULT_SHEET_URL,
) -> dict[str, Any]:
    rows = [build_destination_row_from_computation(lead) for lead in leads]
    ready, skipped = filter_ready_prefinal_rows(rows, require_p2=False, include_unresolved=False)
    if skipped:
        raise ValueError(f"Refusing lifecycle publish: {len(skipped)} routed leads are unresolved")
    if not ready:
        return {"rows_written": 0, "queue_batch": {}, "rows": []}
    queue_batch = enqueue_batch(ready, str(computation_file))
    if queue_batch.get("status") == "prospects_bridged":
        raise ValueError(f"Queue batch {queue_batch['fingerprint']} is already completed")
    try:
        count = write_destination_rows(credentials, sheet_url, destination_tab, ready, start_row=2)
        verified = verify_destination_rows(credentials, sheet_url, destination_tab, ready, 2)
        if not verified:
            record_prefinal_publish(
                queue_batch["fingerprint"],
                start_row=2,
                verified=False,
                error="readback_fingerprint_mismatch",
            )
            raise RuntimeError("Pre-final lifecycle write failed readback verification")
        queue_batch = record_prefinal_publish(
            queue_batch["fingerprint"], start_row=2, verified=True
        )
    except Exception as exc:
        record_prefinal_publish(
            queue_batch["fingerprint"], start_row=2, verified=False, error=str(exc)
        )
        raise
    return {"rows_written": count, "queue_batch": queue_batch, "rows": ready}


def update_review_statuses(
    spreadsheet,
    lead_ids: Iterable[str],
    *,
    status: str,
    notes: str,
    review_tab: str = DEFAULT_REVIEW_TAB,
) -> int:
    worksheet = get_worksheet(spreadsheet, review_tab)
    values = worksheet.get_all_values()
    if not values:
        return 0
    headers = values[0]
    for required in ("Run ID", "Status", "Notes"):
        if required not in headers:
            raise ValueError(f"{review_tab} is missing required lifecycle column: {required}")
    ids = {clean_text(value) for value in lead_ids if clean_text(value)}
    run_index = headers.index("Run ID")
    status_column = headers.index("Status") + 1
    notes_column = headers.index("Notes") + 1
    updates = []
    for row_number, raw in enumerate(values[1:], start=2):
        row = raw + [""] * (len(headers) - len(raw))
        if clean_text(row[run_index]) not in ids:
            continue
        updates.extend(
            [
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, status_column),
                    "values": [[status]],
                },
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, notes_column),
                    "values": [[notes]],
                },
            ]
        )
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates) // 2


def update_review_group_status(
    spreadsheet,
    source_run_file: str,
    *,
    status: str,
    notes: str,
    review_complete: bool,
    review_tab: str = DEFAULT_REVIEW_TAB,
) -> dict[str, Any]:
    if not source_run_file or not Path(source_run_file).exists():
        return {"updated": False, "reason": "source_run_file_missing"}
    run = load_run(Path(source_run_file))
    group_row = run.get("review", {}).get("write", {}).get("group_row") or run.get(
        "review_write", {}
    ).get("group_row")
    if not group_row:
        return {"updated": False, "reason": "group_row_missing"}
    worksheet = get_worksheet(spreadsheet, review_tab)
    headers = worksheet.row_values(1)
    updates = []
    for column, value in (
        ("Status", status),
        ("Notes", notes),
        ("Design Review Complete", "TRUE" if review_complete else "FALSE"),
    ):
        if column not in headers:
            continue
        updates.append(
            {
                "range": gspread.utils.rowcol_to_a1(int(group_row), headers.index(column) + 1),
                "values": [[value]],
            }
        )
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    return {"updated": bool(updates), "group_row": int(group_row), "status": status}


def load_slice_state() -> dict[str, Any]:
    if not SLICE_STATE_PATH.exists():
        return {}
    return json.loads(SLICE_STATE_PATH.read_text())


def save_slice_state(state: dict[str, Any]) -> None:
    SLICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SLICE_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def slice_state_key(source_run_file: str, group_date: str) -> str:
    return clean_text(source_run_file) or clean_text(group_date) or "unknown_group"


def record_slice_completion(
    *,
    computation_file: Path,
    source_run_file: str,
    group_date: str,
    review_slice: str,
    prefinal_count: int,
    archive_count: int,
) -> dict[str, Any]:
    parsed = parse_review_slice(review_slice)
    if not parsed:
        return {"sliced": False, "all_slices_complete": True}
    state = load_slice_state()
    key = slice_state_key(source_run_file, group_date)
    group = state.setdefault(
        key,
        {
            "source_run_file": clean_text(source_run_file),
            "group_date": clean_text(group_date),
            "total_slices": parsed["total"],
            "completed": {},
        },
    )
    group["total_slices"] = parsed["total"]
    group["updated_at"] = datetime.now().isoformat(timespec="seconds")
    group.setdefault("completed", {})[str(parsed["index"])] = {
        "review_slice": review_slice,
        "computation_file": str(computation_file),
        "completed_at": group["updated_at"],
        "prefinal_count": prefinal_count,
        "archive_count": archive_count,
    }
    completed = group.get("completed", {})
    all_done = len(completed) >= parsed["total"]
    aggregate_prefinal = sum(int(item.get("prefinal_count", 0)) for item in completed.values())
    aggregate_archive = sum(int(item.get("archive_count", 0)) for item in completed.values())
    group["all_slices_complete"] = all_done
    group["aggregate_prefinal_count"] = aggregate_prefinal
    group["aggregate_archive_count"] = aggregate_archive
    save_slice_state(state)
    return {
        "sliced": True,
        "key": key,
        "review_slice": review_slice,
        "total_slices": parsed["total"],
        "completed_slices": sorted(completed.keys(), key=int),
        "all_slices_complete": all_done,
        "aggregate_prefinal_count": aggregate_prefinal,
        "aggregate_archive_count": aggregate_archive,
        "state_file": str(SLICE_STATE_PATH),
    }


def read_archive_rows(
    spreadsheet, tab_name: str = DEFAULT_ARCHIVE_TAB
) -> tuple[Any, list[dict[str, Any]]]:
    worksheet = ensure_archive_worksheet(spreadsheet, tab_name)
    rows = worksheet.get_all_records(default_blank="")
    for index, row in enumerate(rows, start=2):
        row["_row_number"] = index
    return worksheet, rows


def archive_status(
    *,
    credentials: Path = DEFAULT_CREDS,
    sheet_url: str = DEFAULT_SHEET_URL,
    archive_tab: str = DEFAULT_ARCHIVE_TAB,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    spreadsheet = open_sheet(get_client(str(credentials)), sheet_url)
    _worksheet, rows = read_archive_rows(spreadsheet, archive_tab)
    plan = archive_promotion_plan(rows, design_threshold=threshold, automation_limit=threshold)
    return {
        "tab": archive_tab,
        "available_count": len(
            [row for row in rows if clean_text(row.get("Status") or "Available") == "Available"]
        ),
        "next_action": "promote_archive" if plan["ready"] else "skip_archive_below_threshold",
        "approved_design_count": plan["approved_design_count"],
        "automation_available_count": plan["automation_available_count"],
        "design_selected": len(plan["design"]),
        "automation_selected": len(plan["automation"]),
        "threshold": threshold,
    }


def promote_archive(
    *,
    credentials: Path = DEFAULT_CREDS,
    sheet_url: str = DEFAULT_SHEET_URL,
    archive_tab: str = DEFAULT_ARCHIVE_TAB,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    spreadsheet = open_sheet(get_client(str(credentials)), sheet_url)
    worksheet, rows = read_archive_rows(spreadsheet, archive_tab)
    plan = archive_promotion_plan(rows, design_threshold=threshold, automation_limit=threshold)
    if not plan["ready"]:
        return {"ok": True, "status": "skipped_archive_below_threshold", **plan}
    selected = []
    for row in plan["design"]:
        routed = dict(row)
        routed["Primary Lane"] = "Design"
        selected.append(routed)
    for row in plan["automation"]:
        routed = dict(row)
        routed["Primary Lane"] = "Automation"
        selected.append(routed)
    destination_rows = [
        {
            column: row.get(column, "")
            for column in (
                "ID",
                "Company",
                "Website",
                "Company LinkedIn",
                "Emp Count",
                "Source Tab",
                "P1 Name",
                "P1 Title",
                "P1 LinkedIn",
                "P1 Email",
                "P2 Name",
                "P2 Title",
                "P2 LinkedIn",
                "P2 Email",
                "P3 Name",
                "P3 Title",
                "P3 LinkedIn",
                "P3 Email",
                "Use",
                "Primary Lane",
            )
        }
        for row in selected
    ]
    ready, skipped = filter_ready_prefinal_rows(
        destination_rows, require_p2=False, include_unresolved=False
    )
    if skipped:
        raise ValueError(
            f"Refusing archive promotion: {len(skipped)} selected archive rows are unresolved"
        )
    queue_batch = enqueue_batch(ready, f"{archive_tab}:promotion")
    count = write_destination_rows(
        credentials, sheet_url, DEFAULT_DESTINATION_TAB, ready, start_row=2
    )
    if not verify_destination_rows(credentials, sheet_url, DEFAULT_DESTINATION_TAB, ready, 2):
        record_prefinal_publish(
            queue_batch["fingerprint"],
            start_row=2,
            verified=False,
            error="readback_fingerprint_mismatch",
        )
        raise RuntimeError("Archive promotion failed Pre-final readback verification")
    queue_batch = record_prefinal_publish(queue_batch["fingerprint"], start_row=2, verified=True)
    headers = worksheet.row_values(1)
    status_column = headers.index("Status") + 1
    bridged_column = headers.index("Bridged At") + 1
    bridged_at = datetime.now().isoformat(timespec="seconds")
    updates = []
    entry_ids = []
    source_ids = []
    for row in selected:
        row_number = int(row["_row_number"])
        entry_ids.append(clean_text(row.get("Archive Entry ID")))
        source_ids.append(clean_text(row.get("Source Run ID") or row.get("ID")))
        updates.extend(
            [
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, status_column),
                    "values": [["Bridged"]],
                },
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, bridged_column),
                    "values": [[bridged_at]],
                },
            ]
        )
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    ResearchArchive(DEFAULT_RESEARCH_ARCHIVE_INDEX).mark_consumed(
        entry_ids, computation_file="archive_promotion", destination=DEFAULT_DESTINATION_TAB
    )
    update_review_statuses(
        spreadsheet,
        source_ids,
        status="Bridged from Archive",
        notes=f"Promoted from {archive_tab} at {bridged_at}",
    )
    return {
        "ok": True,
        "status": "promoted_archive",
        "rows_written": count,
        "design_count": len(plan["design"]),
        "automation_count": len(plan["automation"]),
        "queue_batch": queue_batch,
    }


def finalize_computation(
    computation_file: Path,
    *,
    checkpoint: str,
    threshold: int = DEFAULT_THRESHOLD,
    credentials: Path = DEFAULT_CREDS,
    sheet_url: str = DEFAULT_SHEET_URL,
    archive_tab: str = DEFAULT_ARCHIVE_TAB,
) -> dict[str, Any]:
    computation = load_run(computation_file)
    lane_scope = clean_text(computation.get("review_lane_scope") or "all").lower()
    review_slice = clean_text(computation.get("review_slice"))
    plan = classify_computation_leads(
        computation.get("leads", []),
        threshold=threshold,
        checkpoint=checkpoint,
        lane_scope=lane_scope,
        review_slice=review_slice,
        approval_required=clean_text(computation.get("selection_mode")) != "approval_disabled",
    )
    if plan["action"] == "skip_waiting_fallback":
        return plan
    client = get_client(str(credentials))
    spreadsheet = open_sheet(client, sheet_url)
    archive = ResearchArchive(DEFAULT_RESEARCH_ARCHIVE_INDEX)
    source_run_file = clean_text(computation.get("source_run_file"))
    group_date = clean_text(computation.get("lifecycle", {}).get("group_date"))
    if not group_date and source_run_file and Path(source_run_file).exists():
        source_run = load_run(Path(source_run_file))
        group_date = clean_text(
            source_run.get("review", {}).get("write", {}).get("date")
            or source_run.get("review_write", {}).get("date")
        )

    prefinal_result = publish_prefinal_subset(
        spreadsheet,
        plan["prefinal"],
        computation_file=computation_file,
        credentials=credentials,
        sheet_url=sheet_url,
    )
    archive_result = (
        sync_archive_rows(
            spreadsheet,
            plan["archive"],
            archive=archive,
            computation_file=computation_file,
            source_run_file=source_run_file,
            group_date=group_date,
            tab_name=archive_tab,
        )
        if plan["archive"]
        else {"entry_ids": [], "appended": 0, "updated": 0, "tab": archive_tab}
    )

    prefinal_ids = [clean_text(lead.get("lead_id")) for lead in plan["prefinal"]]
    archive_ids = [clean_text(lead.get("lead_id")) for lead in plan["archive"]]
    if prefinal_ids:
        update_review_statuses(
            spreadsheet,
            prefinal_ids,
            status="Processed — Pre-final",
            notes=f"Lifecycle {computation_file.stem}: routed to Pre-final",
        )
    if archive_ids:
        update_review_statuses(
            spreadsheet,
            archive_ids,
            status="Processed — Archived",
            notes=f"Lifecycle {computation_file.stem}: retained in {archive_tab}",
        )
    group_status = (
        "Processed — Mixed"
        if prefinal_ids and archive_ids
        else "Processed — Pre-final"
        if prefinal_ids
        else "Processed — Archived"
    )
    slice_completion = record_slice_completion(
        computation_file=computation_file,
        source_run_file=source_run_file,
        group_date=group_date,
        review_slice=review_slice,
        prefinal_count=len(prefinal_ids),
        archive_count=len(archive_ids),
    )
    if slice_completion.get("sliced") and not slice_completion.get("all_slices_complete"):
        group_update = {
            "updated": False,
            "reason": "slice_pending",
            **slice_completion,
        }
    else:
        aggregate_prefinal = int(
            slice_completion.get("aggregate_prefinal_count", len(prefinal_ids))
        )
        aggregate_archive = int(slice_completion.get("aggregate_archive_count", len(archive_ids)))
        aggregate_status = (
            "Processed — Mixed"
            if aggregate_prefinal and aggregate_archive
            else "Processed — Pre-final"
            if aggregate_prefinal
            else "Processed — Archived"
        )
        group_update = update_review_group_status(
            spreadsheet,
            source_run_file,
            status=aggregate_status,
            notes=(
                f"Lifecycle {computation_file.stem}: {aggregate_prefinal} to Pre-final, "
                f"{aggregate_archive} to {archive_tab}"
            ),
            review_complete=bool(plan["threshold_met"]),
        )
        group_status = aggregate_status
    computation["lifecycle"] = {
        **computation.get("lifecycle", {}),
        "checkpoint": checkpoint,
        "review_lane_scope": lane_scope,
        "review_slice": review_slice,
        "slice_completion": slice_completion,
        "finalized_at": datetime.now().isoformat(timespec="seconds"),
        "action": plan["action"],
        "approved_design_count": plan["approved_design_count"],
        "threshold": threshold,
        "prefinal_ids": prefinal_ids,
        "archive_ids": archive_ids,
        "archive_tab": archive_tab,
        "group_status": group_status,
    }
    computation["status"] = (
        "processed_mixed"
        if prefinal_ids and archive_ids
        else "processed_prefinal"
        if prefinal_ids
        else "processed_archived"
    )
    computation.setdefault("writes", []).append(
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "destination_tab": DEFAULT_DESTINATION_TAB,
            **prefinal_result,
        }
    )
    computation.setdefault("archive_writes", []).append(archive_result)
    save_run(computation, computation_file)
    return {
        "ok": True,
        "action": plan["action"],
        "approved_design_count": plan["approved_design_count"],
        "prefinal_count": len(prefinal_ids),
        "archive_count": len(archive_ids),
        "prefinal": prefinal_result,
        "archive": archive_result,
        "review_group": group_update,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--input", required=True, help="JSON list of Lead Review-style rows")
    plan.add_argument("--checkpoint", choices=["first", "fallback"], required=True)
    plan.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    plan.add_argument("--lane-scope", choices=["all", "design", "automation"], default="all")
    plan.add_argument("--review-slice", default="")
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--computation-file", required=True)
    finalize.add_argument("--checkpoint", choices=["first", "fallback"], required=True)
    finalize.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    finalize.add_argument("--credentials", default=str(DEFAULT_CREDS))
    finalize.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    finalize.add_argument("--archive-tab", default=DEFAULT_ARCHIVE_TAB)
    ensure = sub.add_parser("ensure-archive-tab")
    ensure.add_argument("--credentials", default=str(DEFAULT_CREDS))
    ensure.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    ensure.add_argument("--archive-tab", default=DEFAULT_ARCHIVE_TAB)
    status_computation = sub.add_parser("status-computation")
    status_computation.add_argument("--computation-file", required=True)
    archive_state = sub.add_parser("archive-status")
    archive_state.add_argument("--credentials", default=str(DEFAULT_CREDS))
    archive_state.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    archive_state.add_argument("--archive-tab", default=DEFAULT_ARCHIVE_TAB)
    archive_state.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    promote = sub.add_parser("promote-archive")
    promote.add_argument("--credentials", default=str(DEFAULT_CREDS))
    promote.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    promote.add_argument("--archive-tab", default=DEFAULT_ARCHIVE_TAB)
    promote.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
        print(
            json.dumps(
                classify_review_rows(
                    rows,
                    threshold=args.threshold,
                    checkpoint=args.checkpoint,
                    lane_scope=args.lane_scope,
                    review_slice=args.review_slice,
                ),
                indent=2,
            )
        )
        return 0
    if args.command == "ensure-archive-tab":
        spreadsheet = open_sheet(get_client(args.credentials), args.sheet_url)
        worksheet = ensure_archive_worksheet(spreadsheet, args.archive_tab)
        print(
            json.dumps(
                {
                    "ok": True,
                    "tab": worksheet.title,
                    "sheet_id": worksheet.id,
                    "headers": worksheet.row_values(1),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "status-computation":
        computation = load_run(Path(args.computation_file))
        print(
            json.dumps(
                {"computation_file": args.computation_file, **computation_next_action(computation)},
                indent=2,
            )
        )
        return 0
    if args.command == "archive-status":
        print(
            json.dumps(
                archive_status(
                    credentials=Path(args.credentials),
                    sheet_url=args.sheet_url,
                    archive_tab=args.archive_tab,
                    threshold=args.threshold,
                ),
                indent=2,
            )
        )
        return 0
    if args.command == "promote-archive":
        print(
            json.dumps(
                promote_archive(
                    credentials=Path(args.credentials),
                    sheet_url=args.sheet_url,
                    archive_tab=args.archive_tab,
                    threshold=args.threshold,
                ),
                indent=2,
                default=str,
            )
        )
        return 0
    result = finalize_computation(
        Path(args.computation_file),
        checkpoint=args.checkpoint,
        threshold=args.threshold,
        credentials=Path(args.credentials),
        sheet_url=args.sheet_url,
        archive_tab=args.archive_tab,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
