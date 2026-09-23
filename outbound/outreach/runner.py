"""The outreach orchestrators: prep, run, and their support machinery.

prepare_8_30_session freezes the day's queue (lanes, workers, runtime plan);
run executes it through the lead machine under the guard cluster. Sheet
identity and weekend gates fire before either touches a browser or sheet.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from outreach_helper import (
    apply_prospect_fields,
    insert_outreach_log_row,
    load_prospect_queue,
    load_templates,
)
from sheets_helper import daily_approval_state, get_daily_group_data, safe_number

from outbound.outreach.control_sheet import (
    _control_lane_targets,
    _control_prospects_start_row,
    _extract_target,
    _find_conn_req_row,
    _read_outreach_control,
    _update_outreach_control_progress,
)
from outbound.outreach.guards import (
    _ensure_session_healthy,
    _outreach_ui_sample_size,
    _run_connection_modal_checks,
    _run_outreach_ui_preflight,
)
from outbound.outreach.journal import (
    _emit_outreach_progress,
    _journal_has_confirmed_send,
    _pending_sync_payload_for_confirmed_send,
    _reconcile_result_from_journal,
    _record_journal_event,
    journal_path,
    prepared_session_path,
    sequence_date_key,
)
from outbound.outreach.lanes import (
    _assign_outreach_workers,
    _auto_assign_missing_primary_lanes,
    _persist_primary_lane_assignments,
    _select_queue_for_lane_targets,
)
from outbound.outreach.lead_machine import _process_outreach_lead_state_machine
from outbound.outreach.paths import OBF_SHEET_URL, STATE_DIR
from outbound.outreach.planning import (
    _runtime_plan_from_generated_sequence,
    load_or_generate_sequence,
)
from outbound.outreach.policy import (
    DAILY_CONN_REQ_LIMIT,
    FAILURE_BACKOFF_MAX_SEC,
    FAILURE_BACKOFF_MIN_SEC,
    MAX_CONSECUTIVE_SEND_FAILURES,
    MAX_SAME_BROWSER_ERROR_STREAK,
    MAX_SAME_SEND_ERROR_STREAK,
    OUTREACH_CONTROL_TAB,
    OUTREACH_SEQUENCE_TAB,
    QUEUE_BUFFER_LIMIT,
    WEEKLY_CONN_REQ_LIMIT,
)
from outbound.outreach.sequence_sheet import (
    _load_runtime_plan_from_sequence_sheet,
    generate_activity_timing_from_sheet,
    generate_batch_sizes_from_sheet,
    generate_delay_seconds_from_sheet,
    generate_lead_diversion_seconds_from_sheet,
    generate_lead_diversions_from_sheet,
)
from outbound.shared.dates import _parse_date, sheet_date
from outbound.shared.sheetutils import _normalize_profile_url


def _extract_sheet_id(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", str(sheet_url).strip())
    if not match:
        raise ValueError(f"Could not extract spreadsheet id from URL: {sheet_url}")
    return match.group(1)


def _verify_sheet_url_identity(actual_url: str, expected_url: str, label: str) -> None:
    actual_id = _extract_sheet_id(actual_url)
    expected_id = _extract_sheet_id(expected_url)
    if actual_id != expected_id:
        raise ValueError(
            f"{label} identity mismatch: expected sheet id {expected_id} but got {actual_id}"
        )


def _is_obf_weekend(date_value: str) -> bool:
    """Return whether an OBF date is Saturday or Sunday.

    This guard belongs in the runner as well as the watcher: a dashboard or
    terminal invocation must not be able to bypass the weekday schedule.
    """
    return _parse_date(date_value).weekday() >= 5


def _weekend_hold_message(date_value: str) -> str:
    return f"OBF is held on weekends ({date_value}); no preparation or outreach is allowed."


def _recover_confirmed_send_sheet_sync(
    *,
    result: dict[str, Any],
    prospect: dict[str, Any],
    date_value: str,
    creds: str,
    args: argparse.Namespace,
) -> None:
    """Replay only sheet syncs that the local journal proves were pending."""
    prospects_payload = _pending_sync_payload_for_confirmed_send(
        date_value, prospect, "prospects_sync"
    )
    if (
        prospects_payload
        and prospects_payload.get("fields")
        and prospects_payload.get("row_number")
    ):
        try:
            apply_prospect_fields(
                prospect_row=int(prospects_payload["row_number"]),
                fields=prospects_payload["fields"],
                credentials_path=creds,
                sheet_url=args.obf_url,
            )
            _record_journal_event(
                result,
                date_value,
                "prospects_sync",
                "synced",
                prospect=prospect,
                recovered=True,
                sheet_payload=prospects_payload,
            )
        except Exception as exc:
            result["sync_failures"].append(
                {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
            )

    outreach_payload = _pending_sync_payload_for_confirmed_send(
        date_value, prospect, "outreach_log_sync"
    )
    if outreach_payload and outreach_payload.get("fields"):
        try:
            insert_outreach_log_row(
                row_data=outreach_payload["fields"],
                credentials_path=creds,
                sheet_url=args.obf_url,
            )
            _record_journal_event(
                result,
                date_value,
                "outreach_log_sync",
                "synced",
                prospect=prospect,
                recovered=True,
                sheet_payload=outreach_payload,
            )
        except Exception as exc:
            result["sync_failures"].append(
                {"prospect_id": prospect.get("id"), "target": "Outreach Log", "error": str(exc)}
            )

    control_payload = _pending_sync_payload_for_confirmed_send(
        date_value, prospect, "outreach_control_sync"
    )
    if control_payload and control_payload.get("fields") and control_payload.get("row_number"):
        fields = control_payload["fields"]
        row_number = int(control_payload["row_number"])
        progress_value = int(safe_number(fields.get("Current Progress", ""), 0) or 0)
        target_value = int(
            safe_number(fields.get("Effective Target", ""), progress_value) or progress_value
        )
        progress_notes = str(fields.get("Notes", "")).strip()
        try:
            result["outreach_control_update"] = _update_outreach_control_progress(
                creds,
                args.obf_url,
                row_number,
                progress_value,
                target_value,
                progress_notes,
            )
            _record_journal_event(
                result,
                date_value,
                "outreach_control_sync",
                "synced",
                prospect=prospect,
                recovered=True,
                sheet_payload=control_payload,
            )
        except Exception as exc:
            result["sync_failures"].append(
                {
                    "prospect_id": prospect.get("id"),
                    "target": OUTREACH_CONTROL_TAB,
                    "error": str(exc),
                }
            )


def _read_json(path: str | None) -> Any | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_prepared_session(date_value: str, prepared_path: str | None = None) -> dict[str, Any]:
    path = Path(prepared_path).expanduser() if prepared_path else prepared_session_path(date_value)
    if not path.exists():
        raise ValueError(f"Prepared session file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Prepared session payload is malformed")
    if not bool(payload.get("ready")):
        raise ValueError("Prepared session is not ready")

    prepared_date = str(payload.get("date", "")).strip()
    if prepared_date:
        if sequence_date_key(prepared_date) != sequence_date_key(date_value):
            raise ValueError(
                f"Prepared session date mismatch: expected {sheet_date(date_value)} got {sheet_date(prepared_date)}"
            )

    queue = payload.get("queue", [])
    runtime_plan = payload.get("runtime_plan", [])
    if not isinstance(queue, list) or not queue:
        raise ValueError("Prepared session queue is empty")
    if not isinstance(runtime_plan, list) or not runtime_plan:
        raise ValueError("Prepared session runtime plan is empty")

    return {
        "path": str(path),
        "prepared": payload,
        "queue": queue,
        "runtime_plan": runtime_plan,
    }


def _prospect_reporting_missing_fields(prospect: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    base_required = [
        ("id", "ID"),
        ("company", "Company"),
        ("website", "Website"),
        ("emp_count", "Emp Count"),
    ]
    for key, label in base_required:
        if not str(prospect.get(key, "")).strip():
            missing.append(label)

    engaged_person = str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1"
    if engaged_person not in {"Person 1", "Person 2"}:
        missing.append("Engaged Person")

    person_required = [
        ("contact_name", f"{engaged_person} Name"),
        ("contact_title", f"{engaged_person} Title"),
        ("contact_linkedin", f"{engaged_person} LinkedIn"),
    ]
    for key, label in person_required:
        if not str(prospect.get(key, "")).strip():
            missing.append(label)

    return missing


def _approval_and_task(
    creds: str,
    task_manager_url: str,
    date_value: str,
    mock_daily: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    if mock_daily:
        approval = mock_daily.get("approval_state", {})
        task_rows = mock_daily.get("task_rows", [])
    else:
        approval = daily_approval_state(creds, task_manager_url, date_value)
        task_rows = get_daily_group_data(creds, task_manager_url, date_value)["task_rows"]

    if not approval.get("approved"):
        return approval, {}, 0, 0

    conn_req_row = _find_conn_req_row(task_rows)
    if not conn_req_row:
        raise ValueError("No conn_req Daily Actions row found for today.")

    target = _extract_target(conn_req_row)
    current_progress = int(safe_number(conn_req_row.get("Current Progress", ""), 0))
    remaining = max(0, target - current_progress)
    return approval, conn_req_row, target, remaining


def _quota_capacity(quotas: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    today_remaining = int(quotas.get("conn_req_limit", DAILY_CONN_REQ_LIMIT)) - int(
        quotas.get("conn_req_today", 0)
    )
    week_remaining = int(quotas.get("conn_req_week_limit", WEEKLY_CONN_REQ_LIMIT)) - int(
        quotas.get("conn_req_week", 0)
    )
    capacity = max(0, min(today_remaining, week_remaining))
    return capacity, {
        "today_remaining": max(0, today_remaining),
        "week_remaining": max(0, week_remaining),
    }


def _load_quotas(mock_quotas: dict[str, Any] | None, dry_run: bool) -> dict[str, Any]:
    if mock_quotas:
        return mock_quotas
    if dry_run:
        return {
            "conn_req_today": 0,
            "conn_req_limit": DAILY_CONN_REQ_LIMIT,
            "conn_req_week": 0,
            "conn_req_week_limit": WEEKLY_CONN_REQ_LIMIT,
            "profile_views_today": 0,
        }
    from linkedin_helper import LinkedInSession

    return LinkedInSession().get_quotas()


def _load_queue(
    creds: str,
    obf_url: str,
    mock_queue: Any | None,
    shuffle: bool = True,
    start_row: int | None = None,
    prospects_tab: str = "Prospects",
    limit: int = QUEUE_BUFFER_LIMIT,
) -> list[dict[str, Any]]:
    if mock_queue:
        if isinstance(mock_queue, list):
            queue = mock_queue
        else:
            queue = mock_queue.get("queue", [])
        if start_row:
            queue = [row for row in queue if int(row.get("_row_number", 0) or 0) >= int(start_row)]
        return queue
    return load_prospect_queue(
        credentials_path=creds,
        sheet_url=obf_url,
        limit=limit,
        shuffle=shuffle,
        start_row=start_row,
        prospects_tab=prospects_tab,
    )["queue"]


def prepare_8_30_session(args: argparse.Namespace) -> dict[str, Any]:
    from linkedin_outreach_session import _make_prepare_summary_message

    creds = str(Path(args.creds).expanduser())
    date_value = sheet_date(args.date)
    mock_daily = _read_json(getattr(args, "mock_daily_json", None))
    mock_queue = _read_json(getattr(args, "mock_queue_json", None))

    result: dict[str, Any] = {
        "ok": True,
        "date": date_value,
        "ready": False,
        "blockers": [],
        "generators": {},
    }

    if _is_obf_weekend(date_value):
        result.update(
            {"status": "skipped_weekend", "skip_reason": _weekend_hold_message(date_value)}
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    try:
        _verify_sheet_url_identity(args.obf_url, OBF_SHEET_URL, "Operation Brute Force")
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_sheet_identity", "blockers": [str(exc)]})
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    try:
        approval, control_row, target, remaining = _read_outreach_control(
            creds,
            args.obf_url,
            date_value,
            mock_daily=mock_daily,
            auto_create_missing=True,
            auto_approve_existing=True,
        )
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_task_state", "blockers": [str(exc)]})
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    result["approval_state"] = approval
    result["target_total"] = target
    result["target_remaining"] = remaining
    result["outreach_control"] = {
        "row_number": control_row.get("_row_number"),
        "date": control_row.get("Date", date_value),
        "base_target": control_row.get("Base Target", ""),
        "rollover": control_row.get("Rollover", ""),
        "effective_target": target,
        "current_progress": int(safe_number(control_row.get("Current Progress", ""), 0) or 0),
        "status": control_row.get("Status", ""),
        "approved": control_row.get("Approved", ""),
        "prospects_start_row": _control_prospects_start_row(control_row),
        "lane_targets": _control_lane_targets(control_row, remaining),
    }
    # Keep the old cache key for backward-compatible prepared-session loading.
    result["daily_action"] = {
        "row_number": control_row.get("_row_number"),
        "task": "Outreach Control",
        "progress_key": "conn_req",
        "current_progress": int(safe_number(control_row.get("Current Progress", ""), 0) or 0),
    }

    if not approval.get("approved"):
        result.update(
            {
                "ok": False,
                "status": "blocked_approval",
                "blockers": [f"{OUTREACH_CONTROL_TAB} row is not approved"],
            }
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    if remaining <= 0:
        result.update({"status": "already_complete", "ready": False})
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    prospects_start_row = _control_prospects_start_row(control_row)
    lane_targets = _control_lane_targets(control_row, remaining)
    queue = _load_queue(
        creds,
        args.obf_url,
        mock_queue,
        shuffle=False,
        start_row=prospects_start_row,
        prospects_tab=args.prospects_tab,
        # A start row is a priority, not an exclusion. For a required split we
        # must inspect the whole eligible pool; otherwise a long Design-only
        # priority segment could hide valid Automation overflow rows and waste
        # the operating day. Only the selected ``remaining`` rows are frozen.
        limit=0 if lane_targets else QUEUE_BUFFER_LIMIT,
    )
    result["queue_count"] = len(queue)
    result["prospects_start_row"] = prospects_start_row
    if not queue:
        result.update(
            {
                "ok": False,
                "status": "notify_empty_queue",
                "blockers": ["No prepared prospects available in Prospects"],
            }
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    if len(queue) < remaining:
        result.update(
            {
                "ok": False,
                "status": "notify_queue_shortage",
                "blockers": [f"Prospects ready: {len(queue)} but target remaining is {remaining}"],
            }
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    # Make missing routing explicit before any sequence values or browser work
    # are generated. The sheet becomes the source of truth for the frozen queue.
    try:
        lane_auto_assignment = _auto_assign_missing_primary_lanes(
            queue=queue,
            remaining=remaining,
            lane_targets=lane_targets,
            worker_config=getattr(args, "worker_config", None),
        )
        assignments = lane_auto_assignment.get("assignments") or []
        assigned_counts: dict[str, int] = {}
        for assignment in assignments:
            lane = str(assignment.get("primary_lane") or "").strip()
            if lane:
                assigned_counts[lane] = assigned_counts.get(lane, 0) + 1
        result["lane_auto_assignment"] = {
            **lane_auto_assignment,
            "assigned_counts": assigned_counts,
            "persisted": False,
        }
        if assignments and mock_queue is None and not getattr(args, "no_write", False):
            _persist_primary_lane_assignments(
                creds=creds,
                obf_url=args.obf_url,
                prospects_tab=args.prospects_tab,
                assignments=assignments,
            )
            result["lane_auto_assignment"]["persisted"] = True
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "status": "blocked_lane_auto_assignment",
                "blockers": [f"Unable to auto-assign blank Primary Lane values: {exc}"],
            }
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    if lane_targets:
        if sum(lane_targets.values()) != remaining:
            result.update(
                {
                    "ok": False,
                    "status": "blocked_lane_target_mismatch",
                    "blockers": [
                        f"Outreach Control lane targets total {sum(lane_targets.values())}, "
                        f"but target remaining is {remaining}."
                    ],
                    "lane_targets": lane_targets,
                }
            )
            result["summary_message"] = _make_prepare_summary_message(result)
            return result
        balanced_queue, selected_counts, available_counts = _select_queue_for_lane_targets(
            queue, lane_targets
        )
        result["lane_targets"] = lane_targets
        result["lane_available_counts"] = available_counts
        result["lane_selected_counts"] = selected_counts
        if selected_counts != lane_targets:
            shortages = [
                f"{lane}: {selected_counts.get(lane, 0)}/{target}"
                for lane, target in lane_targets.items()
                if selected_counts.get(lane, 0) < target
            ]
            result.update(
                {
                    "ok": False,
                    "status": "blocked_lane_queue_shortage",
                    "blockers": [
                        "Autonomous baseline needs an even lane split; available "
                        + ", ".join(shortages)
                        + "."
                    ],
                }
            )
            result["summary_message"] = _make_prepare_summary_message(result)
            return result
        queue = balanced_queue

    malformed = []
    for prospect in queue:
        missing = _prospect_reporting_missing_fields(prospect)
        if missing:
            malformed.append(
                {
                    "prospect_id": prospect.get("id", ""),
                    "company": prospect.get("company", ""),
                    "missing_fields": missing,
                }
            )
    if malformed:
        first = malformed[0]
        result.update(
            {
                "ok": False,
                "status": "notify_malformed_queue",
                "blockers": [
                    f"Prepared prospect row missing required reporting fields for {first.get('company') or first.get('prospect_id')}: {', '.join(first['missing_fields'])}"
                ],
                "malformed_prospects": malformed,
            }
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    result["planned_count"] = remaining
    result["planned_queue_preview"] = [
        {
            "prospect_id": prospect.get("id"),
            "company": prospect.get("company"),
            "contact": prospect.get("contact_name"),
            "linkedin": prospect.get("contact_linkedin"),
        }
        for prospect in queue[: min(remaining, 10)]
    ]

    generator_calls = [
        (
            "batch_sizes",
            lambda: generate_batch_sizes_from_sheet(
                creds, args.obf_url, date_value, write=not args.no_write
            ),
        ),
        (
            "activity_timing",
            lambda: generate_activity_timing_from_sheet(
                creds, args.obf_url, date_value, write=not args.no_write
            ),
        ),
        (
            "delay_seconds",
            lambda: generate_delay_seconds_from_sheet(
                creds, args.obf_url, date_value, write=not args.no_write
            ),
        ),
        (
            "lead_diversions",
            lambda: generate_lead_diversions_from_sheet(
                creds, args.obf_url, date_value, write=not args.no_write
            ),
        ),
        (
            "lead_diversion_seconds",
            lambda: generate_lead_diversion_seconds_from_sheet(
                creds, args.obf_url, date_value, write=not args.no_write
            ),
        ),
    ]

    try:
        for label, fn in generator_calls:
            result["generators"][label] = fn()
        runtime_plan = _load_runtime_plan_from_sequence_sheet(
            creds=creds,
            obf_url=args.obf_url,
            sequence_tab=OUTREACH_SEQUENCE_TAB,
        )
    except Exception as exc:
        result.update(
            {"ok": False, "status": "blocked_sequence_generation", "blockers": [str(exc)]}
        )
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    prepared_queue = queue[:remaining]
    try:
        worker_plan = _assign_outreach_workers(prepared_queue, getattr(args, "worker_config", None))
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_lane_assignment", "blockers": [str(exc)]})
        result["summary_message"] = _make_prepare_summary_message(result)
        return result

    prepared = {
        "date": date_value,
        "date_key": sequence_date_key(date_value),
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "ready": True,
        "approval_state": approval,
        "daily_action": result["daily_action"],
        "outreach_control": result["outreach_control"],
        "target_total": target,
        "target_remaining": remaining,
        "prospects_start_row": prospects_start_row,
        "prospects_tab": args.prospects_tab,
        "planned_count": remaining,
        "queue_count": len(queue),
        "lane_targets": lane_targets,
        "lane_auto_assignment": result.get("lane_auto_assignment", {}),
        "lane_selected_counts": result.get("lane_selected_counts", {}),
        "queue": prepared_queue,
        "runtime_plan": runtime_plan[:remaining],
        "worker_plan": worker_plan,
        "generators": result["generators"],
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prepared_session_path(date_value).write_text(json.dumps(prepared, indent=2), encoding="utf-8")

    result["status"] = "prepared"
    result["ready"] = True
    result["cache_path"] = str(prepared_session_path(date_value))
    result["runtime_plan_count"] = len(runtime_plan[:remaining])
    result["worker_plan"] = worker_plan
    result["summary_message"] = _make_prepare_summary_message(result)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    from linkedin_outreach_session import _make_summary_message

    creds = str(Path(args.creds).expanduser())
    date_value = sheet_date(args.date)
    mock_daily = _read_json(args.mock_daily_json)
    mock_queue = _read_json(args.mock_queue_json)
    mock_quotas = _read_json(args.mock_quotas_json)

    result: dict[str, Any] = {
        "ok": True,
        "dry_run": args.dry_run,
        "date": date_value,
        "blockers": [],
        "skipped": [],
        "sent": [],
        "reconciled": [],
        "engagement_opportunities": [],
        "successful_sends": 0,
        "runtime_enforcement": {
            "sequence_source": None,
            "sequence_warning": None,
            "delays_applied": 0,
            "delay_seconds_total": 0,
            "diversions_executed": 0,
            "diversion_failures": [],
        },
        "journal": {
            "path": str(journal_path(date_value)),
            "events_written": 0,
            "pending_sync": 0,
        },
        "sync_failures": [],
        "browser_health": [],
        "browser_recoveries": [],
        "browser_guard": {
            "enabled": True,
            "max_same_error_streak": MAX_SAME_BROWSER_ERROR_STREAK,
            "same_error_streak": 0,
            "last_error": None,
            "tripped": False,
            "trip_reason": None,
            "recent_failures": [],
        },
        "early_profile_guard": {
            "enabled": True,
            "sample_size": 0,
            "inspected": 0,
            "failures": [],
            "non_failures": 0,
            "tripped": False,
            "trip_reason": None,
        },
        "failure_guard": {
            "enabled": True,
            "max_same_error_streak": MAX_SAME_SEND_ERROR_STREAK,
            "max_consecutive_send_failures": MAX_CONSECUTIVE_SEND_FAILURES,
            "same_error_streak": 0,
            "consecutive_send_failures": 0,
            "last_error": None,
            "tripped": False,
            "trip_reason": None,
            "backoff_min_sec": FAILURE_BACKOFF_MIN_SEC,
            "backoff_max_sec": FAILURE_BACKOFF_MAX_SEC,
            "backoff_count": 0,
            "backoff_total_seconds": 0,
            "recent_failures": [],
        },
    }

    if _is_obf_weekend(date_value):
        result["skipped"].append({"reason": "weekend_hold"})
        result.update(
            {"status": "skipped_weekend", "skip_reason": _weekend_hold_message(date_value)}
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    try:
        _verify_sheet_url_identity(args.obf_url, OBF_SHEET_URL, "Operation Brute Force")
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_sheet_identity", "blockers": [str(exc)]})
        result["summary_message"] = _make_summary_message(result)
        return result

    prepared_bundle: dict[str, Any] | None = None
    if getattr(args, "require_prepared_session", False):
        try:
            prepared_bundle = _load_prepared_session(
                date_value=date_value,
                prepared_path=getattr(args, "prepared_path", None),
            )
        except Exception as exc:
            result.update(
                {
                    "ok": False,
                    "status": "blocked_prepared_session",
                    "blockers": [str(exc)],
                }
            )
            result["summary_message"] = _make_summary_message(result)
            return result

    try:
        if prepared_bundle is not None and not mock_daily:
            prepared_payload = prepared_bundle["prepared"]
            prepared_action = prepared_payload.get("outreach_control")
            if prepared_payload.get("approval_state") and prepared_action:
                # The queue is frozen, but progress is deliberately refreshed at
                # each sequential account handoff.  Otherwise the second account
                # would overwrite the first account's updated progress.
                approval, conn_req_row, target, remaining = _read_outreach_control(
                    creds, args.obf_url, date_value
                )
            else:
                raise ValueError(
                    "Prepared session is missing the Outreach Control snapshot. "
                    "Rerun prepare-8_30-session after creating today's Outreach Control row."
                )
        else:
            approval, conn_req_row, target, remaining = _read_outreach_control(
                creds, args.obf_url, date_value, mock_daily=mock_daily
            )
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_task_state", "blockers": [str(exc)]})
        result["summary_message"] = _make_summary_message(result)
        return result
    starting_progress = int(safe_number(conn_req_row.get("Current Progress", ""), 0))

    result["approval_state"] = approval
    if not approval.get("approved"):
        result.update(
            {
                "ok": False,
                "status": "blocked_approval",
                "blockers": [f"{OUTREACH_CONTROL_TAB} row is not approved"],
            }
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    result["outreach_control"] = {
        "row_number": conn_req_row.get("_row_number"),
        "target": target,
        "remaining": remaining,
        "current_progress": starting_progress,
        "prospects_start_row": _control_prospects_start_row(conn_req_row),
    }
    if remaining <= 0:
        result.update({"status": "already_complete"})
        result["summary_message"] = _make_summary_message(result)
        return result

    quotas = _load_quotas(mock_quotas, dry_run=args.dry_run)
    quota_capacity, quota_remaining = _quota_capacity(quotas)
    result["quotas"] = quotas
    result["quota_remaining"] = quota_remaining
    if quota_capacity <= 0:
        result.update(
            {
                "ok": False,
                "status": "blocked_quota",
                "blockers": ["LinkedIn connection request quota is exhausted"],
            }
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    queue: list[dict[str, Any]]
    runtime_plan: list[dict[str, Any]]
    if prepared_bundle is not None:
        queue = prepared_bundle["queue"]
        runtime_plan = prepared_bundle["runtime_plan"]
        prepared_payload = prepared_bundle["prepared"]
        result["prepared_session"] = {
            "path": prepared_bundle["path"],
            "prepared_at": prepared_payload.get("prepared_at"),
            "planned_count": prepared_payload.get("planned_count"),
            "queue_count": len(queue),
        }
        result["runtime_enforcement"]["sequence_source"] = "prepared_cache"
        result["sequence"] = {
            "source": "prepared_cache",
            "date": prepared_payload.get("date", date_value),
            "prepared_at": prepared_payload.get("prepared_at"),
            "planned_count": prepared_payload.get("planned_count"),
        }
        worker_id = str(getattr(args, "worker_id", "") or "").strip()
        if worker_id:
            paired = list(zip(queue, runtime_plan))
            queue = [
                prospect
                for prospect, _plan in paired
                if str(prospect.get("worker_id") or "") == worker_id
            ]
            runtime_plan = [
                plan
                for prospect, plan in paired
                if str(prospect.get("worker_id") or "") == worker_id
            ]
            if not queue:
                result.update(
                    {
                        "ok": False,
                        "status": "blocked_lane_queue",
                        "blockers": [
                            f"Prepared session has no prospects assigned to worker '{worker_id}'."
                        ],
                    }
                )
                result["summary_message"] = _make_summary_message(result)
                return result
            result["worker_id"] = worker_id
    else:
        send_target_preview = min(remaining, quota_capacity, args.max_sends)
        sequence = load_or_generate_sequence(
            date_value, send_target_preview, regenerate=args.regenerate_sequence
        )
        result["sequence"] = sequence
        try:
            runtime_plan = _load_runtime_plan_from_sequence_sheet(
                creds=creds,
                obf_url=args.obf_url,
                sequence_tab=OUTREACH_SEQUENCE_TAB,
            )
            result["runtime_enforcement"]["sequence_source"] = "sheet"
        except Exception as exc:
            runtime_plan = _runtime_plan_from_generated_sequence(sequence)
            result["runtime_enforcement"]["sequence_source"] = "generated_fallback"
            result["runtime_enforcement"]["sequence_warning"] = str(exc)

        queue = _load_queue(
            creds,
            args.obf_url,
            mock_queue,
            start_row=_control_prospects_start_row(conn_req_row),
        )

    send_target = min(remaining, quota_capacity, args.max_sends, len(queue))
    selected = queue[:send_target]
    selected_plan = runtime_plan[:send_target]
    deduped: list[dict[str, Any]] = []
    deduped_plan: list[dict[str, Any]] = []
    seen_profile_urls: set[str] = set()
    duplicate_skips = 0
    journal_confirmed_skips = 0
    for index, prospect in enumerate(selected):
        plan_item = selected_plan[index] if index < len(selected_plan) else {}
        url_key = _normalize_profile_url(prospect.get("contact_linkedin", ""))
        if url_key and url_key in seen_profile_urls:
            duplicate_skips += 1
            result["skipped"].append(
                {
                    "prospect_id": prospect.get("id"),
                    "reason": "duplicate_profile_in_run",
                }
            )
            continue
        if url_key:
            seen_profile_urls.add(url_key)
        if _journal_has_confirmed_send(date_value, prospect):
            journal_confirmed_skips += 1
            if not getattr(args, "verify_connection_modal", False):
                _recover_confirmed_send_sheet_sync(
                    result=result,
                    prospect=prospect,
                    date_value=date_value,
                    creds=creds,
                    args=args,
                )
            result["skipped"].append(
                {
                    "prospect_id": prospect.get("id"),
                    "reason": "already_journal_confirmed",
                }
            )
            continue
        deduped.append(prospect)
        deduped_plan.append(plan_item)
    selected = deduped
    selected_plan = deduped_plan
    result["queue_count"] = len(queue)
    result["selected_count"] = len(selected)
    result["deduped_profile_skips"] = duplicate_skips
    result["journal_confirmed_skips"] = journal_confirmed_skips
    if not selected:
        status = "already_reconciled" if journal_confirmed_skips else "blocked_empty_queue"
        result.update({"status": status})
        if not journal_confirmed_skips:
            result.update(
                {
                    "ok": False,
                    "blockers": ["No unique prepared prospects available in Prospects"],
                }
            )
        _reconcile_result_from_journal(
            result, date_value, starting_progress=starting_progress, target=target
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    result["planned"] = [
        {
            "prospect_id": prospect.get("id"),
            "row_number": prospect.get("_row_number"),
            "company": prospect.get("company"),
            "person": prospect.get("engaged_person", "Person 1"),
            "contact": prospect.get("contact_name"),
            "linkedin": prospect.get("contact_linkedin"),
        }
        for prospect in selected
    ]

    if args.dry_run:
        result["status"] = "dry_run_planned"
        result["summary_message"] = _make_summary_message(result)
        return result

    from linkedin_helper import LinkedInSession

    session = LinkedInSession()
    connect_result = session.connect(
        skip_rate_check=getattr(args, "skip_acceptance_rate_check", False)
    )
    if not connect_result.get("ok"):
        result.update(
            {
                "ok": False,
                "status": "blocked_preflight",
                "blockers": [connect_result.get("block_reason", "LinkedIn preflight failed")],
                "preflight": connect_result,
            }
        )
        result["summary_message"] = _make_summary_message(result)
        return result
    if not _ensure_session_healthy(
        result=result,
        session=session,
        label="post_connect",
        args=args,
        allow_reconnect=False,
    ):
        result.update(
            {
                "ok": False,
                "status": "blocked_browser_health",
                "blockers": ["Chrome/CDP health check failed after LinkedIn preflight"],
            }
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    ui_preflight = _run_outreach_ui_preflight(
        session=session,
        selected=selected,
        check_connect_modal=not getattr(args, "verify_connection_modal", False),
    )
    result["outreach_ui_preflight"] = ui_preflight
    result["early_profile_guard"]["sample_size"] = _outreach_ui_sample_size(len(selected))
    if not ui_preflight.get("ok"):
        result.update(
            {
                "ok": False,
                "status": "blocked_ui_preflight",
                "blockers": [ui_preflight.get("blocker", "LinkedIn outreach UI preflight failed")],
            }
        )
        result["summary_message"] = _make_summary_message(result)
        return result

    if getattr(args, "verify_connection_modal", False):
        # Deliberately non-sending E2E mode.  It uses the real account, CDP,
        # prepared queue and UI path, but the helper opens then dismisses the
        # invite modal and never invokes send_connection or any Sheet write.
        try:
            verification = _run_connection_modal_checks(session=session, selected=selected)
        finally:
            session.disconnect()
        result["modal_checks"] = verification["checks"]
        if verification["ok"]:
            result["status"] = "connection_modal_check_complete"
        else:
            result["ok"] = False
            result["status"] = "connection_modal_check_failed"
            result["blockers"].append(verification["error"])
        result["summary_message"] = _make_summary_message(result)
        return result

    templates = load_templates(category="CR", credentials_path=creds, sheet_url=args.obf_url)
    try:
        if getattr(args, "skip_warm_up", False):
            warm_up = {"skipped": True, "reason": "skip_warm_up"}
        else:
            try:
                warm_up = session.warm_up()
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "status": "blocked_warm_up",
                        "blockers": [f"warm_up exception: {exc}"],
                    }
                )
                warm_up = {"error": str(exc)}
        result["warm_up"] = warm_up
        if warm_up.get("error"):
            result.update({"ok": False, "status": "blocked_warm_up", "blockers": [str(warm_up)]})
        else:
            _emit_outreach_progress(
                result,
                date_value=date_value,
                stage="lead_loop_started",
                processed=0,
                selected_count=len(selected),
            )
            for index, prospect in enumerate(selected):
                if not _ensure_session_healthy(
                    result=result,
                    session=session,
                    label=f"before_lead:{prospect.get('id')}",
                    args=args,
                ):
                    break
                lead_action = _process_outreach_lead_state_machine(
                    result=result,
                    session=session,
                    prospect=prospect,
                    plan_item=selected_plan[index] if index < len(selected_plan) else {},
                    index=index,
                    selected_count=len(selected),
                    date_value=date_value,
                    creds=creds,
                    args=args,
                    templates=templates,
                    conn_req_row=conn_req_row,
                    starting_progress=starting_progress,
                    target=target,
                )
                processed = index + 1
                if lead_action == "break" or processed % 5 == 0 or processed >= len(selected):
                    _emit_outreach_progress(
                        result,
                        date_value=date_value,
                        stage="lead_loop_progress"
                        if lead_action != "break"
                        else "lead_loop_blocked",
                        processed=processed,
                        selected_count=len(selected),
                        prospect=prospect,
                    )
                if lead_action == "break":
                    break
                continue

    except KeyboardInterrupt:
        result["ok"] = False
        result["status"] = "interrupted"
        result["blockers"].append("Interrupted before runner completed final loop")
        _record_journal_event(
            result,
            date_value,
            "run_interrupted",
            "recorded",
            successful_sends_before_reconcile=result.get("successful_sends", 0),
        )
    finally:
        session.disconnect()

    _reconcile_result_from_journal(
        result, date_value, starting_progress=starting_progress, target=target
    )
    if not result.get("status"):
        result["status"] = "completed" if result["successful_sends"] >= remaining else "partial"
    result["summary_message"] = _make_summary_message(result)
    _emit_outreach_progress(
        result,
        date_value=date_value,
        stage="final",
        processed=len(selected) if "selected" in locals() else 0,
        selected_count=len(selected) if "selected" in locals() else 0,
    )
    return result
