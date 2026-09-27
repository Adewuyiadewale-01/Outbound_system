"""Approval-gate and computation-status helpers for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S11). Pure move.
"""

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.leads.destination import (
    build_destination_row_from_computation,
    computation_rows_for_write,
)
from outbound.leads.runs import (
    latest_computation_file_for_fingerprint,
    load_run,
    resolve_ignore_review_approval,
    source_sheet_url,
)
from outbound.leads.text import is_linkedin_profile_url


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
