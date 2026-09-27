"""Run-build, processing, and workflow orchestration for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S13). Pure move.
"""

import argparse
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from lead_research_archive import (
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    hydrate_computation_lead,
)

from outbound.leads.config import (
    COMPUTATIONS_DIR,
    DESTINATION_COLUMNS,
    PROMPTS_DIR,
    RUNS_DIR,
    SEARCH_RESULTS_DIR,
    SNAPSHOTS_DIR,
)
from outbound.leads.destination import build_destination_row, write_destination_rows
from outbound.leads.extract import (
    fallback_execs_from_employees,
    match_employee,
    reconcile_exec,
    score_search_candidate,
    search_execs_for_company,
)
from outbound.leads.grouping import assign_primary_lanes, chunks, group_source_rows
from outbound.leads.notify import notify_review_with_fallback
from outbound.leads.reviewtab import (
    checkbox_truthy,
    find_successful_review_group_row_for_today,
    read_review_rows,
    review_group_date_value,
    update_review_statuses,
    write_review_rows,
)
from outbound.leads.runs import (
    apply_review_slice,
    archive_index_path,
    destination_sheet_url,
    lead_id_fingerprint,
    now_run_id,
    resolve_ignore_review_approval,
    save_run,
    source_sheet_url,
)
from outbound.leads.search import SearchClient, search_query_variants
from outbound.leads.sheetsio import (
    canonicalize_source_rows,
    load_destination_company_by_id,
    load_reviewed_ids,
    read_worksheet,
    require_columns,
)
from outbound.leads.text import clean_text, linkedin_url_name_score, normalize_key, normalize_url


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
