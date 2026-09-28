"""The activity-check orchestrator (run()).

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S13). Pure move.
"""

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.activity_check.analysis import (
    activity_detail_empty_success_reason,
    activity_evidence,
    activity_level,
    detail_contains_text,
)
from outbound.activity_check.config import (
    BASE_COLUMNS,
    DEFER_ACTIVITY_REASONS,
    P1_COLUMNS,
    P2_COLUMNS,
    PENDING_404_STATUS,
    SKIP_ACTIVITY_REASONS,
)
from outbound.activity_check.danger import (
    blocked_result,
    hard_read_failure_reason,
    is_blocking_danger,
    is_cdp_transport_exception,
)
from outbound.activity_check.diversion import apply_diversion
from outbound.activity_check.finalize import (
    bridge_final_to_prospects,
    bridge_ready_rows_to_final,
    final_source_row_count,
    finalize_prepared_session,
    no_queued_batch_result,
    queue_source_rows,
    ready_final_rows,
    resume_final_bridged_batch,
)
from outbound.activity_check.reader import (
    LiveActivityReader,
    build_fixture_reader,
    build_synthetic_activity_reader,
)
from outbound.activity_check.retry import (
    next_activity_retry_record,
    persist_activity_retry_attempt,
    persist_terminal_activity_issue,
)
from outbound.activity_check.sequence import (
    ensure_activity_sequence,
)
from outbound.activity_check.sheets import read_worksheet
from outbound.activity_check.state import (
    activity_journal_path,
    activity_session_path,
    emit_progress,
    journal_event,
    load_session_state,
    prepare_session_state,
    save_session_state,
)
from outbound.activity_check.targets import extract_targets
from outbound.activity_check.text import (
    canonical_linkedin_profile_url,
    clean_text,
    normalize_activity_value,
)
from outbound.shared.dates import sheet_date
from outbound.shared.queue import (
    enqueue_batch,
    load_batch,
    next_activity_batch,
    update_batch_status,
)
from outbound.shared.sheets import require_columns


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
