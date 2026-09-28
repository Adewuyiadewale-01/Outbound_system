"""Destination-row building and final write logic for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S7). Pure move.
"""

import argparse
from pathlib import Path
from typing import Any

import gspread

from outbound.leads.config import (
    DEFAULT_DESTINATION_TAB,
    DESTINATION_COLUMNS,
    OPTIONAL_DESTINATION_COLUMNS,
)
from outbound.leads.extract import seniority_score
from outbound.leads.runs import destination_sheet_url
from outbound.leads.sheetsio import require_columns
from outbound.leads.text import clean_text, is_linkedin_profile_url, normalize_key
from outbound.shared.queue import enqueue_batch, record_prefinal_publish, rows_fingerprint
from outbound.shared.sheets import get_client, get_worksheet, open_sheet


def choose_people(finalized_execs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with_urls = [item for item in finalized_execs if item.get("linkedin")]
    without_urls = [item for item in finalized_execs if not item.get("linkedin")]
    ordered = sorted(with_urls, key=lambda item: item.get("confidence", 0), reverse=True)
    ordered.extend(sorted(without_urls, key=lambda item: item.get("confidence", 0), reverse=True))
    return ordered[:3]


def build_destination_row(group: dict[str, Any]) -> dict[str, Any]:
    selected = choose_people(group.get("finalized_execs", []))
    row = {
        "ID": group.get("id", ""),
        "Company": group.get("company", {}).get("name", ""),
        "Website": group.get("company", {}).get("website", ""),
        "Company LinkedIn": group.get("company", {}).get("linkedin", ""),
        "Emp Count": group.get("company", {}).get("employee_count")
        or len(group.get("employees_from_sheet", [])),
        "Source Tab": group.get("company", {}).get("class") or group.get("source_tab", ""),
        "Primary Lane": group.get("primary_lane", ""),
        "Use": group.get("review_use", ""),
    }
    for idx, person in enumerate(selected, start=1):
        row[f"P{idx} Name"] = person.get("name", "")
        row[f"P{idx} Title"] = person.get("role", "")
        row[f"P{idx} LinkedIn"] = person.get("linkedin", "")
        row[f"P{idx} Email"] = person.get("email", "")
    return row


def build_destination_row_from_computation(lead: dict[str, Any]) -> dict[str, Any]:
    stored_executives = lead.get("executives", [])
    executives = (
        stored_executives[:3]
        if any(
            executive.get("research_source") == "manual_dashboard"
            for executive in stored_executives
        )
        else select_computation_people(stored_executives)
    )
    row = {
        "ID": lead.get("lead_id", ""),
        "Company": lead.get("company", ""),
        "Website": lead.get("website", ""),
        "Company LinkedIn": lead.get("company_linkedin", ""),
        "Emp Count": lead.get("emp_count", ""),
        "Source Tab": lead.get("source_tab", ""),
        "Primary Lane": lead.get("primary_lane", ""),
        "Use": lead.get("use", ""),
    }
    for idx, executive in enumerate(executives, start=1):
        row[f"P{idx} Name"] = executive.get("name", "")
        row[f"P{idx} Title"] = executive.get("title", "")
        row[f"P{idx} LinkedIn"] = executive.get("linkedin_url", "")
        row[f"P{idx} Email"] = executive.get("email", "")
    return row


def select_computation_people(executives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(executives))
    indexed.sort(
        key=lambda item: (
            0 if is_linkedin_profile_url(item[1].get("linkedin_url", "")) else 1,
            -seniority_score(item[1]),
            item[0],
        )
    )
    return [item for _, item in indexed[:3]]


def prefinal_row_readiness(row: dict[str, Any], require_p2: bool = False) -> tuple[bool, list[str]]:
    reasons = []
    for column in ("ID", "Company", "Website"):
        if not clean_text(row.get(column)):
            reasons.append(f"missing_{normalize_key(column).replace(' ', '_')}")
    if not clean_text(row.get("P1 Name")):
        reasons.append("missing_p1_name")
    if not clean_text(row.get("P1 LinkedIn")):
        reasons.append("missing_p1_linkedin")
    elif not is_linkedin_profile_url(row.get("P1 LinkedIn", "")):
        reasons.append("invalid_p1_linkedin")
    if require_p2:
        if not clean_text(row.get("P2 Name")):
            reasons.append("missing_p2_name")
        if not clean_text(row.get("P2 LinkedIn")):
            reasons.append("missing_p2_linkedin")
        elif not is_linkedin_profile_url(row.get("P2 LinkedIn", "")):
            reasons.append("invalid_p2_linkedin")
    return not reasons, reasons


def filter_ready_prefinal_rows(
    rows: list[dict[str, Any]],
    require_p2: bool = False,
    include_unresolved: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ready = []
    skipped = []
    for row in rows:
        ok, reasons = prefinal_row_readiness(row, require_p2=require_p2)
        if ok or include_unresolved:
            ready.append(row)
        else:
            skipped.append(
                {
                    "lead_id": clean_text(row.get("ID")),
                    "company": clean_text(row.get("Company")),
                    "reasons": reasons,
                }
            )
    return ready, skipped


def computation_rows_for_write(
    computation: dict[str, Any], args: argparse.Namespace
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    leads = computation.get("leads", [])
    if args.write_limit:
        leads = leads[: args.write_limit]
    rows = [build_destination_row_from_computation(lead) for lead in leads]
    return filter_ready_prefinal_rows(
        rows,
        require_p2=args.require_p2,
        include_unresolved=args.include_unresolved,
    )


def verify_destination_rows(
    credentials_path: Path,
    sheet_url: str,
    destination_tab: str,
    rows: list[dict[str, Any]],
    start_row: int,
) -> bool:
    if not rows:
        return True
    client = get_client(str(credentials_path))
    worksheet = get_worksheet(open_sheet(client, sheet_url), destination_tab)
    headers = worksheet.row_values(1)
    end_row = start_row + len(rows) - 1
    values = worksheet.get(f"A{start_row}:{gspread.utils.rowcol_to_a1(end_row, len(headers))}")
    observed = []
    for value_row in values:
        padded = value_row + [""] * (len(headers) - len(value_row))
        observed.append({header: padded[index] for index, header in enumerate(headers)})
    return len(observed) == len(rows) and rows_fingerprint(observed) == rows_fingerprint(rows)


def write_computation_rows(
    computation: dict[str, Any],
    args: argparse.Namespace,
    computation_file: Path | None = None,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if computation.get("publication_mode") == "archive_only":
        raise ValueError(
            "This computation came from an all-leads/unreviewed selection and is archive-only. "
            "Run archive-computation instead of publishing it to Pre-final."
        )
    rows, skipped = computation_rows_for_write(computation, args)
    queue_batch: dict[str, Any] = {}
    if args.destination_tab != DEFAULT_DESTINATION_TAB or not rows:
        rows_written = write_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            start_row=args.write_start_row,
        )
        return rows_written, rows, skipped, queue_batch

    # This is the durable boundary: persist the exact validated rows before the
    # Pre-final overwrite, so later activity work never depends on the live tab.
    queue_batch = enqueue_batch(rows, str(computation_file or ""))
    if queue_batch.get("status") == "prospects_bridged":
        raise ValueError(
            f"Queue batch {queue_batch['fingerprint']} is already completed; refusing to republish it."
        )
    try:
        rows_written = write_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            start_row=args.write_start_row,
        )
        verified = verify_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            args.write_start_row,
        )
        if not verified:
            record_prefinal_publish(
                queue_batch["fingerprint"],
                start_row=args.write_start_row,
                verified=False,
                error="readback_fingerprint_mismatch",
            )
            raise RuntimeError("Pre-final write readback did not match the queued batch")
        queue_batch = record_prefinal_publish(
            queue_batch["fingerprint"], start_row=args.write_start_row, verified=True
        )
    except Exception as exc:
        record_prefinal_publish(
            queue_batch["fingerprint"],
            start_row=args.write_start_row,
            verified=False,
            error=str(exc),
        )
        raise
    return rows_written, rows, skipped, queue_batch


def write_destination_rows(
    credentials_path: Path,
    sheet_url: str,
    destination_tab: str,
    rows: list[dict[str, Any]],
    start_row: int = 2,
) -> int:
    if not rows:
        return 0
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, destination_tab)
    headers = worksheet.row_values(1)
    require_columns(headers, DESTINATION_COLUMNS, destination_tab)
    missing_optional = [column for column in OPTIONAL_DESTINATION_COLUMNS if column not in headers]
    if missing_optional:
        raise ValueError(
            f"{destination_tab} is missing required manually-created columns for this workflow: "
            f"{', '.join(missing_optional)}"
        )
    payload = [[row.get(header, "") for header in headers] for row in rows]
    start_cell = gspread.utils.rowcol_to_a1(start_row, 1)
    end_cell = gspread.utils.rowcol_to_a1(start_row + len(payload) - 1, len(headers))
    worksheet.update(
        range_name=f"{start_cell}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return len(payload)
