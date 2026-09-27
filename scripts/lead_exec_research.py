#!/usr/bin/env python3
"""
Lead executive research workflow.

Reads grouped company/employee rows from a Google Sheet, creates a structured
local JSON run file, searches for top executives without paid APIs, reconciles
LinkedIn URLs against existing employee rows, and writes finalized P1/P2 rows
to a Prospects-style destination tab.
"""

import argparse
import hashlib
import json
import os
import smtplib
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for _path in (str(HELPERS), str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- leads carve: shim re-exports (appended per slice) ---
from lead_research_archive import (  # noqa: E402
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    hydrate_computation_lead,
)
from prefinal_queue import rows_fingerprint  # noqa: E402
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import (  # noqa: E402
    format_sheet_date,
    get_client,
    get_worksheet,
    normalize_rows,
    open_sheet,
    sheet_values_equal,
)

from outbound.leads.computation import (  # noqa: F401
    archive_computation_state,
    archive_unreviewed_computation,
    consume_reused_archive_entries,
)
from outbound.leads.config import (  # noqa: F401
    BRIDGES_DIR,
    COMPUTATIONS_DIR,
    DEFAULT_CREDS,
    DEFAULT_DESTINATION_TAB,
    DEFAULT_FINAL_TAB,
    DEFAULT_NOTIFICATION_QUEUE_TAB,
    DEFAULT_NOTIFY_EMAIL,
    DEFAULT_OBF_SHEET_URL,
    DEFAULT_OUTREACH_CONTROL_TAB,
    DEFAULT_PROSPECTS_TAB,
    DEFAULT_RESEARCH_ARCHIVE_INDEX,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    DEFAULT_SOURCE_TAB,
    DESTINATION_COLUMNS,
    EXEC_TITLE_PATTERNS,
    FINAL_BRIDGE_COLUMNS,
    LEAD_PREP_CONFIG_PATH,
    NOTIFICATION_QUEUE_COLUMNS,
    OPENCLAW_CREDS,
    OPTIONAL_DESTINATION_COLUMNS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    PRIMARY_LANES,
    PROMPTS_DIR,
    PROSPECTS_COLUMNS,
    REPO_CREDS,
    RESEARCH_ARCHIVE_DIR,
    REVIEW_COLUMNS,
    REVIEW_OVERLAP_COLUMNS,
    ROLE_KEYWORDS,
    RUNS_DIR,
    SEARCH_RESULTS_DIR,
    SEARCH_TASKS_DIR,
    SNAPSHOTS_DIR,
    SOURCE_ALIASES,
    SOURCE_COLUMNS,
    STATE_DIR,
)
from outbound.leads.destination import (  # noqa: F401
    build_destination_row,
    build_destination_row_from_computation,
    choose_people,
    computation_rows_for_write,
    filter_ready_prefinal_rows,
    prefinal_row_readiness,
    select_computation_people,
    verify_destination_rows,
    write_computation_rows,
    write_destination_rows,
)
from outbound.leads.extract import (  # noqa: F401
    employee_role_score,
    extract_exec_candidates,
    extract_name_role_pairs,
    fallback_execs_from_employees,
    match_employee,
    reconcile_exec,
    score_search_candidate,
    search_execs_for_company,
    search_linkedin_for_exec,
    seniority_score,
)
from outbound.leads.grouping import (  # noqa: F401
    add_employee,
    assign_primary_lanes,
    chunks,
    group_source_rows,
    looks_like_company_row,
    looks_like_employee_row,
)
from outbound.leads.reviewtab import (  # noqa: F401
    build_review_row,
    checkbox_truthy,
    ensure_review_tab,
    existing_successful_review_group_row,
    find_successful_review_group_row_for_today,
    get_or_create_worksheet,
    last_nonempty_review_row,
    parse_review_group_date,
    read_review_rows,
    remove_review_group_for_run,
    review_group_date_value,
    update_review_statuses,
    write_review_rows,
)
from outbound.leads.runs import (  # noqa: F401
    apply_review_slice,
    approval_gate_enabled,
    archive_index_path,
    computation_fingerprint,
    destination_sheet_url,
    ensure_dirs,
    latest_computation_file,
    latest_computation_file_for_fingerprint,
    latest_review_run_file,
    latest_run_file,
    latest_search_result_file,
    lead_id_fingerprint,
    load_run,
    now_run_id,
    parse_review_slice,
    resolve_ignore_review_approval,
    save_run,
    source_sheet_url,
)
from outbound.leads.search import (  # noqa: F401
    SearchClient,
    parse_bing_results,
    parse_duckduckgo_results,
    parse_yahoo_results,
    search_query_variants,
    strip_tags,
    unwrap_duckduckgo_url,
    unwrap_yahoo_url,
)
from outbound.leads.sheetsio import (  # noqa: F401
    canonicalize_source_rows,
    load_destination_company_by_id,
    load_destination_ids,
    load_reviewed_ids,
    read_worksheet,
    require_columns,
    source_value,
)
from outbound.leads.text import (  # noqa: F401
    clean_person_name,
    clean_role_for_person,
    clean_text,
    compact_company_name,
    is_linkedin_profile_url,
    is_plausible_person_name,
    linkedin_url_name_score,
    meaningful_name_parts,
    normalize_key,
    normalize_url,
    strip_accents,
    title_weight,
    token_overlap_score,
)

load_repo_env()


def parse_bridge_date(value: str) -> datetime:
    raw = clean_text(value)
    if not raw:
        return datetime.now()
    lowered = raw.lower()
    if lowered == "today":
        return datetime.now()
    if lowered == "yesterday":
        return datetime.now() - timedelta(days=1)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported bridge target date: {value}")


def bridge_date_value(value: str) -> str:
    parsed = parse_bridge_date(value)
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def bridge_date_key(value: str) -> str:
    return parse_bridge_date(value).strftime("%Y-%m-%d")


def bridge_state_path(target_date: str, fingerprint: str) -> Path:
    return BRIDGES_DIR / f"{bridge_date_key(target_date)}_{fingerprint}.json"


def prefinal_rows_for_bridge(
    credentials_path: Path,
    sheet_url: str,
    source_tab: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    headers, rows = read_worksheet(credentials_path, sheet_url, source_tab)
    # Pre-final contains DESTINATION_COLUMNS; Final adds FINAL_BRIDGE_COLUMNS.
    required = list(DESTINATION_COLUMNS)
    if "Category" in headers:
        required.extend(FINAL_BRIDGE_COLUMNS)
    require_columns(headers, required, source_tab)
    ready = []
    skipped = []
    for row in rows:
        if not clean_text(row.get("ID")) and not clean_text(row.get("Company")):
            continue
        reasons = []
        for column in ("ID", "Company", "Website"):
            if not clean_text(row.get(column)):
                reasons.append(f"missing_{normalize_key(column).replace(' ', '_')}")
        if "Category" in headers and not clean_text(row.get("Category")):
            reasons.append("missing_category")
        p1_linkedin = clean_text(row.get("P1 LinkedIn"))
        p2_linkedin = clean_text(row.get("P2 LinkedIn"))
        if not p1_linkedin and not p2_linkedin:
            reasons.append("missing_linkedin")
        if p1_linkedin and not is_linkedin_profile_url(p1_linkedin):
            reasons.append("invalid_p1_linkedin")
        if p2_linkedin and not is_linkedin_profile_url(p2_linkedin):
            reasons.append("invalid_p2_linkedin")
        if not reasons:
            ready.append(row)
        else:
            skipped.append(
                {
                    "lead_id": clean_text(row.get("ID")),
                    "company": clean_text(row.get("Company")),
                    "reasons": reasons,
                    "row_number": row.get("_row_number"),
                }
            )
    return headers, ready, skipped


def prefinal_bridge_fingerprint(rows: list[dict[str, Any]]) -> str:
    canonical_rows = []
    for row in rows:
        canonical_rows.append(
            {
                key: clean_text(row.get(key))
                for key in DESTINATION_COLUMNS + OPTIONAL_DESTINATION_COLUMNS + FINAL_BRIDGE_COLUMNS
            }
        )
    payload = json.dumps(canonical_rows, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def pick_engaged_person(row: dict[str, Any]) -> str:
    use = normalize_key(row.get("Use"))
    if "person 2" in use or use in {"p2", "2"}:
        return "Person 2"
    if clean_text(row.get("P1 LinkedIn")):
        return "Person 1"
    if clean_text(row.get("P2 LinkedIn")):
        return "Person 2"
    return "Person 1"


def prospect_row_from_prefinal(row: dict[str, Any]) -> dict[str, Any]:
    prospect = {column: "" for column in PROSPECTS_COLUMNS}
    for column in DESTINATION_COLUMNS:
        prospect[column] = clean_text(row.get(column))
    for column in ("P1 Activity", "P2 Activity"):
        prospect[column] = clean_text(row.get(column))
    prospect["Primary Lane"] = clean_text(row.get("Primary Lane"))
    prospect["Source Tab"] = ""
    prospect["Website"] = normalize_url(prospect.get("Website"))
    prospect["Company LinkedIn"] = normalize_url(prospect.get("Company LinkedIn"))
    prospect["P1 LinkedIn"] = normalize_url(prospect.get("P1 LinkedIn"))
    prospect["P2 LinkedIn"] = normalize_url(prospect.get("P2 LinkedIn"))
    engaged_person = clean_text(row.get("Engaged Person"))
    prospect["Engaged Person"] = (
        engaged_person if engaged_person in {"Person 1", "Person 2"} else pick_engaged_person(row)
    )
    prospect["Notes"] = (
        f"source=final_bridge; category={clean_text(row.get('Category'))}; "
        f"bridged_at={datetime.now().isoformat(timespec='seconds')}"
    )
    return prospect


def existing_prospect_ids(credentials_path: Path, sheet_url: str, prospects_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, prospects_tab)
    except Exception:
        return set()
    if "ID" not in headers:
        return set()
    return {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}


def first_prospect_write_row(rows: list[dict[str, Any]]) -> int:
    last = 1
    for row in rows:
        if any(
            clean_text(row.get(column))
            for column in ("ID", "Company", "P1 LinkedIn", "P2 LinkedIn")
        ):
            last = max(last, int(row.get("_row_number", 1)))
    return last + 1


def write_prospect_rows(
    credentials_path: Path,
    sheet_url: str,
    prospects_tab: str,
    rows: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[int, int]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, prospects_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{prospects_tab} is empty.")
    headers = values[0]
    require_columns(headers, PROSPECTS_COLUMNS, prospects_tab)
    existing_rows = normalize_rows(values)
    start_row = first_prospect_write_row(existing_rows)
    if dry_run or not rows:
        return 0, start_row
    payload = [[row.get(header, "") for header in headers] for row in rows]
    start_cell = gspread.utils.rowcol_to_a1(start_row, 1)
    end_cell = gspread.utils.rowcol_to_a1(start_row + len(payload) - 1, len(headers))
    worksheet.update(
        range_name=f"{start_cell}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return len(payload), start_row


def record_outreach_control_prospects_start_row(
    credentials_path: Path,
    sheet_url: str,
    target_date: str,
    start_row: int,
    control_tab: str = DEFAULT_OUTREACH_CONTROL_TAB,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, control_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{control_tab} is empty.")
    headers = [clean_text(header) for header in values[0]]
    if "Date" not in headers:
        raise ValueError(f"{control_tab} is missing required column: Date")
    if OUTREACH_CONTROL_PROSPECTS_START_ROW not in headers:
        headers.append(OUTREACH_CONTROL_PROSPECTS_START_ROW)
        col = len(headers)
        worksheet.update(
            range_name=gspread.utils.rowcol_to_a1(1, col),
            values=[[OUTREACH_CONTROL_PROSPECTS_START_ROW]],
            value_input_option="USER_ENTERED",
        )
    date_idx = headers.index("Date")
    start_idx = headers.index(OUTREACH_CONTROL_PROSPECTS_START_ROW)
    matches: list[int] = []
    for row_number in range(2, len(values) + 1):
        raw = values[row_number - 1]
        cell_value = raw[date_idx] if date_idx < len(raw) else ""
        if sheet_values_equal(cell_value, target_date):
            matches.append(row_number)
    if not matches:
        # Reuse OBF's daily-row policy instead of inventing target values here.
        from linkedin_outreach_session import _read_outreach_control

        _, control_row, _, _ = _read_outreach_control(
            str(credentials_path),
            sheet_url,
            format_sheet_date(target_date),
            auto_create_missing=True,
        )
        matches.append(int(control_row["_row_number"]))
    if len(matches) > 1:
        raise ValueError(f"Multiple {control_tab} rows found for {format_sheet_date(target_date)}")
    row_number = matches[0]
    worksheet.update(
        range_name=gspread.utils.rowcol_to_a1(row_number, start_idx + 1),
        values=[[str(start_row)]],
        value_input_option="USER_ENTERED",
    )
    return {
        "ok": True,
        "worksheet": control_tab,
        "row_number": row_number,
        "field": OUTREACH_CONTROL_PROSPECTS_START_ROW,
        "value": str(start_row),
    }


def bridge_prefinal_to_prospects(args: argparse.Namespace) -> dict[str, Any]:
    target_date = bridge_date_value(args.target_date)
    target_date_key = bridge_date_key(target_date)
    # Bridge should read from Pre-final by default; allow override via --source-tab.
    source_tab = getattr(args, "source_tab", None) or DEFAULT_DESTINATION_TAB
    headers, prefinal_rows, skipped_unready = prefinal_rows_for_bridge(
        Path(args.credentials),
        source_sheet_url(args),
        source_tab,
    )
    fingerprint = prefinal_bridge_fingerprint(prefinal_rows)
    state_file = bridge_state_path(target_date, fingerprint)
    result: dict[str, Any] = {
        "ok": True,
        "status": "pending",
        "target_date": target_date,
        "target_date_key": target_date_key,
        "source_tab": source_tab,
        "prospects_tab": args.prospects_tab,
        "source_rows_seen": len(prefinal_rows) + len(skipped_unready),
        "source_ready_rows": len(prefinal_rows),
        "prefinal_rows_seen": len(prefinal_rows),
        "fingerprint": fingerprint,
        "state_file": str(state_file),
        "rows_written": 0,
        "write_start_row": None,
        "skipped_existing_ids": [],
        "skipped_unready": skipped_unready,
        "blocked": [],
        "dry_run": bool(args.dry_run),
    }
    if not prefinal_rows:
        result["status"] = "skipped_empty_source"
        if not args.dry_run:
            state_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result
    if state_file.exists() and not args.force:
        previous = json.loads(state_file.read_text())
        if (
            previous.get("write_start_row")
            and previous.get("outreach_control_update", {}).get("ok") is False
        ):
            # Rows already exist: retry only the unfinished control update.
            if args.dry_run:
                return {**previous, "ok": False, "status": "control_sync_pending", "dry_run": True}
            try:
                previous["outreach_control_update"] = record_outreach_control_prospects_start_row(
                    Path(args.credentials),
                    args.prospects_sheet_url,
                    target_date,
                    previous["write_start_row"],
                )
                previous.update(ok=True, status="processed", blocked=[])
            except Exception as exc:
                previous.update(ok=False, status="control_sync_pending", blocked=[str(exc)])
            state_file.write_text(json.dumps(previous, indent=2, ensure_ascii=False) + "\n")
            return previous
        if previous.get("status") == "processed":
            result.update(
                {
                    "status": "skipped_already_bridged",
                    "previous_processed_at": previous.get("processed_at"),
                    "rows_written": previous.get("rows_written", 0),
                }
            )
            return result

    existing_ids = existing_prospect_ids(
        Path(args.credentials), args.prospects_sheet_url, args.prospects_tab
    )
    bridge_rows = []
    for row in prefinal_rows:
        lead_id = clean_text(row.get("ID"))
        if lead_id in existing_ids:
            result["skipped_existing_ids"].append(lead_id)
            continue
        prospect = prospect_row_from_prefinal(row)
        bridge_rows.append(prospect)

    result["candidate_rows"] = len(bridge_rows)
    result["lead_ids"] = [row.get("ID", "") for row in bridge_rows]
    try:
        rows_written, start_row = write_prospect_rows(
            Path(args.credentials),
            args.prospects_sheet_url,
            args.prospects_tab,
            bridge_rows,
            dry_run=args.dry_run,
        )
        result["rows_written"] = rows_written
        result["write_start_row"] = start_row
        if rows_written and not args.dry_run and not getattr(args, "skip_outreach_control", False):
            try:
                result["outreach_control_update"] = record_outreach_control_prospects_start_row(
                    Path(args.credentials),
                    args.prospects_sheet_url,
                    target_date,
                    start_row,
                )
            except Exception as exc:
                result["ok"] = False
                result["outreach_control_update"] = {"ok": False, "error": str(exc)}
                result["blocked"].append(f"Prospects Start Row update failed: {exc}")
        if args.dry_run:
            result["status"] = "dry_run"
        elif rows_written:
            result["status"] = "processed" if result["ok"] else "control_sync_pending"
            result["processed_at"] = datetime.now().isoformat(timespec="seconds")
        elif result["skipped_existing_ids"]:
            result["status"] = "skipped_existing_ids"
        else:
            result["status"] = "skipped_no_candidates"
    except Exception as exc:
        result["ok"] = False
        result["status"] = "failed"
        result["blocked"].append(str(exc))
        result["failed_at"] = datetime.now().isoformat(timespec="seconds")

    if not args.dry_run:
        state_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def approved_gate_status(args: argparse.Namespace) -> dict[str, Any]:
    import automation_gate  # Local script import keeps gate logic in one place.

    gate_args = argparse.Namespace(
        sheet_url=source_sheet_url(args),
        review_tab=args.review_tab,
        credentials=args.credentials,
        threshold=args.approval_threshold,
        all_leads=resolve_ignore_review_approval(args),
        lane_scope=args.review_lane_scope,
        review_slice=args.review_slice,
    )
    return automation_gate.status_payload(gate_args)


def claim_approved_gate(args: argparse.Namespace) -> dict[str, Any]:
    import automation_gate

    return automation_gate.claim(
        argparse.Namespace(
            sheet_url=source_sheet_url(args),
            review_tab=args.review_tab,
            credentials=args.credentials,
            threshold=args.approval_threshold,
            all_leads=resolve_ignore_review_approval(args),
            lane_scope=args.review_lane_scope,
            review_slice=args.review_slice,
        )
    )


def mark_approved_gate(args: argparse.Namespace, fingerprint: str, status: str) -> dict[str, Any]:
    import automation_gate

    return automation_gate.mark(
        argparse.Namespace(
            fingerprint=fingerprint,
            note=f"lead_exec_research {status}",
        ),
        status,
    )


def computation_status_counts(
    computation: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    leads = computation.get("leads", [])
    rows, skipped = computation_rows_for_write(computation, args)
    search_tasks = computation.get("search_tasks", [])
    pending_tasks = [task for task in search_tasks if task.get("status", "pending") == "pending"]
    selected_tasks = [task for task in search_tasks if task.get("status") == "selected"]
    latest_batch = (
        computation.get("search_result_batches", [{}])[-1]
        if computation.get("search_result_batches")
        else {}
    )
    latest_write = computation.get("writes", [{}])[-1] if computation.get("writes") else {}
    p1_linkedin = 0
    p2_linkedin = 0
    for lead in leads:
        row = build_destination_row_from_computation(lead)
        if is_linkedin_profile_url(row.get("P1 LinkedIn", "")):
            p1_linkedin += 1
        if is_linkedin_profile_url(row.get("P2 LinkedIn", "")):
            p2_linkedin += 1
    return {
        "lead_count": len(leads),
        "research_done": len([lead for lead in leads if lead.get("executives")]),
        "research_pending": len(
            [
                lead
                for lead in leads
                if lead.get("status") == "research_pending" and not lead.get("executives")
            ]
        ),
        "reconciliation_pending": len(
            [
                lead
                for lead in leads
                if lead.get("status") == "reconciliation_pending" and lead.get("executives")
            ]
        ),
        "archive_conflicts": len(
            [lead for lead in leads if lead.get("status") == "archive_conflict"]
        ),
        "archive_consumed": len(
            [lead for lead in leads if lead.get("status") == "archive_consumed"]
        ),
        "search_tasks_total": len(search_tasks),
        "search_tasks_pending": len(pending_tasks),
        "search_tasks_selected": len(selected_tasks),
        "search_tasks_remaining": computation.get("search_tasks_remaining", len(pending_tasks)),
        "p1_linkedin": p1_linkedin,
        "p2_linkedin": p2_linkedin,
        "rows_ready_to_write": len(rows),
        "rows_skipped_unresolved": len(skipped),
        "skipped_unresolved": skipped,
        "latest_search_batch": {
            "source_file": latest_batch.get("source_file", ""),
            "status": latest_batch.get("status", ""),
            "task_count": latest_batch.get("task_count", 0),
            "selected": len(
                [row for row in latest_batch.get("results", []) if row.get("status") == "selected"]
            ),
            "needs_review": len(
                [
                    row
                    for row in latest_batch.get("results", [])
                    if row.get("status") == "needs_review"
                ]
            ),
            "errors": len(
                [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
            ),
            "pending_tasks": len(latest_batch.get("pending_tasks", [])),
            "captcha_events": latest_batch.get("captcha_events", 0),
            "hot_profiles": len(latest_batch.get("hot_profiles", [])),
        },
        "latest_write": {
            "created_at": latest_write.get("created_at", ""),
            "rows_written": latest_write.get("rows_written", 0),
            "skipped_unresolved_count": latest_write.get("skipped_unresolved_count", 0),
        },
    }


def next_approved_action(
    gate: dict[str, Any], computation: dict[str, Any] | None, counts: dict[str, Any]
) -> str:
    claim_status = gate.get("claim_status", "")
    if claim_status == "processed":
        return "skip_processed"
    if not gate.get("ready"):
        return "skip_below_threshold"
    if computation is None:
        return "claim_and_freeze" if not claim_status else "freeze_approved"
    if counts.get("research_pending", 0):
        return "research_batch"
    if counts.get("reconciliation_pending", 0) or (
        computation.get("status") == "reconciliation_pending"
        and not computation.get("search_tasks")
    ):
        return "reconcile_existing"
    if counts.get("search_tasks_pending", 0) and counts.get("rows_skipped_unresolved", 0):
        return "run_playwright_search"
    if counts.get("archive_conflicts", 0):
        return "resolve_archive_conflicts"
    if computation.get("publication_mode") == "archive_only":
        if computation.get("status") == "archived_unreviewed":
            return "skip_archived_unreviewed"
        return "archive_unreviewed"
    if not computation.get("writes"):
        return "write_computation"
    if claim_status != "processed":
        return "mark_processed"
    return "skip_processed"


def approved_workflow_status(args: argparse.Namespace) -> dict[str, Any]:
    gate = approved_gate_status(args)
    computation_file = (
        Path(args.computation_file)
        if args.computation_file
        else latest_computation_file_for_fingerprint(gate.get("fingerprint", ""))
    )
    computation: dict[str, Any] | None = None
    counts: dict[str, Any] = {}
    if computation_file and computation_file.exists():
        computation = load_run(computation_file)
        counts = computation_status_counts(computation, args)
    action = next_approved_action(gate, computation, counts)
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "gate": gate,
        "computation_file": str(computation_file) if computation_file else "",
        "computation_status": computation.get("status", "") if computation else "",
        "counts": counts,
        "next_action": action,
        "resumable": action
        in {
            "claim_and_freeze",
            "freeze_approved",
            "research_batch",
            "reconcile_existing",
            "run_playwright_search",
            "archive_unreviewed",
            "write_computation",
            "mark_processed",
        },
    }


def export_pending_search_tasks(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> tuple[Path, list[dict[str, Any]]]:
    tasks = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    tasks = tasks[: args.max_search_tasks]
    export_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_file = SEARCH_TASKS_DIR / f"{export_id}_search_tasks.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "computation_file": str(computation_file),
        "task_count": len(tasks),
        "tasks": tasks,
        "search_tasks": tasks,
    }
    task_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    computation.setdefault("search_task_exports", []).append(
        {
            "created_at": payload["created_at"],
            "file": str(task_file),
            "task_count": len(tasks),
        }
    )
    save_run(computation, computation_file)
    return task_file, tasks


def send_review_email(
    run: dict[str, Any], run_file: Path, review_tab: str, to_email: str
) -> dict[str, Any]:
    if not to_email:
        return {"sent": False, "reason": "No notification email configured."}
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM", user)
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not host or not user or not password or not from_email:
        return {
            "sent": False,
            "reason": "SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM/SMTP_USER are required.",
        }

    subject = f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"
    body = "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
    return {"sent": True, "to": to_email}


def review_email_subject(run: dict[str, Any]) -> str:
    return f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"


def review_email_body(run: dict[str, Any], run_file: Path, review_tab: str) -> str:
    return "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )


def enqueue_review_notification(
    credentials_path: Path,
    sheet_url: str,
    queue_tab: str,
    run: dict[str, Any],
    run_file: Path,
    review_tab: str,
    to_email: str,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_or_create_worksheet(
        spreadsheet, queue_tab, rows=1000, cols=len(NOTIFICATION_QUEUE_COLUMNS)
    )
    headers = worksheet.row_values(1)
    if headers[: len(NOTIFICATION_QUEUE_COLUMNS)] != NOTIFICATION_QUEUE_COLUMNS:
        worksheet.update(
            range_name="A1",
            values=[NOTIFICATION_QUEUE_COLUMNS],
            value_input_option="USER_ENTERED",
        )
        try:
            worksheet.freeze(rows=1)
        except Exception:
            pass
    row = {
        "Created At": datetime.now().isoformat(timespec="seconds"),
        "Status": "Pending",
        "To": to_email,
        "Subject": review_email_subject(run),
        "Body": review_email_body(run, run_file, review_tab),
        "Run ID": run.get("run_id", ""),
        "Run File": str(run_file),
        "Sent At": "",
        "Error": "",
    }
    worksheet.append_row(
        [row.get(header, "") for header in NOTIFICATION_QUEUE_COLUMNS],
        value_input_option="USER_ENTERED",
    )
    return {"queued": True, "tab": queue_tab, "to": to_email}


def notify_review_with_fallback(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    try:
        notification = send_review_email(run, run_file, args.review_tab, args.notify_email)
    except Exception as exc:
        notification = {"sent": False, "reason": str(exc)}
    if not notification.get("sent") and args.queue_notification:
        try:
            queued = enqueue_review_notification(
                Path(args.credentials),
                source_sheet_url(args),
                args.notification_queue_tab,
                run,
                run_file,
                args.review_tab,
                args.notify_email,
            )
            notification["fallback_queue"] = queued
        except Exception as exc:
            notification["fallback_queue"] = {"queued": False, "reason": str(exc)}
    return notification


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    source_headers, source_rows = read_worksheet(
        Path(args.credentials), source_sheet_url(args), args.source_tab
    )
    source_rows = canonicalize_source_rows(source_headers, source_rows, args.source_tab)
    destination_headers, _destination_rows = read_worksheet(
        Path(args.credentials), destination_sheet_url(args), args.destination_tab
    )
    require_columns(destination_headers, DESTINATION_COLUMNS, args.destination_tab)
    groups = group_source_rows(source_rows, args.source_tab)
    return {
        "source_tab": args.source_tab,
        "destination_tab": args.destination_tab,
        "review_tab": args.review_tab,
        "source_headers": source_headers,
        "destination_headers": destination_headers,
        "source_groups": len(groups),
        "first_group": groups[0] if groups else None,
    }


def annotate_overlap_scan(
    lead: dict[str, Any],
    archive: ResearchArchive,
    *,
    overlap_scan_enabled: bool,
) -> dict[str, Any]:
    match = (
        archive.match(lead)
        if overlap_scan_enabled
        else {
            "status": MATCH_FRESH,
            "archive_entry_id": "",
            "matched_on": [],
            "confidence": "disabled",
        }
    )
    lead["archive_match"] = match
    lead["overlap_status"] = match.get("status", MATCH_FRESH)
    return match


def collect_overlap_top_up_waves(
    initial_shortfall: int,
    collect_wave: Callable[[str, int], list[dict[str, Any]]],
    *,
    enabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    waves: list[dict[str, Any]] = []
    shortfall = max(0, int(initial_shortfall))
    wave_number = 1
    if not enabled:
        return selected, waves, shortfall

    while shortfall > 0:
        label = f"Top-up {wave_number}"
        requested = shortfall
        wave = collect_wave(label, requested)
        if not wave:
            break
        selected.extend(wave)
        archive_matches = [
            lead for lead in wave if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
        ]
        conflicts = [
            lead for lead in wave if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT
        ]
        waves.append(
            {
                "label": label,
                "requested": requested,
                "selected": len(wave),
                "archive_matches": len(archive_matches),
                "conflicts": len(conflicts),
            }
        )
        shortfall = len(archive_matches) + len(conflicts)
        wave_number += 1
        if len(wave) < requested:
            break

    return selected, waves, shortfall


def build_run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    headers, rows = read_worksheet(Path(args.credentials), source_sheet_url(args), args.source_tab)
    rows = canonicalize_source_rows(headers, rows, args.source_tab)
    destination_company_by_id = load_destination_company_by_id(
        Path(args.credentials), destination_sheet_url(args), args.destination_tab
    )
    reviewed_ids = load_reviewed_ids(
        Path(args.credentials), source_sheet_url(args), args.review_tab
    )
    groups = group_source_rows(rows, args.source_tab)
    archive = ResearchArchive(archive_index_path(args))
    archive_stats = archive.stats()
    overlap_scan_enabled = (
        not getattr(args, "disable_overlap_scan", False) and archive_stats.get("available", 0) > 0
    )
    overlap_top_ups_enabled = overlap_scan_enabled and not getattr(
        args, "disable_overlap_top_ups", False
    )
    selected: list[dict[str, Any]] = []
    prep_waves: list[dict[str, Any]] = []
    destination_conflicts = []
    skipped_destination_ids = set()
    skipped_reviewed_ids = set()
    skipped_consumed_archive_ids = set()
    group_index = 0

    def next_eligible_group() -> dict[str, Any] | None:
        nonlocal group_index
        while group_index < len(groups):
            group = groups[group_index]
            group_index += 1
            lead_id = group.get("id", "")
            if lead_id in reviewed_ids:
                skipped_reviewed_ids.add(lead_id)
                continue
            source_company = group.get("company", {}).get("name", "")
            destination_company = destination_company_by_id.get(lead_id)
            if destination_company:
                if normalize_key(destination_company) == normalize_key(source_company):
                    skipped_destination_ids.add(lead_id)
                    continue
                destination_conflicts.append(
                    {
                        "id": lead_id,
                        "source_company": source_company,
                        "destination_company": destination_company,
                    }
                )
            match = annotate_overlap_scan(
                group,
                archive,
                overlap_scan_enabled=overlap_scan_enabled,
            )
            if match.get("status") == MATCH_CONSUMED:
                skipped_consumed_archive_ids.add(lead_id)
                continue
            return group
        return None

    def collect_wave(label: str, target: int) -> list[dict[str, Any]]:
        wave = []
        while len(wave) < target:
            group = next_eligible_group()
            if group is None:
                break
            group["prep_wave"] = label
            wave.append(group)
        return wave

    base_wave = collect_wave("Base", args.limit)
    selected.extend(base_wave)
    base_nonfresh = [
        lead
        for lead in base_wave
        if lead.get("archive_match", {}).get("status") in {MATCH_AVAILABLE, MATCH_CONFLICT}
    ]
    prep_waves.append(
        {
            "label": "Base",
            "requested": args.limit,
            "selected": len(base_wave),
            "archive_matches": len(
                [
                    lead
                    for lead in base_wave
                    if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
                ]
            ),
            "conflicts": len(
                [
                    lead
                    for lead in base_wave
                    if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT
                ]
            ),
        }
    )

    top_up_rows, top_up_waves, unresolved_shortfall = collect_overlap_top_up_waves(
        len(base_nonfresh),
        collect_wave,
        enabled=overlap_top_ups_enabled,
    )
    selected.extend(top_up_rows)
    prep_waves.extend(top_up_waves)

    fresh_count = len(
        [lead for lead in selected if lead.get("archive_match", {}).get("status") == MATCH_FRESH]
    )
    archive_match_count = len(
        [
            lead
            for lead in selected
            if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
        ]
    )
    archive_conflict_count = len(
        [lead for lead in selected if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT]
    )
    lane_counts = assign_primary_lanes(selected)
    run_id = now_run_id()
    run_file = RUNS_DIR / f"{run_id}.json"
    run = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pulled",
        "config": {
            "source_sheet_url": source_sheet_url(args),
            "destination_sheet_url": destination_sheet_url(args),
            "source_tab": args.source_tab,
            "destination_tab": args.destination_tab,
            "limit": args.limit,
            "batch_size": args.batch_size,
            "dry_run": args.dry_run,
            "archive_index": str(archive_index_path(args)),
            "overlap_scan_enabled": overlap_scan_enabled,
            "overlap_top_ups_enabled": overlap_top_ups_enabled,
        },
        "source": {
            "total_groups": len(groups),
            "reviewed_ids_seen": len(reviewed_ids),
            "reviewed_ids_skipped": len(skipped_reviewed_ids),
            "destination_ids_seen": len(destination_company_by_id),
            "destination_ids_skipped": len(skipped_destination_ids),
            "destination_id_conflicts": destination_conflicts,
            "consumed_archive_ids_skipped": len(skipped_consumed_archive_ids),
            "base_selected_count": len(base_wave),
            "selected_count": len(selected),
            "lane_counts": lane_counts,
        },
        "overlap_scan": {
            "phase": "pre_review_detection",
            "detection_only": True,
            "archive_reuse_applied": False,
            "archive_entries_consumed": 0,
            "enabled": overlap_scan_enabled,
            "automatic": not getattr(args, "disable_overlap_scan", False),
            "top_ups_enabled": overlap_top_ups_enabled,
            "top_ups_automatic": not getattr(args, "disable_overlap_top_ups", False),
            "archive": archive_stats,
            "fresh_target": args.limit,
            "fresh_count": fresh_count,
            "archive_match_count": archive_match_count,
            "conflict_count": archive_conflict_count,
            "top_up_count": max(0, len(selected) - len(base_wave)),
            "top_up_suppressed_count": (
                len(base_nonfresh) if overlap_scan_enabled and not overlap_top_ups_enabled else 0
            ),
            "unresolved_fresh_shortfall": unresolved_shortfall,
            "target_met": fresh_count >= args.limit,
            "settled_for_day": fresh_count >= args.limit or not overlap_top_ups_enabled,
            "source_exhausted": group_index >= len(groups) and fresh_count < args.limit,
            "waves": prep_waves,
        },
        "batches": [
            {"index": index + 1, "lead_ids": [lead["id"] for lead in batch], "status": "pending"}
            for index, batch in enumerate(chunks(selected, args.batch_size))
        ],
        "leads": selected,
        "destination_rows": [],
        "errors": [],
    }
    save_run(run, run_file)
    return run, run_file


def prepare_review(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    existing_group_row = find_successful_review_group_row_for_today(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
    )
    if existing_group_row is not None:
        run_id = now_run_id()
        run_file = RUNS_DIR / f"{run_id}.json"
        run = {
            "run_id": run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "review_skipped_already_prepared_today",
            "config": {
                "source_sheet_url": source_sheet_url(args),
                "destination_sheet_url": destination_sheet_url(args),
                "source_tab": args.source_tab,
                "destination_tab": args.destination_tab,
                "limit": args.limit,
                "batch_size": args.batch_size,
                "dry_run": args.dry_run,
            },
            "source": {"selected_count": 0},
            "batches": [],
            "leads": [],
            "destination_rows": [],
            "errors": [],
            "review": {
                "tab": args.review_tab,
                "rows_written": 0,
                "write": {
                    "date": review_group_date_value(),
                    "existing_group_row": existing_group_row,
                    "skipped": True,
                },
                "notification": {
                    "sent": False,
                    "reason": f"Review queue already prepared today at group row {existing_group_row}.",
                },
            },
        }
        save_run(run, run_file)
        return run, run_file

    run, run_file = build_run(args)
    if args.dry_run:
        run["status"] = "review_dry_run"
        run["review"] = {
            "tab": args.review_tab,
            "rows_written": 0,
            "notification": {"sent": False, "reason": "Dry run."},
        }
        save_run(run, run_file)
        return run, run_file

    rows_written = write_review_rows(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
        run,
        run_file,
    )
    notification = {"sent": False, "reason": "Email notification disabled."}
    if not args.no_email:
        notification = notify_review_with_fallback(run, run_file, args)

    run["status"] = "awaiting_review"
    run["review"] = {
        "tab": args.review_tab,
        "rows_written": rows_written,
        "write": run.get("review_write", {}),
        "notification": notification,
    }
    save_run(run, run_file)
    return run, run_file


def notify_review(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> dict[str, Any]:
    notification = notify_review_with_fallback(run, run_file, args)
    run.setdefault("review", {})
    run["review"]["tab"] = run["review"].get("tab") or args.review_tab
    run["review"]["notification"] = notification
    save_run(run, run_file)
    return run


def filter_run_to_approved(run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    rows = read_review_rows(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run.get("run_id", "")
    )
    approved_ids = {
        clean_text(row.get("Run ID"))
        for row in rows
        if (ignore_review_approval or checkbox_truthy(row.get("Approved")))
        and clean_text(row.get("Run ID"))
    }
    use_by_id = {
        clean_text(row.get("Run ID")): clean_text(row.get("Use"))
        for row in rows
        if clean_text(row.get("Run ID"))
    }
    lead_by_id = {lead.get("id"): lead for lead in run.get("leads", [])}
    approved_leads = [lead_by_id[lead_id] for lead_id in approved_ids if lead_id in lead_by_id]
    approved_leads.sort(key=lambda lead: lead.get("source_rows", {}).get("company_row") or 0)
    if not approved_leads:
        selected_label = "leads" if ignore_review_approval else "approved leads"
        raise ValueError(
            f"No {selected_label} found in {args.review_tab} for run {run.get('run_id', '')}."
        )

    for lead in approved_leads:
        lead["review_use"] = use_by_id.get(lead.get("id", ""), "")
        if lead.get("status") != "completed":
            lead["status"] = "pending"

    run["leads"] = approved_leads
    run["batches"] = [
        {"index": index + 1, "lead_ids": [lead["id"] for lead in batch], "status": "pending"}
        for index, batch in enumerate(chunks(approved_leads, args.batch_size))
    ]
    run["source"]["approved_count"] = len(approved_leads)
    run["source"]["selection_mode"] = (
        "approval_disabled" if ignore_review_approval else "approved_only"
    )
    run["status"] = "approved_for_processing"
    return run


def approved_leads_from_review(
    run: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    rows = read_review_rows(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run.get("run_id", "")
    )
    approved_rows = [
        row
        for row in rows
        if (ignore_review_approval or checkbox_truthy(row.get("Approved")))
        and clean_text(row.get("Run ID"))
    ]
    lane_scope = clean_text(getattr(args, "review_lane_scope", "all")).lower()
    if lane_scope in {"design", "automation"}:
        approved_rows = [
            row
            for row in approved_rows
            if clean_text(row.get("Primary Lane")).lower() == lane_scope
        ]
    approved_rows = apply_review_slice(approved_rows, getattr(args, "review_slice", ""))
    use_by_id = {clean_text(row.get("Run ID")): clean_text(row.get("Use")) for row in approved_rows}
    approved_by_id = {
        clean_text(row.get("Run ID")): checkbox_truthy(row.get("Approved")) for row in approved_rows
    }
    review_row_by_id = {
        clean_text(row.get("Run ID")): row.get("_row_number") for row in approved_rows
    }
    lead_by_id = {lead.get("id"): lead for lead in run.get("leads", [])}
    approved = []
    for row in approved_rows:
        lead_id = clean_text(row.get("Run ID"))
        lead = lead_by_id.get(lead_id)
        if not lead:
            continue
        frozen = json.loads(json.dumps(lead))
        frozen["review_use"] = use_by_id.get(lead_id, "")
        frozen["review_approved"] = approved_by_id.get(lead_id, False)
        frozen["review_row_number"] = review_row_by_id.get(lead_id)
        approved.append(frozen)
    approved.sort(key=lambda lead: lead.get("source_rows", {}).get("company_row") or 0)
    return approved


def freeze_approved(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    approved = approved_leads_from_review(run, args)
    if not approved:
        selected_label = "leads" if ignore_review_approval else "approved leads"
        raise ValueError(f"No {selected_label} found in {args.review_tab}.")

    snapshot_id = now_run_id()
    snapshot_file = SNAPSHOTS_DIR / f"{snapshot_id}_snapshot.json"
    computation_file = COMPUTATIONS_DIR / f"{snapshot_id}_computation.json"

    snapshot = {
        "snapshot_id": snapshot_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_id": run.get("run_id", ""),
        "source_run_file": str(run_file),
        "approved_count": len(approved),
        "selection_mode": "approval_disabled" if ignore_review_approval else "approved_only",
        "review_lane_scope": args.review_lane_scope,
        "review_slice": args.review_slice,
        "leads": approved,
    }

    archive = ResearchArchive(archive_index_path(args))
    computation_leads = []
    archive_reused_count = 0
    for lead in approved:
        computation_lead = {
            "lead_id": lead.get("id", ""),
            "company": lead.get("company", {}).get("name", ""),
            "website": lead.get("company", {}).get("website", ""),
            "company_linkedin": lead.get("company", {}).get("linkedin", ""),
            "emp_count": lead.get("company", {}).get("employee_count")
            or len(lead.get("employees_from_sheet", [])),
            "source_tab": lead.get("source_tab", ""),
            "use": lead.get("review_use", ""),
            "review_approved": bool(lead.get("review_approved")),
            "review_row_number": lead.get("review_row_number"),
            "primary_lane": lead.get("primary_lane", ""),
            "prep_wave": lead.get("prep_wave", "Base"),
            "source_rows": lead.get("source_rows", {}),
            "employees_from_sheet": lead.get("employees_from_sheet", []),
            "executives": [],
            "search_tasks": [],
            "search_results": [],
            "destination_row": {},
            "status": "research_pending",
            "notes": [],
        }
        match = lead.get("archive_match") or archive.match(lead)
        computation_lead["archive_match"] = match
        if match.get("status") == MATCH_AVAILABLE and match.get("archive_entry_id"):
            entry = archive.get(match["archive_entry_id"])
            if entry:
                computation_lead = hydrate_computation_lead(computation_lead, entry)
                computation_lead["archive_match"] = match
                archive_reused_count += 1
        elif match.get("status") == MATCH_CONFLICT:
            computation_lead["status"] = "archive_conflict"
            computation_lead["notes"].append(
                "Archive identity conflict requires manual resolution."
            )
        elif match.get("status") == MATCH_CONSUMED:
            computation_lead["status"] = "archive_consumed"
            computation_lead["notes"].append(
                "Archive research was already consumed by an earlier publication."
            )
        computation_leads.append(computation_lead)

    selection_mode = "approval_disabled" if ignore_review_approval else "approved_only"
    computation = {
        "computation_id": snapshot_id,
        "created_at": snapshot["created_at"],
        "snapshot_file": str(snapshot_file),
        "source_run_file": str(run_file),
        "approved_fingerprint": lead_id_fingerprint([lead.get("id", "") for lead in approved]),
        "selection_mode": selection_mode,
        "review_lane_scope": args.review_lane_scope,
        "review_slice": args.review_slice,
        "publication_mode": "publish_all"
        if selection_mode == "approval_disabled"
        else "publish_approved",
        "status": "research_pending"
        if archive_reused_count < len(computation_leads)
        else "archive_reused",
        "lead_count": len(computation_leads),
        "archive_reused_count": archive_reused_count,
        "fresh_research_count": len(computation_leads) - archive_reused_count,
        "post_review_reconciliation": {
            "owner_flow": "process_approved_leads",
            "selection_mode": selection_mode,
            "review_lane_scope": args.review_lane_scope,
            "review_slice": args.review_slice,
            "archive_reuse_applied": archive_reused_count,
            "archive_entries_consumed": 0,
        },
        "leads": computation_leads,
        "errors": [],
    }

    save_run(snapshot, snapshot_file)
    save_run(computation, computation_file)
    return snapshot, snapshot_file, computation, computation_file


def build_research_prompt(
    computation: dict[str, Any], args: argparse.Namespace
) -> tuple[str, list[dict[str, Any]]]:
    pending = [
        lead
        for lead in computation.get("leads", [])
        if lead.get("status") == "research_pending" and not lead.get("executives")
    ]
    batch = pending[: args.research_batch_size]
    lines = [
        "For each company below, find the names of the top 3 executives or most senior team members.",
        "",
        "Prioritize founders, owners, CEOs, managing directors, partners, directors, and other clear senior decision makers.",
        "Do not choose generic visible employees just because they are easy to find if a more senior founder/owner/executive is publicly identifiable.",
        "LinkedIn is still important: include the LinkedIn URL when publicly available, but do not invent or guess a URL.",
        "",
        "Return one table with these columns:",
        "- Company",
        "- Names",
        "- Titles",
        "- Linkedin url if publicly available",
        "",
        "Only include people who are clearly connected to the company.",
        "",
        "Companies:",
    ]
    for index, lead in enumerate(batch, start=1):
        lines.append(f"{index}. {lead.get('company', '')} - {lead.get('website', '')}")
    return "\n".join(lines), batch


def write_research_prompt(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> Path:
    prompt, batch = build_research_prompt(computation, args)
    prompt_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompt_file = PROMPTS_DIR / f"{prompt_id}_research_prompt.md"
    payload = [
        f"# Research Prompt {prompt_id}",
        "",
        f"Computation file: `{computation_file}`",
        "",
        prompt,
        "",
        "Lead IDs in this batch:",
    ]
    for lead in batch:
        payload.append(f"- {lead.get('lead_id', '')}: {lead.get('company', '')}")
    prompt_file.write_text("\n".join(payload) + "\n")
    return prompt_file


def normalize_research_executives(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("executives", [])
    if not isinstance(raw, list):
        return []
    executives = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = clean_text(
            item.get("name") or item.get("Name") or item.get("names") or item.get("Names")
        )
        title = clean_text(
            item.get("title") or item.get("Title") or item.get("titles") or item.get("Titles")
        )
        linkedin = normalize_url(
            item.get("linkedin_url")
            or item.get("linkedin")
            or item.get("Linkedin url if publicly available")
            or item.get("LinkedIn")
            or ""
        )
        if not name:
            continue
        executives.append(
            {
                "name": name,
                "title": title,
                "linkedin_url": linkedin,
                "linkedin_source": "research" if linkedin else "",
                "email": "",
                "research_source": "codex",
                "needs_linkedin_search": not bool(linkedin),
                "reconciliation_notes": [],
            }
        )
    return executives[:3]


def apply_research_results(computation: dict[str, Any], research_file: Path) -> dict[str, Any]:
    data = json.loads(research_file.read_text())
    if isinstance(data, dict) and "results" in data:
        rows = data["results"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("Research file must be a list or an object with a 'results' list.")

    lead_by_company = {
        normalize_key(lead.get("company", "")): lead for lead in computation.get("leads", [])
    }
    applied = 0
    unmatched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = clean_text(row.get("company") or row.get("Company"))
        lead = lead_by_company.get(normalize_key(company))
        if not lead:
            unmatched.append(company)
            continue
        executives = normalize_research_executives(row)
        lead["executives"] = executives
        lead["status"] = "reconciliation_pending"
        applied += 1

    computation["status"] = "reconciliation_pending"
    computation.setdefault("research_imports", []).append(
        {
            "file": str(research_file),
            "applied": applied,
            "unmatched_companies": unmatched,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return computation


def reconcile_computation_existing_data(computation: dict[str, Any]) -> dict[str, Any]:
    all_search_tasks = []
    for lead in computation.get("leads", []):
        executives = lead.get("executives", [])
        if not executives:
            continue
        employees = lead.get("employees_from_sheet", [])
        lead_tasks = []
        for executive in executives:
            if executive.get("linkedin_url"):
                executive["needs_linkedin_search"] = False
                continue

            matched = match_employee({"name": executive.get("name", "")}, employees)
            if matched:
                executive["email"] = matched.get("email", "") or executive.get("email", "")
                if not executive.get("title") and matched.get("role"):
                    executive["title"] = matched.get("role", "")
                score = linkedin_url_name_score(
                    executive.get("name", ""), matched.get("linkedin", "")
                )
                if matched.get("linkedin") and score >= 0.34:
                    executive["linkedin_url"] = matched.get("linkedin", "")
                    executive["linkedin_source"] = "source_employee_row"
                    executive["needs_linkedin_search"] = False
                    executive.setdefault("reconciliation_notes", []).append(
                        "LinkedIn matched on same employee row."
                    )
                    continue
                executive.setdefault("reconciliation_notes", []).append(
                    "Name matched source employee row."
                )

            best_url = ""
            best_score = 0.0
            for employee in employees:
                url = employee.get("linkedin", "")
                score = linkedin_url_name_score(executive.get("name", ""), url)
                if url and score > best_score:
                    best_url = url
                    best_score = score
            if best_url and best_score >= 0.34:
                executive["linkedin_url"] = best_url
                executive["linkedin_source"] = "source_group_url_match"
                executive["needs_linkedin_search"] = False
                executive.setdefault("reconciliation_notes", []).append(
                    "LinkedIn matched by name parts across source group URLs."
                )
                continue

            task = {
                "lead_id": lead.get("lead_id", ""),
                "company": lead.get("company", ""),
                "person_name": executive.get("name", ""),
                "title": executive.get("title", ""),
                "query": search_query_variants(executive.get("name", ""), lead.get("company", ""))[
                    0
                ],
                "queries": search_query_variants(
                    executive.get("name", ""), lead.get("company", "")
                ),
                "status": "pending",
            }
            executive["needs_linkedin_search"] = True
            executive["search_task_query"] = task["query"]
            lead_tasks.append(task)
            all_search_tasks.append(task)
        lead["search_tasks"] = lead_tasks
        lead["status"] = "linkedin_search_pending" if lead_tasks else "reconciled"

    computation["search_tasks"] = all_search_tasks
    computation["status"] = "linkedin_search_pending" if all_search_tasks else "reconciled"
    computation["reconciled_at"] = datetime.now().isoformat(timespec="seconds")
    return computation


def score_task_candidate(task: dict[str, Any], result: dict[str, str]) -> float:
    exec_item = {"name": task.get("person_name", ""), "role": task.get("title", "")}
    company = {"name": task.get("company", "")}
    return score_search_candidate(exec_item, company, result)


def find_executive_for_task(
    computation: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any] | None:
    for lead in computation.get("leads", []):
        if lead.get("lead_id") != task.get("lead_id"):
            continue
        for executive in lead.get("executives", []):
            if normalize_key(executive.get("name", "")) == normalize_key(
                task.get("person_name", "")
            ):
                return executive
    return None


def run_search_tasks(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], Path]:
    tasks = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    tasks = tasks[: args.max_search_tasks]
    search_client = SearchClient(delay_min=args.delay_min, delay_max=args.delay_max)
    result_batch = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "computation_file": str(computation_file),
        "task_count": len(tasks),
        "results": [],
    }

    for task in tasks:
        item = dict(task)
        try:
            queries = task.get("queries") or [task.get("query", "")]
            results = []
            best_scored = []
            search_errors = []
            for query in queries:
                try:
                    results = search_client.search(query, limit=5)
                except Exception as exc:
                    search_errors.append(f"{query}: {exc}")
                    continue
                if results:
                    item["query_used"] = query
                scored_for_query = []
                for result in results:
                    scored_item = dict(result)
                    scored_item.setdefault("query", query)
                    scored_item["score"] = score_task_candidate(task, result)
                    scored_for_query.append(scored_item)
                scored_for_query.sort(key=lambda row: row.get("score", 0), reverse=True)
                if scored_for_query and not best_scored:
                    best_scored = scored_for_query
                if (
                    scored_for_query
                    and scored_for_query[0].get("score", 0) >= args.search_accept_threshold
                ):
                    best_scored = scored_for_query
                    break
            if not results and search_errors:
                raise RuntimeError("; ".join(search_errors))
            scored = best_scored
            selected = (
                scored[0]
                if scored and scored[0].get("score", 0) >= args.search_accept_threshold
                else None
            )
            item["results"] = scored
            item["selected"] = selected
            item["status"] = "selected" if selected else "needs_review"
            if selected:
                executive = find_executive_for_task(computation, task)
                if executive is not None:
                    executive["linkedin_url"] = normalize_url(selected.get("url", ""))
                    executive["linkedin_source"] = "search_task"
                    executive["needs_linkedin_search"] = False
                    executive.setdefault("reconciliation_notes", []).append(
                        "LinkedIn selected from search task results."
                    )
        except Exception as exc:
            item["results"] = []
            item["selected"] = None
            item["status"] = "error"
            item["error"] = str(exc)
        result_batch["results"].append(item)

    completed_status_by_key = {
        (row.get("lead_id"), row.get("person_name")): row.get("status", "needs_review")
        for row in result_batch["results"]
    }
    for task in computation.get("search_tasks", []):
        key = (task.get("lead_id"), task.get("person_name"))
        if key in completed_status_by_key:
            task["status"] = completed_status_by_key[key]

    remaining_pending = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    computation.setdefault("search_result_batches", []).append(result_batch)
    computation["status"] = (
        "linkedin_search_pending" if remaining_pending else "search_tasks_completed"
    )
    computation["search_tasks_remaining"] = len(remaining_pending)
    result_file = (
        SEARCH_RESULTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_search_results.json"
    )
    result_file.write_text(json.dumps(result_batch, indent=2, ensure_ascii=False) + "\n")
    save_run(computation, computation_file)
    return computation, result_file


def apply_search_result_batch(
    computation: dict[str, Any], result_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    batch = json.loads(result_file.read_text())
    normalized_batch = {
        "created_at": batch.get("created_at", datetime.now().isoformat(timespec="seconds")),
        "source_file": str(result_file),
        "status": batch.get("status", ""),
        "task_count": batch.get("task_count", len(batch.get("results", []))),
        "completed_tasks": batch.get("completed_tasks", len(batch.get("results", []))),
        "remaining_tasks": batch.get("remaining_tasks", len(batch.get("pending_tasks", []))),
        "pending_tasks": batch.get("pending_tasks", []),
        "captcha_events": batch.get("captcha_events", 0),
        "hot_profiles": batch.get("hot_profiles", []),
        "profile_transfers": batch.get("profile_transfers", []),
        "results": [],
    }
    completed_status_by_key = {}
    for item in batch.get("results", []):
        scored = []
        for result in item.get("results", []):
            scored_item = dict(result)
            scored_item["score"] = score_task_candidate(item, result)
            scored.append(scored_item)
        scored.sort(key=lambda row: row.get("score", 0), reverse=True)
        selected = (
            scored[0]
            if scored and scored[0].get("score", 0) >= args.search_accept_threshold
            else None
        )
        out = dict(item)
        out["results"] = scored
        out["selected"] = selected
        source_status = item.get("status", "")
        if selected:
            out["status"] = "selected"
        elif source_status in {"error", "captcha", "paused", "paused_due_to_all_profiles_hot"}:
            out["status"] = source_status
        else:
            out["status"] = "needs_review"
        normalized_batch["results"].append(out)
        completed_status_by_key[(item.get("lead_id"), item.get("person_name"))] = out["status"]
        if selected:
            executive = find_executive_for_task(computation, item)
            if executive is not None:
                executive["linkedin_url"] = normalize_url(selected.get("url", ""))
                executive["linkedin_source"] = "playwright_search_task"
                executive["needs_linkedin_search"] = False
                executive.setdefault("reconciliation_notes", []).append(
                    "LinkedIn selected from Playwright search task results."
                )

    for task in computation.get("search_tasks", []):
        key = (task.get("lead_id"), task.get("person_name"))
        if key in completed_status_by_key:
            task["status"] = completed_status_by_key[key]

    remaining_pending = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    computation.setdefault("search_result_batches", []).append(normalized_batch)
    computation["search_tasks_remaining"] = len(remaining_pending)
    if normalized_batch.get("status") == "paused_due_to_all_profiles_hot" or normalized_batch.get(
        "pending_tasks"
    ):
        computation["status"] = "playwright_search_paused"
    else:
        computation["status"] = (
            "linkedin_search_pending" if remaining_pending else "search_tasks_completed"
        )
    return computation


def process_run(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> dict[str, Any]:
    search_client = SearchClient(delay_min=args.delay_min, delay_max=args.delay_max)
    lead_by_id = {lead["id"]: lead for lead in run.get("leads", [])}
    for batch in run.get("batches", []):
        if batch.get("status") == "completed":
            continue
        batch["status"] = "in_progress"
        save_run(run, run_file)
        for lead_id in batch.get("lead_ids", []):
            group = lead_by_id[lead_id]
            if group.get("status") == "completed":
                continue
            try:
                execs = search_execs_for_company(search_client, group["company"], max_execs=3)
                if not execs:
                    execs = fallback_execs_from_employees(
                        group.get("employees_from_sheet", []), max_execs=3
                    )
                group["execs_found"] = execs
                group["finalized_execs"] = [
                    reconcile_exec(search_client, group, exec_item) for exec_item in execs
                ]
                group["destination_row"] = build_destination_row(group)
                group["status"] = "completed"
            except Exception as exc:
                group["status"] = "error"
                group.setdefault("errors", []).append(str(exc))
                run.setdefault("errors", []).append({"lead_id": lead_id, "error": str(exc)})
            save_run(run, run_file)
        batch["status"] = "completed"
        save_run(run, run_file)

    run["destination_rows"] = [
        lead.get("destination_row", {})
        for lead in run.get("leads", [])
        if lead.get("status") == "completed" and lead.get("destination_row")
    ]
    run["status"] = "processed"
    save_run(run, run_file)
    return run


def process_approved_run(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    run = filter_run_to_approved(run, args)
    save_run(run, run_file)
    status_by_id = {lead.get("id", ""): "Processing" for lead in run.get("leads", [])}
    update_review_statuses(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run["run_id"], status_by_id
    )
    run = process_run(run, run_file, args)
    final_status = {
        lead.get("id", ""): ("Processed" if lead.get("status") == "completed" else "Error")
        for lead in run.get("leads", [])
    }
    update_review_statuses(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run["run_id"], final_status
    )
    return run


def push_run(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> int:
    if args.dry_run:
        run["rows_written"] = 0
        run["status"] = "dry_run_completed"
        save_run(run, run_file)
        return 0
    rows_written = write_destination_rows(
        Path(args.credentials),
        destination_sheet_url(args),
        args.destination_tab,
        run.get("destination_rows", []),
    )
    run["rows_written"] = rows_written
    run["status"] = "completed"
    save_run(run, run_file)
    return rows_written


def print_summary(run: dict[str, Any], run_file: Path, rows_written: int, dry_run: bool) -> None:
    print("Lead exec research summary")
    print(f"Run file: {run_file}")
    print(
        f"Selected leads: {run.get('source', {}).get('selected_count', len(run.get('leads', [])))}"
    )
    if "approved_count" in run.get("source", {}):
        print(f"Approved leads: {run.get('source', {}).get('approved_count', 0)}")
    print(f"Batches: {len(run.get('batches', []))}")
    if run.get("review"):
        print(f"Review tab: {run['review'].get('tab', '')}")
        print(f"Review rows written: {run['review'].get('rows_written', 0)}")
        if run["review"].get("write"):
            write = run["review"].get("write", {})
            print(f"Review group date: {write.get('date', '')}")
            print(f"Review group row: {write.get('group_row', '')}")
        notification = run["review"].get("notification", {})
        print(f"Email notification: {notification.get('sent', False)}")
        if notification.get("reason"):
            print(f"Email note: {notification.get('reason')}")
    print(f"Destination rows ready: {len(run.get('destination_rows', []))}")
    print(f"Rows written: {rows_written}")
    print(f"Dry run: {dry_run}")
    print(f"Errors: {len(run.get('errors', []))}")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sheet-url",
        default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_SHEET_URL),
        help="Google Sheet URL when source and destination are the same.",
    )
    parser.add_argument(
        "--source-sheet-url",
        default=os.environ.get("LEAD_RESEARCH_SOURCE_SHEET_URL", ""),
        help="Source Google Sheet URL.",
    )
    parser.add_argument(
        "--destination-sheet-url",
        default=os.environ.get("LEAD_RESEARCH_DESTINATION_SHEET_URL", ""),
        help="Destination Google Sheet URL.",
    )
    parser.add_argument(
        "--source-tab", default=os.environ.get("LEAD_RESEARCH_SOURCE_TAB", DEFAULT_SOURCE_TAB)
    )
    parser.add_argument(
        "--destination-tab",
        default=os.environ.get("LEAD_RESEARCH_DESTINATION_TAB", DEFAULT_DESTINATION_TAB),
    )
    parser.add_argument(
        "--final-tab", default=os.environ.get("LEAD_RESEARCH_FINAL_TAB", DEFAULT_FINAL_TAB)
    )
    parser.add_argument(
        "--prospects-sheet-url", default=os.environ.get("OBF_SHEET_URL", DEFAULT_OBF_SHEET_URL)
    )
    parser.add_argument(
        "--prospects-tab", default=os.environ.get("OBF_PROSPECTS_TAB", DEFAULT_PROSPECTS_TAB)
    )
    parser.add_argument("--target-date", default=os.environ.get("BRIDGE_TARGET_DATE", "today"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--review-tab", default=os.environ.get("LEAD_RESEARCH_REVIEW_TAB", DEFAULT_REVIEW_TAB)
    )
    parser.add_argument(
        "--notification-queue-tab",
        default=os.environ.get(
            "LEAD_RESEARCH_NOTIFICATION_QUEUE_TAB", DEFAULT_NOTIFICATION_QUEUE_TAB
        ),
    )
    parser.add_argument(
        "--notify-email",
        default=os.environ.get(
            "LEAD_RESEARCH_NOTIFY_EMAIL", os.environ.get("NOTIFY_TO", DEFAULT_NOTIFY_EMAIL)
        ),
    )
    parser.add_argument(
        "--no-email", action="store_true", help="Skip review-ready email notification."
    )
    parser.add_argument(
        "--no-queue-notification",
        dest="queue_notification",
        action="store_false",
        default=True,
        help="Do not write to the Notification Queue tab when SMTP fails.",
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument(
        "--archive-index",
        default=os.environ.get("LEAD_RESEARCH_ARCHIVE_INDEX", str(DEFAULT_RESEARCH_ARCHIVE_INDEX)),
        help="Local reusable research archive index.",
    )
    parser.add_argument(
        "--disable-overlap-scan",
        "--disable-archive-reconciliation",
        dest="disable_overlap_scan",
        action="store_true",
        default=os.environ.get(
            "LEAD_OVERLAP_SCAN_ENABLED",
            os.environ.get("LEAD_ARCHIVE_RECONCILIATION_ENABLED", "1"),
        )
        .strip()
        .lower()
        in {"0", "false", "no", "off"},
        help="Pause the detection-only archive overlap scan.",
    )
    parser.add_argument(
        "--disable-overlap-top-ups",
        dest="disable_overlap_top_ups",
        action="store_true",
        default=os.environ.get("LEAD_OVERLAP_TOP_UPS_ENABLED", "1").strip().lower()
        in {"0", "false", "no", "off"},
        help="Keep the overlap scan but stop after the single base review batch.",
    )
    parser.add_argument(
        "--archive-reason",
        default="unreviewed_processing",
        help="Audit reason recorded when archiving a computation.",
    )
    parser.add_argument("--delay-min", type=float, default=2.0)
    parser.add_argument("--delay-max", type=float, default=5.0)
    parser.add_argument("--snapshot-file", default="")
    parser.add_argument("--computation-file", default="")
    parser.add_argument("--research-file", default="")
    parser.add_argument("--search-result-file", default="")
    parser.add_argument("--research-batch-size", type=int, default=5)
    parser.add_argument("--max-search-tasks", type=int, default=50)
    parser.add_argument("--search-accept-threshold", type=float, default=0.45)
    parser.add_argument("--approval-threshold", type=int, default=20)
    approval_mode = parser.add_mutually_exclusive_group()
    approval_mode.add_argument(
        "--ignore-review-approval",
        dest="ignore_review_approval",
        action="store_true",
        default=None,
        help="Process every Lead Review row, overriding the shared approval-gate setting.",
    )
    approval_mode.add_argument(
        "--require-review-approval",
        dest="ignore_review_approval",
        action="store_false",
        help="Process approved Lead Review rows only, overriding the shared approval-gate setting.",
    )
    parser.add_argument(
        "--review-lane-scope",
        choices=("all", "design", "automation"),
        default="all",
        help="Limit Lead Review status/freeze selection to a primary lane.",
    )
    parser.add_argument(
        "--review-slice",
        default="",
        help="Process a stable slice of selected Lead Review rows as N/M, for example 1/3.",
    )
    parser.add_argument("--write-limit", type=int, default=0)
    parser.add_argument("--write-start-row", type=int, default=2)
    parser.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Allow write-computation to write rows that fail readiness checks.",
    )
    parser.add_argument(
        "--require-p2",
        action="store_true",
        help="Require P2 name and LinkedIn before writing/bridging rows.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-file", default="")


def validate_args(args: argparse.Namespace) -> None:
    if not source_sheet_url(args):
        raise SystemExit("Missing --source-sheet-url or --sheet-url.")
    if not destination_sheet_url(args):
        raise SystemExit("Missing --destination-sheet-url or --sheet-url.")
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1.")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1.")
    if args.research_batch_size < 1:
        raise SystemExit("--research-batch-size must be >= 1.")
    if args.max_search_tasks < 1:
        raise SystemExit("--max-search-tasks must be >= 1.")
    if args.approval_threshold < 1:
        raise SystemExit("--approval-threshold must be >= 1.")
    try:
        parse_review_slice(args.review_slice)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.write_limit < 0:
        raise SystemExit("--write-limit must be >= 0.")
    if args.write_start_row < 2:
        raise SystemExit("--write-start-row must be >= 2.")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Lead executive research workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "preflight",
        "pull",
        "prepare-review",
        "notify-review",
        "status-approved",
        "resume-approved",
        "freeze-approved",
        "research-prompt",
        "apply-research",
        "reconcile-existing",
        "export-search-tasks",
        "run-search-tasks",
        "apply-search-results",
        "write-computation",
        "archive-status",
        "archive-computation",
        "archive-unreviewed",
        "bridge-prefinal-to-prospects",
        "process",
        "process-approved",
        "push",
        "run",
    ):
        sub = subparsers.add_parser(command)
        add_common_args(sub)
    args = parser.parse_args(argv)
    if args.command == "bridge-prefinal-to-prospects" and "--source-tab" not in raw_argv:
        # Bridge reads from Pre-final by default even if the workflow's global DEFAULT_SOURCE_TAB differs.
        args.source_tab = "Pre-final"
    validate_args(args)
    ensure_dirs()

    if args.command == "preflight":
        print(json.dumps(preflight(args), indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-status":
        archive = ResearchArchive(archive_index_path(args))
        print(json.dumps(archive.stats(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-computation":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit("No computation file found. Pass --computation-file.")
        computation = load_run(computation_file)
        result = archive_computation_state(computation, computation_file, args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-unreviewed":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit("No computation file found. Pass --computation-file.")
        computation = load_run(computation_file)
        if computation.get("publication_mode") != "archive_only":
            raise SystemExit(
                "Refusing to archive and remove a reviewed group. "
                "archive-unreviewed only accepts archive_only computations."
            )
        result = archive_unreviewed_computation(computation, computation_file, args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    run_file: Path
    if args.command == "pull":
        run, run_file = build_run(args)
        print_summary(run, run_file, rows_written=0, dry_run=args.dry_run)
        return 0

    if args.command == "prepare-review":
        run, run_file = prepare_review(args)
        print_summary(run, run_file, rows_written=0, dry_run=args.dry_run)
        return 0

    if args.command == "status-approved":
        print(json.dumps(approved_workflow_status(args), indent=2, ensure_ascii=False))
        return 0

    if args.command == "resume-approved":
        status = approved_workflow_status(args)
        print(json.dumps(status, indent=2, ensure_ascii=False))
        action = status.get("next_action")
        computation_file = (
            Path(status.get("computation_file", "")) if status.get("computation_file") else None
        )
        if action in {"claim_and_freeze", "freeze_approved"}:
            if action == "claim_and_freeze":
                claim = claim_approved_gate(args)
                if not claim.get("claimed"):
                    raise SystemExit(
                        f"Could not claim approved group: {claim.get('reason', 'unknown reason')}"
                    )
            latest = latest_review_run_file()
            if latest is None:
                raise SystemExit("No review run file found for freeze-approved.")
            run = load_run(latest)
            _snapshot, snapshot_file, _computation, new_computation_file = freeze_approved(
                run, latest, args
            )
            print(f"Snapshot file: {snapshot_file}")
            print(f"Computation file: {new_computation_file}")
        elif action == "research_batch":
            if not computation_file:
                raise SystemExit("No computation file available for research prompt.")
            computation = load_run(computation_file)
            prompt_file = write_research_prompt(computation, computation_file, args)
            print(f"Prompt file: {prompt_file}")
        elif action == "reconcile_existing":
            if not computation_file:
                raise SystemExit("No computation file available for reconcile-existing.")
            computation = reconcile_computation_existing_data(load_run(computation_file))
            save_run(computation, computation_file)
            print(f"Reconciled existing data: {computation_file}")
            print(f"Search tasks: {len(computation.get('search_tasks', []))}")
        elif action == "run_playwright_search":
            if not computation_file:
                raise SystemExit("No computation file available for search task export.")
            computation = load_run(computation_file)
            task_file, tasks = export_pending_search_tasks(computation, computation_file, args)
            print(f"Search tasks file: {task_file}")
            print(f"Search tasks: {len(tasks)}")
        elif action == "write_computation":
            if not computation_file:
                raise SystemExit("No computation file available for write-computation.")
            computation = load_run(computation_file)
            if args.dry_run:
                rows, skipped = computation_rows_for_write(computation, args)
                rows_written = 0
                queue_batch = {
                    "dry_run": True,
                    "fingerprint": rows_fingerprint(rows) if rows else "",
                    "row_count": len(rows),
                }
            else:
                rows_written, rows, skipped, queue_batch = write_computation_rows(
                    computation, args, computation_file
                )
                consume_reused_archive_entries(computation, rows, computation_file, args)
            computation.setdefault("writes", []).append(
                {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "destination_tab": args.destination_tab,
                    "write_limit": args.write_limit,
                    "include_unresolved": args.include_unresolved,
                    "require_p2": args.require_p2,
                    "dry_run": args.dry_run,
                    "rows_written": rows_written,
                    "lead_ids": [row.get("ID", "") for row in rows],
                    "queue_batch": queue_batch,
                    "skipped_unresolved_count": len(skipped),
                    "skipped_unresolved": skipped,
                }
            )
            computation["status"] = "written" if rows_written and not skipped else "write_partial"
            save_run(computation, computation_file)
            print(f"Rows written: {rows_written}")
            print(f"Skipped unresolved: {len(skipped)}")
        elif action == "archive_unreviewed":
            if not computation_file:
                raise SystemExit("No computation file available for archive-unreviewed.")
            computation = load_run(computation_file)
            result = archive_unreviewed_computation(computation, computation_file, args)
            print(f"Archived leads: {result.get('archived_count', 0)}")
            print(f"Skipped leads: {result.get('skipped_count', 0)}")
            removal = result.get("review_group_removal", {})
            print(
                "Review group removed: "
                f"{removal.get('start_row', '?')}–{removal.get('end_row', '?')}"
            )
        elif action == "mark_processed":
            fingerprint = clean_text(status.get("gate", {}).get("fingerprint"))
            if not fingerprint:
                raise SystemExit("No approved-group fingerprint available to mark processed.")
            result = mark_approved_gate(args, fingerprint, "processed")
            print(f"Marked processed: {result.get('fingerprint', fingerprint)}")
        else:
            print(f"No automatic local resume step for action: {action}")
        return 0

    if args.command == "research-prompt":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        prompt_file = write_research_prompt(computation, computation_file, args)
        print(prompt_file.read_text())
        print(f"Prompt file: {prompt_file}")
        return 0

    if args.command == "apply-research":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        if not args.research_file:
            raise SystemExit("Missing --research-file.")
        computation = load_run(computation_file)
        computation = apply_research_results(computation, Path(args.research_file))
        save_run(computation, computation_file)
        latest_import = computation.get("research_imports", [{}])[-1]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Research file: {args.research_file}")
        print(f"Companies applied: {latest_import.get('applied', 0)}")
        print(f"Unmatched companies: {len(latest_import.get('unmatched_companies', []))}")
        return 0

    if args.command == "reconcile-existing":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        computation = reconcile_computation_existing_data(computation)
        save_run(computation, computation_file)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Status: {computation.get('status')}")
        print(f"Search tasks: {len(computation.get('search_tasks', []))}")
        return 0

    if args.command == "export-search-tasks":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        task_file, tasks = export_pending_search_tasks(computation, computation_file, args)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search tasks file: {task_file}")
        print(f"Search tasks: {len(tasks)}")
        return 0

    if args.command == "run-search-tasks":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        computation, result_file = run_search_tasks(computation, computation_file, args)
        latest_batch = computation.get("search_result_batches", [{}])[-1]
        selected = [
            row for row in latest_batch.get("results", []) if row.get("status") == "selected"
        ]
        needs_review = [
            row for row in latest_batch.get("results", []) if row.get("status") == "needs_review"
        ]
        errors = [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search result file: {result_file}")
        print(f"Tasks processed: {latest_batch.get('task_count', 0)}")
        print(f"Selected: {len(selected)}")
        print(f"Needs review: {len(needs_review)}")
        print(f"Errors: {len(errors)}")
        print(f"Remaining pending: {computation.get('search_tasks_remaining', 0)}")
        return 0

    if args.command == "apply-search-results":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        if not args.search_result_file:
            raise SystemExit("Missing --search-result-file.")
        computation = load_run(computation_file)
        computation = apply_search_result_batch(computation, Path(args.search_result_file), args)
        save_run(computation, computation_file)
        latest_batch = computation.get("search_result_batches", [{}])[-1]
        selected = [
            row for row in latest_batch.get("results", []) if row.get("status") == "selected"
        ]
        needs_review = [
            row for row in latest_batch.get("results", []) if row.get("status") == "needs_review"
        ]
        errors = [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search result file: {args.search_result_file}")
        print(f"Selected: {len(selected)}")
        print(f"Needs review: {len(needs_review)}")
        print(f"Errors: {len(errors)}")
        print(f"Remaining pending: {computation.get('search_tasks_remaining', 0)}")
        return 0

    if args.command == "write-computation":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        if args.dry_run:
            rows, skipped = computation_rows_for_write(computation, args)
            rows_written = 0
            queue_batch = {
                "dry_run": True,
                "fingerprint": rows_fingerprint(rows) if rows else "",
                "row_count": len(rows),
            }
        else:
            rows_written, rows, skipped, queue_batch = write_computation_rows(
                computation, args, computation_file
            )
            consume_reused_archive_entries(computation, rows, computation_file, args)
        computation.setdefault("writes", []).append(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "destination_tab": args.destination_tab,
                "write_limit": args.write_limit,
                "include_unresolved": args.include_unresolved,
                "require_p2": args.require_p2,
                "dry_run": args.dry_run,
                "rows_written": rows_written,
                "lead_ids": [row.get("ID", "") for row in rows],
                "queue_batch": queue_batch,
                "skipped_unresolved_count": len(skipped),
                "skipped_unresolved": skipped,
            }
        )
        computation["status"] = "written" if rows_written and not skipped else "write_partial"
        save_run(computation, computation_file)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Destination tab: {args.destination_tab}")
        print(f"Rows ready: {len(rows)}")
        print(f"Rows written: {rows_written}")
        print(f"Queue batch: {queue_batch.get('fingerprint', '')}")
        print(f"Skipped unresolved: {len(skipped)}")
        for item in skipped:
            reasons = ", ".join(item.get("reasons", []))
            print(f"- {item.get('lead_id', '')} {item.get('company', '')}: {reasons}")
        return 0

    if args.command == "bridge-prefinal-to-prospects":
        result = bridge_prefinal_to_prospects(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1

    if args.run_file:
        run_file = Path(args.run_file)
        run = load_run(run_file)
    else:
        if args.command in {"process-approved", "notify-review", "freeze-approved"}:
            latest = latest_review_run_file()
            if latest is None:
                raise SystemExit("No run file found. Run prepare-review first or pass --run-file.")
            run_file = latest
            run = load_run(run_file)
        else:
            run, run_file = build_run(args)

    rows_written = int(run.get("rows_written", 0) or 0)
    if args.command == "freeze-approved":
        snapshot, snapshot_file, computation, computation_file = freeze_approved(
            run, run_file, args
        )
        print("Lead exec research summary")
        print(f"Run file: {run_file}")
        print(f"Snapshot file: {snapshot_file}")
        print(f"Computation file: {computation_file}")
        print(f"Approved leads frozen: {snapshot.get('approved_count', 0)}")
        return 0
    if args.command == "notify-review":
        run = notify_review(run, run_file, args)
    elif args.command == "process-approved":
        if not args.force:
            raise SystemExit(
                "process-approved is deprecated for normal operation because it bypasses the Codex research flow. "
                "Use freeze-approved -> research-prompt -> apply-research -> reconcile/search -> write-computation, "
                "or pass --force if you intentionally need the legacy shortcut."
            )
        run = process_approved_run(run, run_file, args)
    elif args.command in {"process", "run"}:
        run = process_run(run, run_file, args)
    if args.command in {"push", "run", "process-approved"}:
        rows_written = push_run(run, run_file, args)
    print_summary(run, run_file, rows_written=rows_written, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
