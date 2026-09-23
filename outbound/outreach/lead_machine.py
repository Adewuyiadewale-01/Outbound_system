"""The per-prospect lead state machine: inspect -> decide -> act -> report -> divert.

Runs one prospect through the full outreach flow, firing the guard cluster on
failures, writing the three-sheet report bundle journal-first with tiered
failure semantics, and executing the runtime plan's diversions and delays.
Returns loop-control signals ("continue"/"break") to the run loop.
"""

from __future__ import annotations

import argparse
import random
import time
from typing import Any

from outreach_helper import (
    apply_prospect_fields,
    build_connection_sent_fields,
    build_outreach_log_row,
    build_template_variables,
    insert_outreach_log_row,
    pick_connection_template,
    render_template,
)

from outbound.outreach.activity import (
    _activity_summary,
    _read_and_sync_activity,
    _reconcile_profile_state,
    _record_requires_email_status,
    _short_skip_dwell,
)
from outbound.outreach.control_sheet import _update_outreach_control_progress
from outbound.outreach.guards import (
    _ensure_session_healthy,
    _normalize_failure_key,
    _record_early_profile_guard,
    _register_browser_failure,
    _register_send_failure,
    _reset_browser_failure_guard,
    _reset_send_failure_guard,
    _sleep_failure_backoff,
)
from outbound.outreach.journal import (
    _journal_confirmed_send_records,
    _record_breadcrumb,
    _record_journal_event,
    _sheet_target_payload,
)
from outbound.outreach.planning import _normalize_activity_timing
from outbound.outreach.policy import (
    EMAIL_REQUIRED_TO_CONNECT,
    HARD_STOP_ERRORS,
    NON_FATAL_SEND_ERRORS,
    OUTREACH_CONTROL_TAB,
    PROFILE_UNKNOWN_RETRIES,
    PROFILE_UNKNOWN_RETRY_MAX_SEC,
    PROFILE_UNKNOWN_RETRY_MIN_SEC,
)
from outbound.shared.diversion import _first_post_url, _run_diversion
from outbound.shared.sheetutils import _parse_int


