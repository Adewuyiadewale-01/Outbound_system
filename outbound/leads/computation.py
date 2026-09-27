"""Computation archive/consume helpers for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S8). Pure move.
"""

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from lead_research_archive import ResearchArchive, has_reusable_research

from outbound.leads.reviewtab import remove_review_group_for_run
from outbound.leads.runs import archive_index_path, load_run, save_run, source_sheet_url
from outbound.leads.text import clean_text


def archive_computation_state(
    computation: dict[str, Any],
    computation_file: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    archive = ResearchArchive(archive_index_path(args))
    result = archive.archive_computation(
        computation,
        computation_file=str(computation_file),
        reason=args.archive_reason,
    )
    computation.setdefault("archive_writes", []).append(
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "archive_index": str(archive_index_path(args)),
            "reason": args.archive_reason,
            **result,
        }
    )
    computation["status"] = "archived"
    save_run(computation, computation_file)
    return result


def archive_unreviewed_computation(
    computation: dict[str, Any],
    computation_file: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    archive = ResearchArchive(archive_index_path(args))
    uncovered = []
    for lead in computation.get("leads", []):
        if has_reusable_research(lead):
            continue
        entry_id = clean_text(lead.get("archive_entry_id"))
        if entry_id and archive.get(entry_id):
            continue
        uncovered.append(
            {
                "lead_id": clean_text(lead.get("lead_id")),
                "company": clean_text(lead.get("company")),
                "status": clean_text(lead.get("status")),
            }
        )
    if uncovered:
        raise ValueError(
            f"Refusing to remove the unreviewed group because {len(uncovered)} leads "
            "do not yet have reusable research."
        )
    result = archive_computation_state(computation, computation_file, args)
    run_file_value = clean_text(computation.get("source_run_file"))
    if not run_file_value:
        raise ValueError("Computation is missing source_run_file; cannot remove its review group.")
    run_file = Path(run_file_value)
    run = load_run(run_file)
    removal = remove_review_group_for_run(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
        run,
    )
    if not removal.get("removed"):
        raise ValueError(f"Archive was saved but review group removal was blocked: {removal}")
    run["status"] = "archived_unreviewed"
    run["archive_result"] = result
    run["review_group_removal"] = removal
    save_run(run, run_file)
    computation["status"] = "archived_unreviewed"
    computation["review_group_removal"] = removal
    save_run(computation, computation_file)
    return {**result, "review_group_removal": removal}


def consume_reused_archive_entries(
    computation: dict[str, Any],
    rows: list[dict[str, Any]],
    computation_file: Path,
    args: argparse.Namespace,
) -> int:
    written_ids = {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}
    entry_ids = [
        clean_text(lead.get("archive_entry_id"))
        for lead in computation.get("leads", [])
        if clean_text(lead.get("lead_id")) in written_ids
        and clean_text(lead.get("archive_entry_id"))
    ]
    if not entry_ids:
        return 0
    archive = ResearchArchive(archive_index_path(args))
    consumed = archive.mark_consumed(
        entry_ids,
        computation_file=str(computation_file),
        destination=args.destination_tab,
    )
    computation.setdefault("post_review_reconciliation", {})["archive_entries_consumed"] = consumed
    save_run(computation, computation_file)
    return consumed
