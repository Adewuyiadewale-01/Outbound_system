#!/usr/bin/env python3
"""Standalone Pre-final activity checker with staged Final and Prospects bridges."""

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for _path in (str(HELPERS), str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- activity_check carve: shim re-exports (appended per slice) ---
from linkedin_outreach_session import (  # noqa: E402
    OBF_SHEET_URL,
    sheet_date,
)
from prefinal_queue import (  # noqa: E402
    enqueue_batch,
    load_batch,
    next_activity_batch,
    update_batch_status,
)
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import require_columns  # noqa: E402

from outbound.activity_check.analysis import (  # noqa: F401
    activity_detail_empty_success_reason,
    activity_detail_should_defer,
    activity_evidence,
    activity_level,
    activity_score,
    category_for_row,
    count_entries_within,
    detail_contains_text,
    final_sort_key,
    is_aggregate_activity_container,
    non_aggregate_entries,
    rank_row_for_final,
    should_swap,
)
from outbound.activity_check.config import (  # noqa: F401
    ACTIVITY_COLUMNS,
    ACTIVITY_SCORE,
    ACTIVITY_VALUES,
    BASE_COLUMNS,
    BLOCKING_DANGER_PATTERNS,
    CATEGORY_PRIORITY,
    DEFAULT_ACTIVITY_SEQUENCE_TAB,
    DEFAULT_CREDS,
    DEFAULT_FINAL_TAB,
    DEFAULT_LEADS_SHEET_URL,
    DEFAULT_MAX_TARGET_ATTEMPTS,
    DEFAULT_PREFINAL_TAB,
    DEFER_ACTIVITY_REASONS,
    DIVERSION_OPTIONS,
    FINAL_REQUIRED_COLUMNS,
    HARD_READ_DANGERS,
    HARD_READ_REASONS,
    JOURNAL_DIR,
    NAVIGATION_TYPE_OPTIONS,
    OPENCLAW_CREDS,
    P1_COLUMNS,
    P2_COLUMNS,
    PERSON_COLUMNS,
    REPO_CREDS,
    SKIP_ACTIVITY_REASONS,
    STATE_DIR,
)
from outbound.activity_check.danger import (  # noqa: F401
    blocked_result,
    hard_read_failure_reason,
    is_blocking_danger,
    is_cdp_transport_exception,
)
from outbound.activity_check.diversion import apply_diversion  # noqa: F401
from outbound.activity_check.finalize import (  # noqa: F401
    bridge_final_to_prospects,
    bridge_ready_rows_to_final,
    final_source_row_count,
    finalize_prepared_session,
    no_queued_batch_result,
    queue_source_rows,
    ready_final_rows,
    resume_final_bridged_batch,
    row_activity_from_state,
    row_targets,
    target_has_activity_decision,
    write_final_batch_upsert_and_sort,
)
from outbound.activity_check.reader import (  # noqa: F401
    LiveActivityReader,
    build_fixture_reader,
    build_synthetic_activity_reader,
)
from outbound.activity_check.retry import (  # noqa: F401
    next_activity_retry_record,
    persist_activity_retry_attempt,
    persist_terminal_activity_issue,
)
from outbound.activity_check.sequence import (  # noqa: F401
    distribute_slots_to_batches,
    ensure_activity_sequence,
    generated_activity_timing,
    generated_batch_gaps,
    generated_delay_seconds,
    generated_diversion_seconds,
    generated_diversions,
    generated_navigation_types,
    resolve_activity_sequence_columns,
    unique_ints_in_order,
)
from outbound.activity_check.sheets import person_from_row, read_worksheet, set_person  # noqa: F401
from outbound.activity_check.state import (  # noqa: F401
    activity_journal_path,
    activity_session_path,
    append_jsonl,
    emit_progress,
    journal_event,
    load_session_state,
    prepare_session_state,
    save_session_state,
)
from outbound.activity_check.targets import extract_targets  # noqa: F401
from outbound.activity_check.text import (  # noqa: F401
    _parse_positive_int,
    canonical_linkedin_profile_url,
    clean_text,
    is_linkedin_profile_url,
    navigation_type_key,
    normalize_activity_value,
    normalize_navigation_type,
    normalize_url,
    normalized_profile_key,
    target_key,
)

load_repo_env()


# A LinkedIn route can occasionally land on a visually blank shell: URL is loaded,
# CDP is reachable, but the app never hydrates. That is a per-profile/page load
# problem, not a reason to kill the whole activity lane.
PENDING_404_STATUS = "retry_pending_404"


def run(args: argparse.Namespace) -> dict[str, Any]:
    date_value = sheet_date(args.date)
    queue_batch = (
        load_batch(args.queue_fingerprint) if args.queue_fingerprint else next_activity_batch()
    )
    if queue_batch is None and args.enqueue_current_prefinal:
        headers, legacy_rows = read_worksheet(
            Path(args.credentials), args.sheet_url, args.prefinal_tab
        )
        require_columns(headers, BASE_COLUMNS + P1_COLUMNS + P2_COLUMNS, args.prefinal_tab)
        legacy_rows = [
            row
            for row in legacy_rows
            if clean_text(row.get("ID")) or clean_text(row.get("Company"))
        ]
        if legacy_rows:
            queue_batch = enqueue_batch(legacy_rows, "legacy_prefinal_import")
    if queue_batch is None:
        return no_queued_batch_result(args, date_value)
    queue_fingerprint = clean_text(queue_batch.get("fingerprint"))
    if queue_batch.get("status") == "final_bridged":
        return resume_final_bridged_batch(args, date_value, queue_batch)
    headers, source_rows = queue_source_rows(queue_batch)
    require_columns(headers, BASE_COLUMNS + P1_COLUMNS + P2_COLUMNS, args.prefinal_tab)
    all_targets = extract_targets(
        source_rows,
        queue_batch.get("activity_results") or {},
        queue_batch.get("activity_issues") or {},
    )
    state = load_session_state(date_value)
    # Carry forward confirmed decisions from prior dates so a partially checked
    # row can be completed by checking only its remaining blank profile, while
    # Final receives the combined P1/P2 decision set.
    for key, result in (queue_batch.get("activity_results") or {}).items():
        activity_value = normalize_activity_value((result or {}).get("activity_value", ""))
        if activity_value:
            state.setdefault("targets", {}).setdefault(
                key,
                {
                    "activity_value": activity_value,
                    "status": "recorded",
                    "recorded_at": (result or {}).get("recorded_at", ""),
                    "persisted_from_queue": True,
                },
            )
    if args.activity_only and args.prepare_only:
        return blocked_result(
            args=args,
            date_value=date_value,
            targets=[],
            completed=0,
            skipped_resume=0,
            bridged=0,
            failures=[{"error": "activity_only_prepare_only_conflict"}],
            sequence={
                "ok": False,
                "sequence_tab": args.activity_sequence_tab,
                "activity_only": True,
            },
            state=state,
        )
    if args.activity_only:
        if clean_text(state.get("queue_fingerprint")) != queue_fingerprint:
            return blocked_result(
                args=args,
                date_value=date_value,
                targets=[],
                completed=0,
                skipped_resume=0,
                bridged=0,
                failures=[
                    {
                        "error": "prepared_session_queue_mismatch",
                        "prepared_queue_fingerprint": state.get("queue_fingerprint", ""),
                        "active_queue_fingerprint": queue_fingerprint,
                    }
                ],
                sequence={
                    "ok": False,
                    "sequence_tab": args.activity_sequence_tab,
                    "activity_only": True,
                },
                state=state,
            )
        prepared_targets = state.get("prepared_targets") or []
        prepared_plan = state.get("runtime_plan") or []
        sequence = {
            **(state.get("sequence_summary") or {}),
            "ok": True,
            "sequence_tab": args.activity_sequence_tab,
            "activity_only": True,
        }
        if not prepared_targets or not prepared_plan:
            return blocked_result(
                args=args,
                date_value=date_value,
                targets=[],
                completed=0,
                skipped_resume=0,
                bridged=0,
                failures=[
                    {
                        "error": "prepared_session_missing_or_not_ready",
                        "session_path": str(activity_session_path(date_value)),
                    }
                ],
                sequence=sequence,
                state=state,
            )
        prepared_pairs = list(zip(prepared_targets, prepared_plan))
        if args.worker_id:
            assigned_pairs = [
                pair
                for pair in prepared_pairs
                if clean_text(pair[0].get("worker_id")) == args.worker_id
            ]
            if not assigned_pairs:
                return blocked_result(
                    args=args,
                    date_value=date_value,
                    targets=[],
                    completed=0,
                    skipped_resume=0,
                    bridged=0,
                    failures=[
                        {"error": "worker_has_no_assigned_targets", "worker_id": args.worker_id}
                    ],
                    sequence=sequence,
                    state=state,
                )
            prepared_pairs = assigned_pairs

        if args.retry_pending_404:
            prepared_pairs = [
                pair
                for pair in prepared_pairs
                if state.get("targets", {}).get(pair[0]["key"], {}).get("status")
                == PENDING_404_STATUS
            ]
        targets = [dict(target) for target, _plan in prepared_pairs]
        if args.retry_pending_404:
            for target in targets:
                original_url = target.get("profile_url", "")
                target["retry_original_profile_url"] = original_url
                target["profile_url"] = canonical_linkedin_profile_url(original_url)
        runtime_plan = [dict(plan) for _target, plan in prepared_pairs]
        if args.limit and args.limit > 0:
            targets = targets[: args.limit]
            runtime_plan = runtime_plan[: args.limit]
    else:
        targets = list(all_targets)
        if args.limit and args.limit > 0:
            targets = targets[: args.limit]
        if args.test_synthetic_activity:
            runtime_plan = [
                {
                    "slot_id": index + 1,
                    "batch_number": index + 1,
                    "activity_log_timing": "before_diversion",
                    "delay_sec": 0,
                    "lead_diversion": "none",
                    "activity_diversion_sec": None,
                    "navigation_type": "Direct Url",
                    "batch_gap_sec": 0,
                }
                for index in range(len(targets))
            ]
            sequence = {
                "ok": True,
                "sequence_tab": args.activity_sequence_tab,
                "target_count": len(targets),
                "plan": runtime_plan,
                "dry_run": args.dry_run,
                "test_synthetic": True,
            }
        else:
            sequence = ensure_activity_sequence(
                creds=args.credentials,
                obf_url=args.obf_url,
                sequence_tab=args.activity_sequence_tab,
                date_value=date_value,
                target_count=len(targets),
                delay_min_sec=args.delay_min_sec,
                delay_max_sec=args.delay_max_sec,
                batch_gap_min_sec=args.batch_gap_min_sec,
                batch_gap_max_sec=args.batch_gap_max_sec,
                dry_run=args.dry_run,
            )
            runtime_plan = sequence["plan"]
    # Execution boundary: prepared sessions may predate URL normalization or
    # may have been edited manually. Never navigate with their raw value.
    for target in targets:
        original_profile_url = clean_text(target.get("original_profile_url")) or clean_text(
            target.get("profile_url")
        )
        profile_url = canonical_linkedin_profile_url(target.get("profile_url"))
        target["original_profile_url"] = original_profile_url
        target["profile_url"] = profile_url
        target["profile_url_normalized"] = profile_url != original_profile_url.rstrip("/")

    plan_failures: list[dict[str, Any]] = []
    if len(runtime_plan) != len(targets):
        plan_failures.append(
            {
                "error": "runtime_plan_target_mismatch",
                "target_count": len(targets),
                "runtime_plan_count": len(runtime_plan),
            }
        )
    for index, target in enumerate(targets):
        plan_item = runtime_plan[index] if index < len(runtime_plan) else {}
        if not target.get("profile_url"):
            plan_failures.append(
                {
                    "error": "invalid_or_unsupported_profile_url",
                    "target": target,
                    "plan": plan_item,
                }
            )
        if not plan_item.get("slot_id") or not plan_item.get("batch_number"):
            plan_failures.append(
                {
                    "error": "runtime_plan_item_missing_slot_or_batch",
                    "target": target,
                    "plan": plan_item,
                }
            )
    if plan_failures:
        return blocked_result(
            args=args,
            date_value=date_value,
            targets=targets,
            completed=0,
            skipped_resume=0,
            bridged=0,
            failures=plan_failures,
            sequence=sequence,
            state=state,
        )
    if not args.activity_only:
        state = prepare_session_state(
            date_value=date_value,
            state=state,
            targets=targets,
            runtime_plan=runtime_plan,
            sequence=sequence,
            queue_fingerprint=queue_fingerprint,
            dry_run=args.dry_run,
        )
    if args.finalize_only:
        return finalize_prepared_session(
            args=args,
            date_value=date_value,
            state=state,
            source_rows=source_rows,
            all_targets=all_targets,
            queue_fingerprint=queue_fingerprint,
        )
    if args.prepare_only:
        result = {
            "ok": True,
            "status": "prepared_dry_run" if args.dry_run else "prepared",
            "dry_run": bool(args.dry_run),
            "prepare_only": True,
            "date": date_value,
            "source_tab": args.prefinal_tab,
            "queue_fingerprint": queue_fingerprint,
            "final_tab": args.final_tab,
            "activity_sequence_tab": args.activity_sequence_tab,
            "targets": len(targets),
            "session_path": str(activity_session_path(date_value)),
            "journal_path": str(activity_journal_path(date_value)),
            "sequence": {k: v for k, v in sequence.items() if k != "plan"},
            "sample_prepared_targets": state.get("prepared_targets", [])[:3],
        }
        emit_progress(date_value, "prepared", **result)
        return result
    # A row may only enter Final once every valid P1/P2 target on that source row
    # has been resolved. A limited smoke test therefore cannot publish half a row.
    target_keys = {target["key"] for target in all_targets}

    if args.activity_fixture:
        activity_reader: Any = build_fixture_reader(args.activity_fixture)
        session = None
    elif args.test_synthetic_activity:
        activity_reader = build_synthetic_activity_reader()
        session = None
    elif args.dry_run and not args.live_dry_run:

        def activity_reader(_url, _plan_item=None):
            return {}

        session = None
    else:
        activity_reader = LiveActivityReader(args.activity_timeout, args.activity_retries)
        session = activity_reader.session

    completed = 0
    skipped_resume = 0
    bridged = 0
    failures: list[dict[str, Any]] = []
    consecutive_hard_failures = 0
    current_batch: int | None = None
    test_stop_triggered = False
    if isinstance(activity_reader, LiveActivityReader):
        try:
            connect_result = activity_reader.connect()
        except Exception as exc:
            connect_result = {"ok": False, "block_reason": f"LinkedIn preflight exception: {exc}"}
        if not connect_result.get("ok"):
            failures.append(
                {
                    "error": "linkedin_preflight_blocked",
                    "danger": connect_result.get("block_reason", "LinkedIn preflight failed"),
                    "connect_result": connect_result,
                }
            )
            return blocked_result(
                args=args,
                date_value=date_value,
                targets=targets,
                completed=completed,
                skipped_resume=skipped_resume,
                bridged=bridged,
                failures=failures,
                sequence=sequence,
                state=state,
            )
    try:
        if not args.dry_run:
            update_batch_status(
                queue_fingerprint,
                "activity_in_progress",
                activity={
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "session_path": str(activity_session_path(date_value)),
                },
            )
        emit_progress(
            date_value,
            "started",
            targets=len(targets),
            dry_run=args.dry_run,
            session_path=str(activity_session_path(date_value)),
        )
        for index, target in enumerate(targets):
            plan_item = runtime_plan[index] if index < len(runtime_plan) else {}
            batch_number = plan_item.get("batch_number")
            if current_batch is not None and batch_number != current_batch and not args.dry_run:
                previous_gap = int(runtime_plan[index - 1].get("batch_gap_sec") or 0)
                emit_progress(
                    date_value,
                    "batch_gap_start",
                    batch_number=current_batch,
                    gap_sec=previous_gap,
                    processed=index,
                )
                if previous_gap > 0:
                    time.sleep(previous_gap)
                emit_progress(
                    date_value,
                    "batch_gap_done",
                    batch_number=current_batch,
                    gap_sec=previous_gap,
                    processed=index,
                )
            current_batch = batch_number

            retrying_pending_404 = (
                args.retry_pending_404
                and state.get("targets", {}).get(target["key"], {}).get("status")
                == PENDING_404_STATUS
            )
            if target["key"] in state.get("targets", {}) and not retrying_pending_404:
                skipped_resume += 1
                emit_progress(date_value, "target_resume_skip", processed=index + 1, target=target)
                continue

            emit_progress(
                date_value, "target_start", processed=index + 1, target=target, plan=plan_item
            )
            delay_sec = int(plan_item.get("delay_sec") or 0)
            if delay_sec > 0 and not args.dry_run:
                emit_progress(
                    date_value,
                    "target_pre_delay_start",
                    processed=index + 1,
                    target=target,
                    delay_sec=delay_sec,
                )
                time.sleep(delay_sec)
                emit_progress(
                    date_value,
                    "target_pre_delay_done",
                    processed=index + 1,
                    target=target,
                    delay_sec=delay_sec,
                )
            transport_exception = False
            try:
                detail = activity_reader(target["profile_url"], plan_item)
            except Exception as exc:
                transport_exception = is_cdp_transport_exception(exc)
                detail = {
                    "error": True,
                    "danger": "activity_reader_exception",
                    "reason": "activity_reader_exception",
                    "error_detail": str(exc),
                }
            if transport_exception and isinstance(activity_reader, LiveActivityReader):
                emit_progress(
                    date_value,
                    "cdp_recovery_start",
                    processed=index + 1,
                    target=target,
                    error=detail.get("error_detail", ""),
                )
                recovery = activity_reader.recover_connection()
                if not args.dry_run:
                    journal_event(
                        date_value,
                        "cdp_recovery",
                        target=target,
                        plan=plan_item,
                        recovery=recovery,
                    )
                if not recovery.get("ok"):
                    failures.append(
                        {
                            "error": "cdp_recovery_failed",
                            "target": target,
                            "plan": plan_item,
                            "initial_error": detail,
                            "recovery": recovery,
                        }
                    )
                    return blocked_result(
                        args=args,
                        date_value=date_value,
                        targets=targets,
                        completed=completed,
                        skipped_resume=skipped_resume,
                        bridged=bridged,
                        failures=failures,
                        sequence=sequence,
                        state=state,
                        status="paused_for_browser_recovery",
                    )
                session = activity_reader.session
                emit_progress(
                    date_value,
                    "cdp_recovery_retry",
                    processed=index + 1,
                    target=target,
                    recovery_action=recovery.get("action"),
                )
                try:
                    detail = activity_reader(target["profile_url"], plan_item)
                except Exception as retry_exc:
                    retry_detail = {
                        "error": True,
                        "danger": "",
                        "reason": "activity_fresh_tab_retry_failed",
                        "error_detail": str(retry_exc),
                        "original_profile_url": target.get("original_profile_url", ""),
                        "canonical_profile_url": target.get("profile_url", ""),
                        "navigation_type_used": "selector_based",
                        "fresh_tab_recovery": True,
                    }
                    # The retry has already isolated this failure to one
                    # profile. Clean the failed replacement tab before moving
                    # on; only a failure to create another clean tab blocks the
                    # browser lane.
                    retry_cleanup = activity_reader.recover_connection()
                    activity_reader.force_selector_on_next_call = False
                    if not args.dry_run:
                        journal_event(
                            date_value,
                            "cdp_recovery_retry_failed",
                            target=target,
                            plan=plan_item,
                            initial_error=detail,
                            retry_error=retry_detail,
                            recovery=recovery,
                            cleanup=retry_cleanup,
                        )
                    if not retry_cleanup.get("ok"):
                        failures.append(
                            {
                                "error": "cdp_recovery_retry_cleanup_failed",
                                "target": target,
                                "plan": plan_item,
                                "initial_error": detail,
                                "retry_error": retry_detail,
                                "recovery": recovery,
                                "cleanup": retry_cleanup,
                            }
                        )
                        return blocked_result(
                            args=args,
                            date_value=date_value,
                            targets=targets,
                            completed=completed,
                            skipped_resume=skipped_resume,
                            bridged=bridged,
                            failures=failures,
                            sequence=sequence,
                            state=state,
                            status="paused_for_browser_recovery",
                        )
                    session = activity_reader.session
                    detail = retry_detail
            empty_success_reason = activity_detail_empty_success_reason(detail)
            if empty_success_reason:
                detail = {
                    **detail,
                    "error": True,
                    "danger": "invalid_profile_or_404"
                    if empty_success_reason == "invalid_profile_or_404"
                    else "activity_read_timeout",
                    "reason": empty_success_reason,
                    "deferred_empty_extract": empty_success_reason == "activity_feed_not_hydrated",
                }
            hard_reason = hard_read_failure_reason(detail)
            reason_text = clean_text(detail.get("reason")) or hard_reason
            if hard_reason and is_blocking_danger(hard_reason):
                emit_progress(
                    date_value,
                    "target_blocking_danger",
                    processed=index + 1,
                    target=target,
                    reason=hard_reason,
                    danger=clean_text(detail.get("danger")),
                )
                failures.append(
                    {
                        "error": "linkedin_danger_blocked",
                        "danger": hard_reason,
                        "target": target,
                        "plan": plan_item,
                        "detail": detail,
                    }
                )
                break
            if reason_text in SKIP_ACTIVITY_REASONS or hard_reason in SKIP_ACTIVITY_REASONS:
                terminal_reason = hard_reason or reason_text
                evidence = activity_evidence(target["profile_url"], detail)
                record = {
                    **target,
                    "activity_value": "",
                    "status": "terminal_error" if args.retry_pending_404 else PENDING_404_STATUS,
                    "terminal_reason": terminal_reason,
                    "retryable": not args.retry_pending_404,
                    "attempt": 2 if args.retry_pending_404 else 1,
                    "activity_evidence": evidence,
                    "plan": plan_item,
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
                state.setdefault("targets", {})[target["key"]] = record
                if not args.dry_run:
                    save_session_state(date_value, state)
                    journal_event(
                        date_value,
                        "target_activity_error"
                        if args.retry_pending_404
                        else "target_activity_retry_pending",
                        **record,
                    )
                completed += 1
                emit_progress(
                    date_value,
                    "target_error" if args.retry_pending_404 else "target_retry_pending",
                    processed=index + 1,
                    completed=completed,
                    target=target,
                    reason=terminal_reason,
                    danger=clean_text(detail.get("danger")),
                )
                consecutive_hard_failures = 0
                continue
            if (
                hard_reason == "activity_read_timeout"
                and reason_text == "activity_page_not_ready"
                and detail_contains_text(detail, ("/recent-activity/",))
            ):
                hard_reason = ""
                reason_text = "activity_blank_page_not_ready"
                detail = {
                    **detail,
                    "danger": "",
                    "reason": reason_text,
                    "deferred_empty_extract": True,
                }
            retryable_reason = hard_reason or (
                reason_text if reason_text in DEFER_ACTIVITY_REASONS else ""
            )
            if retryable_reason:
                event_type = (
                    "target_activity_retryable_error" if hard_reason else "target_activity_deferred"
                )
                retry_record = next_activity_retry_record(
                    (queue_batch.get("activity_retry_state") or {}).get(target["key"]),
                    target=target,
                    reason=retryable_reason,
                    danger=clean_text(detail.get("danger")),
                    max_attempts=args.max_target_attempts,
                )
                if not args.dry_run:
                    retry_record = persist_activity_retry_attempt(
                        queue_fingerprint,
                        target=target,
                        reason=retryable_reason,
                        danger=clean_text(detail.get("danger")),
                        max_attempts=args.max_target_attempts,
                    )
                    queue_batch.setdefault("activity_retry_state", {})[target["key"]] = retry_record
                emit_progress(
                    date_value,
                    event_type,
                    processed=index + 1,
                    target=target,
                    reason=retryable_reason,
                    danger=clean_text(detail.get("danger")),
                    attempt=retry_record["attempts"],
                    max_attempts=args.max_target_attempts,
                )
                if not args.dry_run:
                    journal_event(
                        date_value,
                        event_type,
                        target=target,
                        reason=retryable_reason,
                        retryable=True,
                        danger=clean_text(detail.get("danger")),
                        detail=activity_evidence(target["profile_url"], detail),
                        plan=plan_item,
                        attempt=retry_record["attempts"],
                        max_attempts=args.max_target_attempts,
                    )
                if retry_record["exhausted"]:
                    terminal_reason = f"activity_retry_limit_reached:{retryable_reason}"
                    evidence = activity_evidence(target["profile_url"], detail)
                    record = {
                        **target,
                        "activity_value": "",
                        "status": "terminal_error",
                        "terminal_reason": terminal_reason,
                        "retryable": False,
                        "attempt": retry_record["attempts"],
                        "max_attempts": args.max_target_attempts,
                        "activity_evidence": evidence,
                        "plan": plan_item,
                        "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    state.setdefault("targets", {})[target["key"]] = record
                    if not args.dry_run:
                        persist_terminal_activity_issue(
                            queue_fingerprint,
                            target=target,
                            terminal_reason=terminal_reason,
                            retry_record=retry_record,
                        )
                        save_session_state(date_value, state)
                        journal_event(
                            date_value,
                            "target_activity_retry_exhausted",
                            **record,
                        )
                    completed += 1
                    consecutive_hard_failures = 0
                    emit_progress(
                        date_value,
                        "target_retry_exhausted",
                        processed=index + 1,
                        completed=completed,
                        target=target,
                        reason=terminal_reason,
                        attempt=retry_record["attempts"],
                    )
                    continue
                if not hard_reason:
                    consecutive_hard_failures = 0
                    continue
                consecutive_hard_failures += 1
                if consecutive_hard_failures >= args.max_consecutive_hard_failures:
                    failures.append(
                        {
                            "error": "consecutive_hard_activity_failures",
                            "reason": hard_reason,
                            "threshold": args.max_consecutive_hard_failures,
                            "target": target,
                            "plan": plan_item,
                            "detail": detail,
                        }
                    )
                    break
                continue
            else:
                consecutive_hard_failures = 0
            level = activity_level(detail)
            evidence = activity_evidence(target["profile_url"], detail)
            record = {
                **target,
                "activity_value": level,
                "status": "recorded" if level else "blank_uncertain_or_failed",
                "activity_evidence": evidence,
                "plan": plan_item,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            state.setdefault("targets", {})[target["key"]] = record
            if not args.dry_run:
                save_session_state(date_value, state)
                journal_event(date_value, "target_activity_read", **record)

            if plan_item.get("activity_log_timing") != "after_diversion":
                if not args.dry_run:
                    journal_event(date_value, "target_activity", **record)
                diversion = apply_diversion(session, plan_item, detail, args.dry_run)
                if not diversion.get("ok", True):
                    failures.append(
                        {"target": target, "error": diversion.get("error", "diversion_failed")}
                    )
                    break
            else:
                diversion = apply_diversion(session, plan_item, detail, args.dry_run)
                if not diversion.get("ok", True):
                    failures.append(
                        {"target": target, "error": diversion.get("error", "diversion_failed")}
                    )
                    break
                if not args.dry_run:
                    journal_event(date_value, "target_activity", **record)
            completed += 1
            emit_progress(
                date_value,
                "target_done",
                processed=index + 1,
                completed=completed,
                target=target,
                activity_value=level,
            )
            if (
                args.test_stop_cdp_after_completed
                and not test_stop_triggered
                and completed >= args.test_stop_cdp_after_completed
                and isinstance(activity_reader, LiveActivityReader)
            ):
                # Explicit E2E failpoint. Browser.close is a clean Chrome
                # shutdown; the next target must exercise the real unhealthy
                # CDP handoff rather than a mocked result.
                test_stop_triggered = True
                emit_progress(
                    date_value,
                    "test_cdp_shutdown",
                    processed=index + 1,
                    completed=completed,
                    target=target,
                )
                if not args.dry_run:
                    journal_event(
                        date_value, "test_cdp_shutdown", target=target, completed=completed
                    )
                try:
                    activity_reader.session.cdp.send("Browser.close", timeout=5)
                except Exception:
                    pass
    finally:
        close = getattr(activity_reader, "close", None)
        if callable(close):
            close()

    resolved_targets = sum(
        1
        for target in targets
        if target["key"] in state.get("targets", {})
        and state["targets"][target["key"]].get("status") != PENDING_404_STATUS
    )
    completion_ratio = (resolved_targets / len(targets)) if targets else 1.0
    planned_target_keys = {target["key"] for target in targets}
    ranked_rows = ready_final_rows(
        source_rows=source_rows,
        state=state,
        all_target_keys=target_keys,
        planned_target_keys=planned_target_keys,
    )
    source_row_count = final_source_row_count(source_rows, target_keys, planned_target_keys)
    ready_row_ratio = (len(ranked_rows) / source_row_count) if source_row_count else 1.0
    final_bridge_eligible = (
        not args.no_bridges
        and not failures
        and completion_ratio >= args.final_bridge_threshold
        and ready_row_ratio >= args.final_bridge_threshold
        and bool(ranked_rows)
    )
    final_bridge: dict[str, Any] = {
        "attempted": False,
        "eligible": final_bridge_eligible,
        "threshold": args.final_bridge_threshold,
        "resolved_targets": resolved_targets,
        "target_count": len(targets),
        "completion_ratio": completion_ratio,
        "ready_rows": len(ranked_rows),
        "source_rows": source_row_count,
        "ready_row_ratio": ready_row_ratio,
    }
    prospects_bridge: dict[str, Any] = {"attempted": False}
    if final_bridge_eligible:
        final_bridge["attempted"] = True
        if ranked_rows:
            try:
                bridge_plan = {
                    "prepared_at": datetime.now().isoformat(timespec="seconds"),
                    "completion_ratio": completion_ratio,
                    "ready_row_ratio": ready_row_ratio,
                    "rows": ranked_rows,
                }
                if not args.dry_run:
                    state["final_bridge_plan"] = bridge_plan
                    save_session_state(date_value, state)
                    journal_event(
                        date_value,
                        "final_bridge_planned",
                        completion_ratio=completion_ratio,
                        ready_row_ratio=ready_row_ratio,
                        row_count=len(ranked_rows),
                    )
                emit_progress(
                    date_value,
                    "final_bridge_start",
                    ready_rows=len(ranked_rows),
                    completion_ratio=completion_ratio,
                )
                final_bridge["result"] = bridge_ready_rows_to_final(
                    args=args, date_value=date_value, state=state, ranked_rows=ranked_rows
                )
                bridged = len(ranked_rows)
                emit_progress(date_value, "final_bridge_done", bridged_rows=bridged)
            except Exception as exc:
                failures.append({"error": "final_bridge_failed", "detail": str(exc)})
                final_bridge["error"] = str(exc)
        else:
            final_bridge["result"] = {"rows_requested": 0, "rows_written": 0, "rows_in_final": None}
        if not failures and not args.no_prospects_bridge:
            delay_seconds = max(0, args.prospects_bridge_delay_sec)
            prospects_bridge = {"attempted": True, "delay_seconds": delay_seconds}
            if delay_seconds and not args.dry_run:
                emit_progress(
                    date_value, "prospects_bridge_wait_start", delay_seconds=delay_seconds
                )
                remaining = delay_seconds
                while remaining > 0:
                    wait_seconds = min(60, remaining)
                    time.sleep(wait_seconds)
                    remaining -= wait_seconds
                    emit_progress(
                        date_value, "prospects_bridge_wait_progress", remaining_seconds=remaining
                    )
            try:
                emit_progress(date_value, "prospects_bridge_start")
                prospects_bridge["result"] = bridge_final_to_prospects(args, date_value)
                if not prospects_bridge["result"].get("ok", False):
                    failures.append(
                        {"error": "prospects_bridge_failed", "detail": prospects_bridge["result"]}
                    )
                emit_progress(
                    date_value, "prospects_bridge_done", result=prospects_bridge["result"]
                )
            except Exception as exc:
                failures.append({"error": "prospects_bridge_failed", "detail": str(exc)})
                prospects_bridge["error"] = str(exc)
    elif not failures:
        final_bridge["skip_reason"] = "no_complete_rows_or_activity_below_final_bridge_threshold"

    status = "blocked" if failures else ("dry_run" if args.dry_run else "completed")
    result = {
        "ok": not failures,
        "status": status,
        "dry_run": bool(args.dry_run),
        "date": date_value,
        "source_tab": args.prefinal_tab,
        "final_tab": args.final_tab,
        "activity_sequence_tab": args.activity_sequence_tab,
        "queue_fingerprint": queue_fingerprint,
        "targets": len(targets),
        "completed_this_run": completed,
        "resume_skips": skipped_resume,
        "bridged_rows": bridged,
        "final_bridge": final_bridge,
        "prospects_bridge": prospects_bridge,
        "failures": failures,
        "session_path": str(activity_session_path(date_value)),
        "journal_path": str(activity_journal_path(date_value)),
        "sequence": {k: v for k, v in sequence.items() if k != "plan"},
    }
    if not args.dry_run:
        if not args.no_bridges and (failures or not final_bridge.get("attempted")):
            queue_status = "activity_blocked"
        elif not args.no_bridges and prospects_bridge.get("result", {}).get("ok", False):
            queue_status = "prospects_bridged"
        elif not args.no_bridges:
            queue_status = "final_bridged"
        if not args.no_bridges:
            update_batch_status(
                queue_fingerprint,
                queue_status,
                activity={
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                    "completion_ratio": completion_ratio,
                    "ready_row_ratio": ready_row_ratio,
                    "session_path": str(activity_session_path(date_value)),
                },
                activity_results={
                    key: {
                        "activity_value": normalize_activity_value(
                            record.get("activity_value", "")
                        ),
                        "recorded_at": record.get("recorded_at", ""),
                    }
                    for key, record in state.get("targets", {}).items()
                    if normalize_activity_value(record.get("activity_value", ""))
                },
                activity_issues={
                    key: {
                        "terminal": True,
                        "terminal_reason": record.get("terminal_reason", ""),
                        "profile_url": record.get("profile_url", ""),
                        "recorded_at": record.get("recorded_at", ""),
                        "attempts": record.get("attempt", ""),
                        "max_attempts": record.get("max_attempts", ""),
                    }
                    for key, record in state.get("targets", {}).items()
                    if record.get("status") == "terminal_error"
                },
                final_bridge=final_bridge,
                prospects_bridge=prospects_bridge,
            )
        else:
            # Lane runners defer Final/Prospects bridging, but their confirmed
            # activity decisions must still persist on the durable queue.
            update_batch_status(
                queue_fingerprint,
                "activity_in_progress",
                activity_results={
                    key: {
                        "activity_value": normalize_activity_value(
                            record.get("activity_value", "")
                        ),
                        "recorded_at": record.get("recorded_at", ""),
                    }
                    for key, record in state.get("targets", {}).items()
                    if normalize_activity_value(record.get("activity_value", ""))
                },
                activity_issues={
                    key: {
                        "terminal": True,
                        "terminal_reason": record.get("terminal_reason", ""),
                        "profile_url": record.get("profile_url", ""),
                        "recorded_at": record.get("recorded_at", ""),
                        "attempts": record.get("attempt", ""),
                        "max_attempts": record.get("max_attempts", ""),
                    }
                    for key, record in state.get("targets", {}).items()
                    if record.get("status") == "terminal_error"
                },
            )
        state["status"] = status
        state["completed_at"] = datetime.now().isoformat(timespec="seconds")
        state["completed_this_run"] = completed
        state["resume_skips"] = skipped_resume
        state["bridged_rows_count"] = bridged
        state["final_bridge_run"] = final_bridge
        state["prospects_bridge"] = prospects_bridge
        state["failures"] = failures
        save_session_state(date_value, state)
    emit_progress(date_value, "final", **result)
    return result


def fake_activity(
    *, posts: Sequence[str] = (), comments: Sequence[str] = (), reactions: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "tabs": {
            "posts": {"activities": [{"time_text": value} for value in posts]},
            "comments": {"activities": [{"time_text": value} for value in comments]},
            "reactions": {"activities": [{"time_text": value} for value in reactions]},
        }
    }


def run_self_tests() -> None:
    row = {
        "ID": "1",
        "Company": "Example",
        "Website": "https://example.com",
        "Company LinkedIn": "",
        "Emp Count": "10",
        "Source Tab": "",
        "Use": "yes",
        "P1 Name": "One",
        "P1 Title": "CEO",
        "P1 LinkedIn": "https://www.linkedin.com/in/one",
        "P1 Email": "",
        "P2 Name": "Two",
        "P2 Title": "COO",
        "P2 LinkedIn": "https://www.linkedin.com/in/two",
        "P2 Email": "",
        "P3 Name": "Three",
        "P3 LinkedIn": "https://www.linkedin.com/in/three",
        "_row_number": 2,
    }
    targets = extract_targets([row])
    assert [target["prefix"] for target in targets] == ["P1", "P2"]
    locale_row = {
        **row,
        "ID": "2",
        "P1 LinkedIn": "https://nl.linkedin.com/in/example-person/nl?trk=test",
        "P2 LinkedIn": "",
    }
    locale_targets = extract_targets([locale_row])
    assert len(locale_targets) == 1
    assert locale_targets[0]["profile_url"] == "https://www.linkedin.com/in/example-person"
    assert (
        locale_targets[0]["original_profile_url"]
        == "https://nl.linkedin.com/in/example-person/nl?trk=test"
    )
    assert locale_targets[0]["profile_url_normalized"] is True
    assert activity_level(fake_activity(posts=["6d"])) == "Very active"
    assert activity_level(fake_activity(comments=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(reactions=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(posts=["13d"])) == "Active"
    assert (
        activity_level(fake_activity(comments=["20d", "21d", "22d"], reactions=["23d", "24d"]))
        == "Active"
    )
    assert activity_level(fake_activity(comments=["300d"])) == "Not active"
    proven_recent_with_uncertain_tab = fake_activity(posts=["6d"])
    proven_recent_with_uncertain_tab["tabs"]["comments"]["activity_classification_uncertain"] = True
    assert activity_level(proven_recent_with_uncertain_tab) == "Very active"
    proven_active_with_uncertain_tab = fake_activity(
        comments=["20d", "21d", "22d"], reactions=["23d", "24d"]
    )
    proven_active_with_uncertain_tab["tabs"]["posts"]["activity_classification_uncertain"] = True
    assert activity_level(proven_active_with_uncertain_tab) == "Active"
    old_only_with_uncertain_tab = fake_activity(comments=["300d"])
    old_only_with_uncertain_tab["tabs"]["reactions"]["activity_classification_uncertain"] = True
    assert activity_level(old_only_with_uncertain_tab) == ""
    assert (
        activity_detail_empty_success_reason(old_only_with_uncertain_tab)
        == "activity_classification_uncertain"
    )
    canonical_example = "https://www.linkedin.com/in/example-person"
    assert (
        canonical_linkedin_profile_url("https://nl.linkedin.com/in/example-person/nl?trk=test")
        == canonical_example
    )
    assert (
        canonical_linkedin_profile_url("https://www.nl.linkedin.com/in/example-person/")
        == canonical_example
    )
    assert canonical_linkedin_profile_url("linkedin.com/in/example-person") == canonical_example
    assert (
        canonical_linkedin_profile_url("https://rs.linkedin.com/in/example-person#:~:text=Example")
        == canonical_example
    )
    assert (
        canonical_linkedin_profile_url(
            "https://www.linkedin.com/in/example-person/recent-activity/reactions/"
        )
        == canonical_example
    )
    assert canonical_linkedin_profile_url("https://example.com/in/example-person") == ""
    assert canonical_linkedin_profile_url("https://www.linkedin.com/company/example-person") == ""
    assert canonical_linkedin_profile_url("https://www.linkedin.com/jobs/view/123") == ""
    assert normalize_navigation_type("Selector-based") == "Selector-based"
    assert navigation_type_key("Direct Url") == "direct_url"
    assert navigation_type_key("selector based") == "selector_based"
    duplicate_batch_sizes = distribute_slots_to_batches(4, [1, 1, 2, 2, 3, 4, 5, 6], "7/6/2026")
    assert sum(duplicate_batch_sizes.values()) == 4
    assert all(size >= 0 for size in duplicate_batch_sizes.values())
    assert all(batch in duplicate_batch_sizes for batch in [1, 2, 3, 4, 5, 6])
    ranked = rank_row_for_final(row, {"P1": "Not active", "P2": "Very active"})
    assert ranked["P1 Name"] == "Two"
    assert ranked["P1 Activity"] == "Very active"
    assert ranked["Category"] == "Hyper"
    planned_keys = {target["key"] for target in targets}
    staged_state = {
        "targets": {
            targets[0]["key"]: {"activity_value": "Not active"},
            targets[1]["key"]: {"activity_value": "Very active"},
        }
    }
    staged_rows = ready_final_rows(
        source_rows=[row],
        state=staged_state,
        all_target_keys=planned_keys,
        planned_target_keys=planned_keys,
    )
    assert len(staged_rows) == 1
    assert staged_rows[0]["Category"] == "Hyper"
    assert final_source_row_count([row], planned_keys, planned_keys) == 1
    assert not ready_final_rows(
        source_rows=[row],
        state={"targets": {targets[0]["key"]: {"activity_value": "Not active"}}},
        all_target_keys=planned_keys,
        planned_target_keys=planned_keys,
    )
    blank = rank_row_for_final(row, {"P1": "", "P2": ""})
    assert blank["Category"] == ""
    ambiguous_empty_success = {
        "error": False,
        "tabs": {
            "posts": {"total_visible": 0, "activities": []},
            "reactions": {"total_visible": 0, "activities": []},
            "comments": {"total_visible": 0, "activities": []},
        },
    }
    assert (
        activity_detail_empty_success_reason(ambiguous_empty_success)
        == "activity_feed_not_hydrated"
    )
    invalid_empty_success = {**ambiguous_empty_success, "page_url": "https://www.linkedin.com/404/"}
    assert activity_detail_empty_success_reason(invalid_empty_success) == "invalid_profile_or_404"
    explicit_empty_success = {
        "error": False,
        "tabs": {
            "posts": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
            "reactions": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
            "comments": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
        },
    }
    assert activity_detail_empty_success_reason(explicit_empty_success) == ""
    assert activity_level(explicit_empty_success) == "Not active"
    reader = LiveActivityReader(timeout=1, retries=1)
    reader.connected = True

    class FakeSession:
        cdp = None

        def __init__(self) -> None:
            self.calls = 0

        def read_activity_detail(
            self, _profile_url: str, max_seconds: float, navigation_type: str
        ) -> dict[str, Any]:
            self.calls += 1
            return {
                "error": True,
                "danger": "invalid_profile_or_404",
                "reason": "invalid_profile_or_404",
            }

        def _check_danger(self) -> str:
            return ""

    fake_session = FakeSession()
    reader.session = fake_session
    retained_error = reader(
        "https://www.linkedin.com/in/missing", {"navigation_type": "Direct Url"}
    )
    assert retained_error.get("reason") == "invalid_profile_or_404"
    assert len(retained_error.get("_attempts", [])) == 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Pre-final P1/P2 activity, then bridge Final and Prospects in stages."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument(
        "--sheet-url", default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_LEADS_SHEET_URL)
    )
    parser.add_argument(
        "--prefinal-tab", default=os.environ.get("LEAD_RESEARCH_PREFINAL_TAB", DEFAULT_PREFINAL_TAB)
    )
    parser.add_argument(
        "--final-tab", default=os.environ.get("LEAD_RESEARCH_FINAL_TAB", DEFAULT_FINAL_TAB)
    )
    parser.add_argument("--obf-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--prospects-tab", default=os.environ.get("OBF_PROSPECTS_TAB", "Prospects"))
    parser.add_argument(
        "--activity-sequence-tab",
        default=os.environ.get("ACTIVITY_SEQUENCE_TAB", DEFAULT_ACTIVITY_SEQUENCE_TAB),
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--activity-timeout", type=float, default=180.0)
    parser.add_argument("--activity-retries", type=int, default=1)
    parser.add_argument("--max-consecutive-hard-failures", type=int, default=3)
    parser.add_argument(
        "--max-target-attempts",
        type=int,
        default=DEFAULT_MAX_TARGET_ATTEMPTS,
        help="Terminally skip one profile after this many unresolved Activity Check attempts (default: 4).",
    )
    parser.add_argument("--activity-fixture", default="")
    parser.add_argument(
        "--test-synthetic-activity",
        action="store_true",
        help="Test only: generate deterministic synthetic activity evidence; requires test destinations.",
    )
    parser.add_argument(
        "--queue-fingerprint",
        default="",
        help="Run one explicit queued batch instead of the oldest open batch.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--live-dry-run",
        action="store_true",
        help="In dry-run mode, still read LinkedIn live activity.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare Activity Sequence/local session state, then exit before LinkedIn reads or Final writes.",
    )
    parser.add_argument(
        "--activity-only",
        action="store_true",
        help="Run only from an existing prepared local session; do not regenerate Activity Sequence.",
    )
    parser.add_argument(
        "--worker-id", default="", help="Run only targets assigned to one prepared activity worker."
    )
    parser.add_argument(
        "--retry-pending-404",
        action="store_true",
        help="Fresh-session retry of only targets deferred after an initial 404-like result.",
    )
    parser.add_argument(
        "--no-bridges",
        action="store_true",
        help="Record this lane only; defer Final/Prospects bridges to a later finalizer.",
    )
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="Bridge the prepared session without opening LinkedIn or running activity reads.",
    )
    parser.add_argument(
        "--test-stop-cdp-after-completed",
        type=int,
        default=0,
        help="E2E test only: cleanly close this worker's Chrome after N completed targets.",
    )
    parser.add_argument(
        "--enqueue-current-prefinal",
        action="store_true",
        help="One-time bootstrap: snapshot the currently visible Pre-final rows into the local queue before activity work.",
    )
    parser.add_argument(
        "--final-bridge-threshold",
        type=float,
        default=0.90,
        help="Minimum resolved activity share required before the staged Final bridge (default: 0.90).",
    )
    parser.add_argument(
        "--prospects-bridge-delay-sec",
        type=int,
        default=300,
        help="Delay after a successful Final bridge before Final -> Prospects (default: 300).",
    )
    parser.add_argument(
        "--no-prospects-bridge", action="store_true", help="Stop after the staged Final bridge."
    )
    parser.add_argument("--delay-min-sec", type=int, default=5)
    parser.add_argument("--delay-max-sec", type=int, default=20)
    parser.add_argument("--batch-gap-min-sec", type=int, default=10)
    parser.add_argument("--batch-gap-max-sec", type=int, default=45)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.final_bridge_threshold <= 1:
        parser.error("--final-bridge-threshold must be between 0 and 1.")
    if args.prospects_bridge_delay_sec < 0:
        parser.error("--prospects-bridge-delay-sec must be >= 0.")
    if args.max_target_attempts < 1:
        parser.error("--max-target-attempts must be >= 1.")
    if args.worker_id and not args.activity_only:
        parser.error(
            "--worker-id requires --activity-only so the prepared assignment cannot change."
        )
    if args.finalize_only and not args.activity_only:
        parser.error("--finalize-only requires --activity-only.")
    if args.finalize_only and args.worker_id:
        parser.error("--finalize-only cannot target a single worker.")
    if args.finalize_only and args.no_bridges:
        parser.error("--finalize-only cannot be combined with --no-bridges.")
    if args.retry_pending_404 and (not args.activity_only or args.finalize_only):
        parser.error(
            "--retry-pending-404 requires --activity-only and cannot be used with --finalize-only."
        )
    if args.test_stop_cdp_after_completed < 0:
        parser.error("--test-stop-cdp-after-completed must be >= 0.")
    if args.test_stop_cdp_after_completed and not args.worker_id:
        parser.error("--test-stop-cdp-after-completed requires --worker-id.")
    if args.test_synthetic_activity and (
        not args.final_tab.endswith(" - Test") or not args.prospects_tab.endswith(" - Test")
    ):
        parser.error(
            "--test-synthetic-activity requires --final-tab and --prospects-tab ending in ' - Test'."
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("check_prefinal_activity self-tests passed")
        return 0
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    result = run(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