def _process_outreach_lead_state_machine(
    *,
    result: dict[str, Any],
    session: Any,
    prospect: dict[str, Any],
    plan_item: dict[str, Any],
    index: int,
    selected_count: int,
    date_value: str,
    creds: str,
    args: argparse.Namespace,
    templates: list[dict[str, Any]],
    conn_req_row: dict[str, Any],
    starting_progress: int,
    target: int,
) -> str:
    """Run one prospect through inspect -> decide -> act -> report -> divert."""
    from linkedin_outreach_session import _recover_confirmed_send_sheet_sync

    timing = _normalize_activity_timing(plan_item.get("activity_log_timing"))
    profile_url = prospect.get("contact_linkedin", "")
    lead_started_at = time.time()
    is_last = index >= (selected_count - 1)
    activity: dict[str, Any] = {}
    activity_summary = ""

    _record_breadcrumb(
        result, date_value, prospect, "lead_start", lead_started_at, profile_url=profile_url
    )
    if not profile_url:
        result["skipped"].append(
            {"prospect_id": prospect.get("id"), "reason": "missing_linkedin_url"}
        )
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "lead_done",
            lead_started_at,
            status="skipped",
            error="missing_linkedin_url",
        )
        return "continue"

    try:
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "profile_inspect_start",
            lead_started_at,
            profile_url=profile_url,
        )
        profile_state = session.inspect_profile_action_state(profile_url)
        live_state = str(profile_state.get("state", "unknown") or "unknown")
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "profile_state_detected",
            lead_started_at,
            profile_url=profile_url,
            live_state=live_state,
            status=live_state,
        )
        for retry_attempt in range(1, PROFILE_UNKNOWN_RETRIES + 1):
            if live_state != "unknown":
                break
            retry_wait = random.uniform(
                PROFILE_UNKNOWN_RETRY_MIN_SEC, PROFILE_UNKNOWN_RETRY_MAX_SEC
            )
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "profile_inspect_retry_start",
                lead_started_at,
                profile_url=profile_url,
                retry_attempt=retry_attempt,
                retry_wait_sec=round(retry_wait, 1),
                previous_error=profile_state.get("error"),
                status="retrying_unknown",
            )
            time.sleep(retry_wait)
            try:
                retry_state = session.inspect_profile_action_state(profile_url)
                retry_live_state = str(retry_state.get("state", "unknown") or "unknown")
            except Exception as retry_exc:
                retry_live_state = "unknown"
                retry_state = {
                    "state": "unknown",
                    "error": f"retry_exception:{_normalize_failure_key(retry_exc)}",
                }
                result.setdefault("browser_failures", []).append(
                    {
                        "prospect_id": prospect.get("id"),
                        "step": "profile_inspect_retry",
                        "error": str(retry_exc),
                    }
                )
            profile_state = retry_state
            live_state = retry_live_state
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "profile_state_detected",
                lead_started_at,
                profile_url=profile_url,
                live_state=live_state,
                retry_attempt=retry_attempt,
                status=live_state,
            )
        if live_state != "unknown":
            _reset_browser_failure_guard(result)
        trip_reason = _record_early_profile_guard(
            result,
            prospect.get("id"),
            live_state,
            profile_state,
            selected_count,
        )
        if trip_reason:
            result["ok"] = False
            result["status"] = "blocked_early_profile_guard"
            result["blockers"].append(trip_reason)
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "lead_done",
                lead_started_at,
                status="blocked_early_profile_guard",
                error=trip_reason,
            )
            return "break"
    except Exception as exc:
        failure_key = _normalize_failure_key(exc)
        result.setdefault("browser_failures", []).append(
            {"prospect_id": prospect.get("id"), "step": "profile_inspect", "error": str(exc)}
        )
        result["skipped"].append(
            {
                "prospect_id": prospect.get("id"),
                "reason": f"profile_inspect_exception:{failure_key}",
            }
        )
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "lead_done",
            lead_started_at,
            status="skipped",
            error=f"profile_inspect_exception:{failure_key}",
        )
        trip_reason = _register_browser_failure(
            result, prospect.get("id"), failure_key, "profile_inspect"
        )
        if trip_reason:
            result["ok"] = False
            result["status"] = "blocked_browser_guard"
            result["blockers"].append(trip_reason)
            return "break"
        _ensure_session_healthy(
            result=result,
            session=session,
            label=f"profile_inspect_exception:{prospect.get('id')}",
            args=args,
        )
        if result.get("status") == "blocked_browser_health":
            return "break"
        _sleep_failure_backoff(result, is_last=is_last)
        return "continue"

    if timing == "before_conn" and live_state not in {"profile_unavailable", "unknown"}:
        try:
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "activity_before_start",
                lead_started_at,
                profile_url=profile_url,
            )
            activity, activity_summary, _activity_fields = _read_and_sync_activity(
                result=result,
                session=session,
                prospect=prospect,
                profile_url=profile_url,
                date_value=date_value,
                creds=creds,
                obf_url=args.obf_url,
                write_phase="before_conn",
            )
        except Exception as exc:
            failure_key = _normalize_failure_key(exc)
            result.setdefault("browser_failures", []).append(
                {
                    "prospect_id": prospect.get("id"),
                    "step": "before_conn_profile_activity",
                    "error": str(exc),
                }
            )
            if failure_key in HARD_STOP_ERRORS:
                result["ok"] = False
                result["status"] = "blocked_circuit_breaker"
                result["blockers"].append(f"before_conn_profile_activity exception: {exc}")
                return "break"
            activity = {
                "error": True,
                "danger": "activity_read_exception",
                "exception": failure_key,
            }
            activity_summary = f"activity_read=activity_read_exception; reason={failure_key}"
            _record_journal_event(
                result,
                date_value,
                "prospect_activity_sync",
                "skipped_exception",
                prospect=prospect,
                write_phase="before_conn",
                activity_summary=activity_summary,
            )
            result.setdefault("warnings", []).append(
                f"Activity read failed {timing} before send for prospect {prospect.get('id')}; no activity field was written"
            )
            _ensure_session_healthy(
                result=result,
                session=session,
                label=f"before_conn_activity_exception:{prospect.get('id')}",
                args=args,
            )
            if result.get("status") == "blocked_browser_health":
                return "break"

    if live_state in {"already_pending", "already_connected"}:
        reconciled = _reconcile_profile_state(
            result=result,
            prospect=prospect,
            state=live_state,
            date_value=date_value,
            creds=creds,
            obf_url=args.obf_url,
        )
        _recover_confirmed_send_sheet_sync(
            result=result,
            prospect=prospect,
            date_value=date_value,
            creds=creds,
            args=args,
        )
        result["reconciled"].append(
            {
                "prospect_id": prospect.get("id"),
                "state": live_state,
                "synced": bool(reconciled.get("synced")),
            }
        )
        result["skipped"].append(
            {"prospect_id": prospect.get("id"), "reason": f"{live_state}_reconciled"}
        )
        _short_skip_dwell()
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "lead_done",
            lead_started_at,
            status=f"{live_state}_reconciled",
        )
        return "continue"

    if live_state in {"profile_unavailable", "unknown", "no_connect_button"}:
        _record_journal_event(
            result,
            date_value,
            "lead_skip",
            "recorded",
            prospect=prospect,
            reason=live_state,
            profile_state=profile_state,
        )
        result["skipped"].append({"prospect_id": prospect.get("id"), "reason": live_state})
        _short_skip_dwell()
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "lead_done",
            lead_started_at,
            status="skipped",
            error=live_state,
        )
        return "continue"

    note = None
    if not args.no_notes:
        template = pick_connection_template(prospect, templates=templates, credentials_path=creds)
        if template:
            note = render_template(template, build_template_variables(prospect))

    try:
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "send_start",
            lead_started_at,
            profile_url=profile_url,
            live_state=live_state,
        )
        send_result = session.send_connection_only(
            profile_url, profile_state=profile_state, note=note
        )
    except Exception as exc:
        failure_key = _normalize_failure_key(exc)
        result.setdefault("warnings", []).append(f"send_connection_only exception: {failure_key}")
        result.setdefault("browser_failures", []).append(
            {"prospect_id": prospect.get("id"), "step": "send_connection_only", "error": str(exc)}
        )
        result["skipped"].append(
            {"prospect_id": prospect.get("id"), "reason": f"send_exception:{failure_key}"}
        )
        if failure_key == EMAIL_REQUIRED_TO_CONNECT:
            _record_requires_email_status(
                result=result,
                prospect=prospect,
                date_value=date_value,
                creds=creds,
                obf_url=args.obf_url,
            )
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "lead_done",
                lead_started_at,
                status="skipped",
                error=failure_key,
            )
            _reset_send_failure_guard(result)
            _sleep_failure_backoff(result, is_last=is_last)
            return "continue"
        trip_reason = _register_send_failure(result, prospect.get("id"), failure_key)
        if trip_reason:
            result["ok"] = False
            result["status"] = "blocked_failure_guard"
            result["blockers"].append(trip_reason)
            return "break"
        _sleep_failure_backoff(result, is_last=is_last)
        return "continue"

    send_result["prospect_id"] = prospect.get("id")
    result["sent"].append(send_result)
    if send_result.get("error") in HARD_STOP_ERRORS:
        result["ok"] = False
        result["blockers"].append(str(send_result["error"]))
        result["status"] = "blocked_circuit_breaker"
        return "break"

    if not send_result.get("success"):
        send_error = _normalize_failure_key(send_result.get("error", "send_failed"))
        result["skipped"].append({"prospect_id": prospect.get("id"), "reason": send_error})
        if send_error == EMAIL_REQUIRED_TO_CONNECT:
            _record_requires_email_status(
                result=result,
                prospect=prospect,
                date_value=date_value,
                creds=creds,
                obf_url=args.obf_url,
            )
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "lead_done",
            lead_started_at,
            status="skipped",
            error=send_error,
        )
        if send_error in NON_FATAL_SEND_ERRORS:
            _reset_send_failure_guard(result)
            _sleep_failure_backoff(result, is_last=is_last)
            return "continue"
        trip_reason = _register_send_failure(result, prospect.get("id"), send_error)
        if trip_reason:
            result["ok"] = False
            result["status"] = "blocked_failure_guard"
            result["blockers"].append(trip_reason)
            return "break"
        _sleep_failure_backoff(result, is_last=is_last)
        return "continue"

    result["successful_sends"] += 1
    _reset_send_failure_guard(result)
    _record_breadcrumb(
        result,
        date_value,
        prospect,
        "send_confirmed",
        lead_started_at,
        profile_url=profile_url,
        status="sent",
    )
    _record_journal_event(
        result,
        date_value,
        "connection_request_confirmed",
        "recorded",
        prospect=prospect,
        touch_method="LinkedIn",
        outcome="Pending",
        profile_url=profile_url,
    )

    if timing != "before_conn":
        try:
            _record_breadcrumb(
                result,
                date_value,
                prospect,
                "activity_after_start",
                lead_started_at,
                profile_url=profile_url,
            )
            activity, activity_summary, _activity_fields = _read_and_sync_activity(
                result=result,
                session=session,
                prospect=prospect,
                profile_url=profile_url,
                date_value=date_value,
                creds=creds,
                obf_url=args.obf_url,
                write_phase="after_conn",
            )
        except Exception as exc:
            failure_key = _normalize_failure_key(exc)
            result.setdefault("browser_failures", []).append(
                {
                    "prospect_id": prospect.get("id"),
                    "step": "after_conn_profile_activity",
                    "error": str(exc),
                }
            )
            activity = {
                "error": True,
                "danger": "activity_read_exception",
                "exception": failure_key,
            }
            activity_summary = f"activity_read=activity_read_exception; reason={failure_key}"
            _record_journal_event(
                result,
                date_value,
                "prospect_activity_sync",
                "skipped_exception",
                prospect=prospect,
                write_phase="after_conn",
                activity_summary=activity_summary,
            )
            result.setdefault("warnings", []).append(
                f"Activity read failed after confirmed send for prospect {prospect.get('id')}; reporting send without activity field"
            )
    elif not activity_summary:
        activity_summary = _activity_summary(activity)

    if activity.get("is_active"):
        result["engagement_opportunities"].append(
            {
                "prospect_id": prospect.get("id"),
                "company": prospect.get("company"),
                "contact": prospect.get("contact_name"),
                "post_url": _first_post_url(activity),
                "reason": "Recent LinkedIn activity found",
            }
        )

    if timing == "before_conn":
        log_notes = (
            f"activity_timing=instant; {activity_summary}"
            if activity_summary
            else "activity_timing=instant"
        )
    elif timing == "after_conn":
        log_notes = (
            f"activity_timing=post_conn; {activity_summary}"
            if activity_summary
            else "activity_timing=post_conn"
        )
    else:
        log_notes = activity_summary

    prospect_fields = build_connection_sent_fields(prospect=prospect, touch_method="LinkedIn")
    outreach_log_row = build_outreach_log_row(
        prospect_id=str(prospect.get("id", "")).strip(),
        company=str(prospect.get("company", "")).strip(),
        person_engaged=str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1",
        contact_name=str(prospect.get("contact_name", "")).strip(),
        action="Connection Request",
        touch_method="LinkedIn",
        outcome="Pending",
        notes=log_notes,
    )
    row_number = int(conn_req_row["_row_number"])
    journal_confirmed_count = len(_journal_confirmed_send_records(date_value))
    new_progress = max(starting_progress + result["successful_sends"], journal_confirmed_count)
    progress_value = target if new_progress >= target else new_progress
    progress_notes = f"8:30 LinkedIn outreach sent {new_progress}/{target}"
    control_progress_fields = {
        "Status": "Done" if new_progress >= target else "Partial",
        "Current Progress": str(progress_value),
        "Effective Target": str(target),
        "Notes": progress_notes,
    }

    _record_breadcrumb(
        result,
        date_value,
        prospect,
        "report_start",
        lead_started_at,
        profile_url=profile_url,
        status="pending_sheet_sync",
    )
    _record_journal_event(
        result,
        date_value,
        "lead_reporting_bundle",
        "recorded",
        prospect=prospect,
        sheet_payloads={
            "prospects": _sheet_target_payload(
                target="Prospects", row_number=int(prospect["_row_number"]), fields=prospect_fields
            ),
            "outreach_log": _sheet_target_payload(
                target="Outreach Log", fields=outreach_log_row, insert_mode="below_header"
            ),
            "outreach_control": _sheet_target_payload(
                target=OUTREACH_CONTROL_TAB, row_number=row_number, fields=control_progress_fields
            ),
        },
    )
    try:
        apply_prospect_fields(
            prospect_row=int(prospect["_row_number"]),
            fields=prospect_fields,
            credentials_path=creds,
            sheet_url=args.obf_url,
        )
        result.setdefault("prospect_updates", []).append(
            {"updated": True, "row": int(prospect["_row_number"]), "fields": prospect_fields}
        )
        _record_journal_event(
            result,
            date_value,
            "prospects_sync",
            "synced",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target="Prospects", row_number=int(prospect["_row_number"]), fields=prospect_fields
            ),
        )
    except Exception as exc:
        _record_journal_event(
            result,
            date_value,
            "prospects_sync",
            "pending_sync",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target="Prospects", row_number=int(prospect["_row_number"]), fields=prospect_fields
            ),
            error=str(exc),
        )
        result["sync_failures"].append(
            {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
        )
        result["ok"] = False
        result["blockers"].append(
            f"Prospects sync failed after successful send for prospect {prospect.get('id')}: {exc}"
        )
        result["status"] = "blocked_prospect_sync"
        return "break"

    try:
        insert_outreach_log_row(
            row_data=outreach_log_row, credentials_path=creds, sheet_url=args.obf_url
        )
        _record_journal_event(
            result,
            date_value,
            "outreach_log_sync",
            "synced",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target="Outreach Log", fields=outreach_log_row, insert_mode="below_header"
            ),
        )
    except Exception as exc:
        _record_journal_event(
            result,
            date_value,
            "outreach_log_sync",
            "pending_sync",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target="Outreach Log", fields=outreach_log_row, insert_mode="below_header"
            ),
            error=str(exc),
        )
        result["sync_failures"].append(
            {"prospect_id": prospect.get("id"), "target": "Outreach Log", "error": str(exc)}
        )

    _record_journal_event(
        result,
        date_value,
        "outreach_control_sync",
        "recorded",
        prospect=prospect,
        sheet_payload=_sheet_target_payload(
            target=OUTREACH_CONTROL_TAB, row_number=row_number, fields=control_progress_fields
        ),
    )
    try:
        result["outreach_control_update"] = _update_outreach_control_progress(
            creds, args.obf_url, row_number, progress_value, target, progress_notes
        )
        _record_journal_event(
            result,
            date_value,
            "outreach_control_sync",
            "synced",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target=OUTREACH_CONTROL_TAB, row_number=row_number, fields=control_progress_fields
            ),
        )
    except Exception as exc:
        _record_journal_event(
            result,
            date_value,
            "outreach_control_sync",
            "pending_sync",
            prospect=prospect,
            sheet_payload=_sheet_target_payload(
                target=OUTREACH_CONTROL_TAB, row_number=row_number, fields=control_progress_fields
            ),
            error=str(exc),
        )
        result["sync_failures"].append(
            {"prospect_id": prospect.get("id"), "target": OUTREACH_CONTROL_TAB, "error": str(exc)}
        )
        result["ok"] = False
        result["blockers"].append(
            f"{OUTREACH_CONTROL_TAB} sync failed after successful send for prospect {prospect.get('id')}: {exc}"
        )
        result["status"] = "blocked_outreach_control_sync"
        return "break"

    _record_breadcrumb(
        result,
        date_value,
        prospect,
        "report_synced",
        lead_started_at,
        profile_url=profile_url,
        status="synced",
    )

    diversion = str(plan_item.get("lead_diversion", "none")).strip().lower()
    diversion_sec = _parse_int(plan_item.get("lead_diversion_sec"))
    if diversion and diversion != "none":
        _record_breadcrumb(
            result,
            date_value,
            prospect,
            "diversion_start",
            lead_started_at,
            profile_url=profile_url,
            diversion=diversion,
            diversion_sec=diversion_sec,
        )
        diversion_result = _run_diversion(
            session=session,
            diversion=diversion,
            diversion_sec=diversion_sec,
            activity=activity,
            company_linkedin=str(prospect.get("company_linkedin") or ""),
        )
        if diversion_result.get("executed"):
            result["runtime_enforcement"]["diversions_executed"] += 1
        if not diversion_result.get("ok", True):
            result["runtime_enforcement"]["diversion_failures"].append(
                {
                    "prospect_id": prospect.get("id"),
                    "error": diversion_result.get("error", "diversion_failed"),
                    "diversion": diversion,
                }
            )

    if not is_last:
        delay_sec = max(0, _parse_int(plan_item.get("delay_sec")) or 0)
        if delay_sec > 0:
            time.sleep(delay_sec)
            result["runtime_enforcement"]["delays_applied"] += 1
            result["runtime_enforcement"]["delay_seconds_total"] += delay_sec

    _record_breadcrumb(
        result,
        date_value,
        prospect,
        "lead_done",
        lead_started_at,
        profile_url=profile_url,
        status="sent",
    )
    return "continue"
