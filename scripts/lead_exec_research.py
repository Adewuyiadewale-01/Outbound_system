#!/usr/bin/env python3
"""
Lead executive research workflow.

Reads grouped company/employee rows from a Google Sheet, creates a structured
local JSON run file, searches for top executives without paid APIs, reconciles
LinkedIn URLs against existing employee rows, and writes finalized P1/P2 rows
to a Prospects-style destination tab.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for _path in (str(HELPERS), str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- leads carve: shim re-exports (appended per slice) ---
from lead_research_archive import (  # noqa: E402
    ResearchArchive,
)
from prefinal_queue import rows_fingerprint  # noqa: E402
from runtime_environment import load_repo_env  # noqa: E402

from outbound.leads.bridge import (  # noqa: F401
    bridge_date_key,
    bridge_date_value,
    bridge_prefinal_to_prospects,
    bridge_state_path,
    existing_prospect_ids,
    first_prospect_write_row,
    parse_bridge_date,
    pick_engaged_person,
    prefinal_bridge_fingerprint,
    prefinal_rows_for_bridge,
    prospect_row_from_prefinal,
    record_outreach_control_prospects_start_row,
    write_prospect_rows,
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
from outbound.leads.gates import (  # noqa: F401
    approved_gate_status,
    approved_workflow_status,
    claim_approved_gate,
    computation_status_counts,
    mark_approved_gate,
    next_approved_action,
)
from outbound.leads.grouping import (  # noqa: F401
    add_employee,
    assign_primary_lanes,
    chunks,
    group_source_rows,
    looks_like_company_row,
    looks_like_employee_row,
)
from outbound.leads.notify import (  # noqa: F401
    enqueue_review_notification,
    export_pending_search_tasks,
    notify_review_with_fallback,
    review_email_body,
    review_email_subject,
    send_review_email,
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
from outbound.leads.workflow import (  # noqa: F401
    annotate_overlap_scan,
    apply_research_results,
    apply_search_result_batch,
    approved_leads_from_review,
    build_research_prompt,
    build_run,
    collect_overlap_top_up_waves,
    filter_run_to_approved,
    find_executive_for_task,
    freeze_approved,
    normalize_research_executives,
    notify_review,
    preflight,
    prepare_review,
    print_summary,
    process_approved_run,
    process_run,
    push_run,
    reconcile_computation_existing_data,
    run_search_tasks,
    score_task_candidate,
    write_research_prompt,
)

load_repo_env()


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
