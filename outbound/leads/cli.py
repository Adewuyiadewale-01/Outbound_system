"""CLI entry for the leads research workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S14). Pure move.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from outbound.leads.archive import ResearchArchive
from outbound.leads.bridge import bridge_prefinal_to_prospects
from outbound.leads.computation import (
    archive_computation_state,
    archive_unreviewed_computation,
    consume_reused_archive_entries,
)
from outbound.leads.config import (
    DEFAULT_CREDS,
    DEFAULT_DESTINATION_TAB,
    DEFAULT_FINAL_TAB,
    DEFAULT_NOTIFICATION_QUEUE_TAB,
    DEFAULT_NOTIFY_EMAIL,
    DEFAULT_OBF_SHEET_URL,
    DEFAULT_PROSPECTS_TAB,
    DEFAULT_RESEARCH_ARCHIVE_INDEX,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    DEFAULT_SOURCE_TAB,
)
from outbound.leads.destination import computation_rows_for_write, write_computation_rows
from outbound.leads.gates import approved_workflow_status, claim_approved_gate, mark_approved_gate
from outbound.leads.notify import export_pending_search_tasks
from outbound.leads.runs import (
    archive_index_path,
    destination_sheet_url,
    ensure_dirs,
    latest_computation_file,
    latest_review_run_file,
    load_run,
    parse_review_slice,
    save_run,
    source_sheet_url,
)
from outbound.leads.text import clean_text
from outbound.leads.workflow import (
    apply_research_results,
    apply_search_result_batch,
    build_run,
    freeze_approved,
    notify_review,
    preflight,
    prepare_review,
    print_summary,
    process_approved_run,
    process_run,
    push_run,
    reconcile_computation_existing_data,
    run_search_tasks,
    write_research_prompt,
)
from outbound.shared.queue import rows_fingerprint


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
