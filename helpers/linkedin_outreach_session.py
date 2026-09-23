#!/usr/bin/env python3
"""
Deterministic runner for the 8:30 AM LinkedIn outreach block.

The runner keeps the cron agent out of low-level decision-making:
- Outreach approval, targets, rollover, and progress stay in Operation Brute Force -> Outreach Control.
- Prospect status/logging stays in outreach_helper.py.
- Browser actions and quota state stay in linkedin_helper.py.
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from gspread.exceptions import WorksheetNotFound
from runtime_environment import load_repo_env

load_repo_env()

HELPERS_DIR = Path(__file__).resolve().parent
ROOT_DIR = HELPERS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

sys.path.insert(0, str(HELPERS_DIR))

from outreach_helper import (  # noqa: E402
    append_pipeline_row_for_acceptance,
    apply_prospect_fields,
    build_connection_sent_fields,
    build_outreach_log_row,
    build_prospect_activity_fields,
    build_template_variables,
    insert_outreach_log_row,
    load_pending_outreach_log_connections,
    load_prospect_queue,
    load_templates,
    mark_connected,
    mark_outreach_log_connected,
    pick_connection_template,
    render_template,
)
from sheets_helper import (  # noqa: E402
    daily_approval_state,
    format_sheet_date,
    get_client,
    get_daily_group_data,
    get_worksheet,
    is_checked_value,
    open_sheet,
    safe_number,
    sheet_values_equal,
    update_row,
)

from outbound.outreach.journal import (  # noqa: F401
    _append_jsonl,
    _confirmed_send_key,
    _emit_outreach_progress,
    _journal_confirmed_send_records,
    _journal_event,
    _journal_event_matches_prospect,
    _journal_has_confirmed_send,
    _journal_pending_sync_count,
    _pending_sync_payload_for_confirmed_send,
    _prospect_identity,
    _read_journal_events,
    _read_journal_events_for_prospect,
    _reconcile_result_from_journal,
    _record_breadcrumb,
    _record_journal_event,
    _sheet_target_payload,
    journal_path,
    prepared_session_path,
    sequence_path,
)
from outbound.outreach.paths import (  # noqa: F401
    ACCEPTANCE_STATE_DIR,
    ACCEPTANCE_STATE_FILE,
    DEFAULT_CREDS,
    JOURNAL_DIR,
    OBF_SHEET_URL,
    OUTREACH_WORKERS_CONFIG_PATH,
    STATE_DIR,
    TASK_MANAGER_URL,
    _resolve_default_creds,
)
from outbound.outreach.planning import (  # noqa: F401
    _normalize_activity_timing,
    _random_positive_partition,
    _runtime_plan_from_generated_sequence,
    generate_activity_timing_plan,
    generate_batch_size_plan,
    generate_delay_seconds_plan,
    generate_sequence,
    load_or_generate_sequence,
)
from outbound.outreach.policy import (  # noqa: F401
    ACTIVITY_READ_TIMEOUT_SEC,
    CDP_HEALTH_EVAL_TIMEOUT_SEC,
    DAILY_CONN_REQ_LIMIT,
    DEFAULT_LANE_SPLIT_MARKER,
    DEFAULT_LANE_TARGETS,
    DEFAULT_TARGET,
    EMAIL_REQUIRED_TO_CONNECT,
    FAILURE_BACKOFF_MAX_SEC,
    FAILURE_BACKOFF_MIN_SEC,
    HARD_STOP_ERRORS,
    MAX_BROWSER_RECOVERY_ATTEMPTS,
    MAX_CONSECUTIVE_SEND_FAILURES,
    MAX_SAME_BROWSER_ERROR_STREAK,
    MAX_SAME_SEND_ERROR_STREAK,
    NO_CONNECT_BUTTON_RETRIES,
    NO_CONNECT_BUTTON_RETRY_MAX_SEC,
    NO_CONNECT_BUTTON_RETRY_MIN_SEC,
    NON_FATAL_SEND_ERRORS,
    OUTREACH_CONTROL_APPROVED_STATUSES,
    OUTREACH_CONTROL_HEADERS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    OUTREACH_CONTROL_TAB,
    OUTREACH_CONTROL_TERMINAL_STATUSES,
    OUTREACH_SEQUENCE_TAB,
    OUTREACH_STATUS_REQUIRES_EMAIL,
    PROFILE_UI_GUARD_ERRORS,
    PROFILE_UNKNOWN_RETRIES,
    PROFILE_UNKNOWN_RETRY_MAX_SEC,
    PROFILE_UNKNOWN_RETRY_MIN_SEC,
    QUEUE_BUFFER_LIMIT,
    RETRYABLE_CONNECT_ERRORS,
    WEEKLY_CONN_REQ_LIMIT,
)
from outbound.outreach.sequence_sheet import (  # noqa: F401
    _load_runtime_plan_from_sequence_sheet,
    _resolve_sequence_columns,
    generate_activity_timing_from_sheet,
    generate_batch_sizes_from_sheet,
    generate_delay_seconds_from_sheet,
    generate_lead_diversion_seconds_from_sheet,
    generate_lead_diversions_from_sheet,
)
from outbound.shared.dates import (  # noqa: F401
    _parse_date,
    sequence_date_key,
    sheet_date,
)
from outbound.shared.diversion import (  # noqa: F401
    _diversion_range,
    _first_post_url,
    _run_diversion,
    generate_lead_diversion_plan,
    generate_lead_diversion_seconds_plan,
)
from outbound.shared.sheetutils import (  # noqa: F401
    _a1_cell,
    _batch_update_cells,
    _clamped_gauss,
    _col_to_a1,
    _find_first_index,
    _find_last_index,
    _is_enabled,
    _normalize_person_name,
    _normalize_profile_url,
    _parse_int,
    _parse_iso_datetime,
)


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


def _load_acceptance_state(path: Path = ACCEPTANCE_STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _save_acceptance_state(state: dict[str, Any], path: Path = ACCEPTANCE_STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")


def _prune_seen_acceptances(
    seen: dict[str, Any], now: datetime, keep_days: int = 14
) -> dict[str, str]:
    threshold = now - timedelta(days=keep_days)
    pruned: dict[str, str] = {}
    for key, value in (seen or {}).items():
        seen_at = _parse_iso_datetime(str(value))
        if seen_at is None or seen_at >= threshold:
            pruned[str(key)] = str(value)
    return pruned


def _acceptance_fingerprint(candidate: dict[str, Any]) -> str:
    url_key = _normalize_profile_url(candidate.get("url", ""))
    if url_key:
        return f"url:{url_key}"
    name_key = _normalize_person_name(candidate.get("name", ""))
    if name_key:
        return f"name:{name_key}"
    return ""


def _load_pending_queue(creds: str, obf_url: str, mock_pending: Any | None) -> list[dict[str, Any]]:
    if mock_pending:
        if isinstance(mock_pending, list):
            return mock_pending
        return mock_pending.get("queue", mock_pending.get("pending", []))
    return load_pending_outreach_log_connections(credentials_path=creds, sheet_url=obf_url)


def _is_active_hour(now: datetime, start_hour: int, end_hour: int) -> bool:
    current = now.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= current < end_hour
    return current >= start_hour or current < end_hour


def _schedule_acceptance_check(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    active_window = _is_active_hour(now, args.active_start_hour, args.active_end_hour)
    if active_window:
        min_minutes = args.active_min_minutes
        max_minutes = args.active_max_minutes
        mode = "active_hours"
    else:
        min_minutes = args.off_hours_min_minutes
        max_minutes = args.off_hours_max_minutes
        mode = "off_hours"

    rng = random.SystemRandom()
    base_minutes = rng.randint(min_minutes, max_minutes)
    jitter = rng.randint(args.jitter_min_minutes, args.jitter_max_minutes)
    jitter *= rng.choice((-1, 1))
    interval_minutes = max(5, base_minutes + jitter)
    next_due = now + timedelta(minutes=interval_minutes)
    return {
        "mode": mode,
        "base_minutes": base_minutes,
        "jitter_minutes": jitter,
        "interval_minutes": interval_minutes,
        "next_check_not_before": next_due.isoformat(timespec="seconds"),
    }


def _build_pending_indexes(
    pending: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_url: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for prospect in pending:
        url_key = _normalize_profile_url(prospect.get("contact_linkedin", ""))
        if url_key:
            by_url.setdefault(url_key, []).append(prospect)
        name_key = _normalize_person_name(prospect.get("contact_name", ""))
        if name_key:
            by_name.setdefault(name_key, []).append(prospect)
    return by_url, by_name


def _match_pending_acceptance(
    acceptance: dict[str, Any],
    by_url: dict[str, list[dict[str, Any]]],
    by_name: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str, str]:
    url_key = _normalize_profile_url(acceptance.get("url", ""))
    name_key = _normalize_person_name(acceptance.get("name", ""))

    matches: list[dict[str, Any]] = []
    match_source = ""
    if url_key and url_key in by_url:
        matches = by_url[url_key]
        match_source = "url"
    elif name_key and name_key in by_name:
        matches = by_name[name_key]
        match_source = "name"

    if len(matches) == 1:
        return matches[0], match_source, ""
    if len(matches) > 1:
        return None, match_source, f"ambiguous_{match_source}_match"
    return None, "", "no_pending_match"


def _pick_first_message_template(templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not templates:
        return None
    for template in templates:
        if str(template.get("template_id", "")).strip().upper() == "FM-01":
            return template
    return templates[0]


def _render_first_message_draft(
    prospect: dict[str, Any],
    templates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    template = _pick_first_message_template(templates)
    if not template:
        return None
    return {
        "prospect_id": str(prospect.get("id", "")).strip(),
        "company": str(prospect.get("company", "")).strip(),
        "contact_name": str(prospect.get("contact_name", "")).strip(),
        "template_id": str(template.get("template_id", "")).strip(),
        "message": render_template(template, build_template_variables(prospect)),
    }


def _normalize_failure_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "unknown_send_failure"
    if "| diagnostics=" in raw:
        raw = raw.split("| diagnostics=", 1)[0].strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw[:180]


def _register_send_failure(
    result: dict[str, Any], prospect_id: Any, error_value: Any
) -> str | None:
    guard = result.setdefault("failure_guard", {})
    if not guard.get("enabled", False):
        return None

    error_key = _normalize_failure_key(error_value)
    last_error = str(guard.get("last_error") or "")
    if error_key == last_error:
        guard["same_error_streak"] = int(guard.get("same_error_streak", 0)) + 1
    else:
        guard["same_error_streak"] = 1
        guard["last_error"] = error_key

    guard["consecutive_send_failures"] = int(guard.get("consecutive_send_failures", 0)) + 1

    recent = guard.setdefault("recent_failures", [])
    if isinstance(recent, list):
        recent.append(
            {
                "prospect_id": str(prospect_id or "").strip(),
                "error": error_key,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(recent) > 10:
            del recent[:-10]

    if int(guard.get("same_error_streak", 0)) >= int(
        guard.get("max_same_error_streak", MAX_SAME_SEND_ERROR_STREAK)
    ):
        reason = (
            f"Failure guard tripped: same send error repeated "
            f"{guard['same_error_streak']}x ({error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    if int(guard.get("consecutive_send_failures", 0)) >= int(
        guard.get("max_consecutive_send_failures", MAX_CONSECUTIVE_SEND_FAILURES)
    ):
        reason = (
            f"Failure guard tripped: {guard['consecutive_send_failures']} consecutive send failures "
            f"(last={error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason

    return None


def _register_browser_failure(
    result: dict[str, Any],
    prospect_id: Any,
    error_value: Any,
    step: str,
) -> str | None:
    guard = result.setdefault(
        "browser_guard",
        {
            "enabled": True,
            "max_same_error_streak": MAX_SAME_BROWSER_ERROR_STREAK,
            "same_error_streak": 0,
            "last_error": None,
            "tripped": False,
            "trip_reason": None,
            "recent_failures": [],
        },
    )
    if not guard.get("enabled", False):
        return None

    error_key = _normalize_failure_key(error_value)
    if error_key == str(guard.get("last_error") or ""):
        guard["same_error_streak"] = int(guard.get("same_error_streak", 0)) + 1
    else:
        guard["same_error_streak"] = 1
        guard["last_error"] = error_key

    recent = guard.setdefault("recent_failures", [])
    if isinstance(recent, list):
        recent.append(
            {
                "prospect_id": str(prospect_id or "").strip(),
                "step": step,
                "error": error_key,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(recent) > 10:
            del recent[:-10]

    if int(guard.get("same_error_streak", 0)) >= int(
        guard.get("max_same_error_streak", MAX_SAME_BROWSER_ERROR_STREAK)
    ):
        reason = (
            f"Browser guard tripped: same browser/CDP error repeated "
            f"{guard['same_error_streak']}x during {step} ({error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    return None


def _reset_browser_failure_guard(result: dict[str, Any]) -> None:
    guard = result.get("browser_guard")
    if not isinstance(guard, dict):
        return
    guard["same_error_streak"] = 0
    guard["last_error"] = None


def _outreach_ui_sample_size(selected_count: int) -> int:
    if selected_count <= 0:
        return 0
    rounded_ten_percent = int(((selected_count * 10) + 50) // 100)
    return min(selected_count, max(3, rounded_ten_percent))


def _is_profile_ui_failure(live_state: str, error_value: Any) -> bool:
    if str(live_state or "").strip().lower() != "unknown":
        return False
    error_key = _normalize_failure_key(error_value)
    return (
        error_key in PROFILE_UI_GUARD_ERRORS
        or "selector" in error_key
        or "topcard" in error_key
        or "modal_not_ready" in error_key
    )


def _record_early_profile_guard(
    result: dict[str, Any],
    prospect_id: Any,
    live_state: str,
    profile_state: dict[str, Any],
    selected_count: int,
) -> str | None:
    sample_size = _outreach_ui_sample_size(selected_count)
    guard = result.setdefault(
        "early_profile_guard",
        {
            "enabled": True,
            "sample_size": sample_size,
            "inspected": 0,
            "failures": [],
            "non_failures": 0,
            "tripped": False,
            "trip_reason": None,
        },
    )
    if not guard.get("enabled", False) or guard.get("tripped") or sample_size <= 0:
        return None
    if int(guard.get("inspected", 0)) >= sample_size:
        return None

    guard["sample_size"] = sample_size
    guard["inspected"] = int(guard.get("inspected", 0)) + 1
    error_key = _normalize_failure_key(profile_state.get("error"))
    if _is_profile_ui_failure(live_state, profile_state.get("error")):
        failures = guard.setdefault("failures", [])
        if isinstance(failures, list):
            failures.append(
                {
                    "prospect_id": str(prospect_id or "").strip(),
                    "state": str(live_state or "").strip(),
                    "error": error_key,
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
    else:
        guard["non_failures"] = int(guard.get("non_failures", 0)) + 1

    failures_count = (
        len(guard.get("failures", [])) if isinstance(guard.get("failures"), list) else 0
    )
    inspected = int(guard.get("inspected", 0))
    if inspected >= sample_size and failures_count == inspected:
        first_error = error_key
        failures = guard.get("failures", [])
        if isinstance(failures, list) and failures:
            first_error = str(failures[0].get("error") or first_error)
        reason = (
            f"Early profile UI guard tripped: first {sample_size} inspected leads "
            f"all failed profile readiness ({first_error})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    return None


def _run_outreach_ui_preflight(
    *,
    session: Any,
    selected: list[dict[str, Any]],
    check_connect_modal: bool = True,
) -> dict[str, Any]:
    sample_size = _outreach_ui_sample_size(len(selected))
    payload: dict[str, Any] = {
        "ok": True,
        "sample_size": sample_size,
        "inspected": [],
        "connectable_checked": False,
        "selectors": {
            "profile_topcard": '[componentkey*="profile.card"][componentkey*="Topcard"]',
            "more_connect": 'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]',
            "send_without_note": 'button[aria-label="Send without a note"]',
            "shadow_dom_modal": True,
        },
    }
    if sample_size <= 0:
        payload["ok"] = False
        payload["blocker"] = "No selected leads available for UI preflight"
        return payload

    profile_readiness_failures: list[dict[str, Any]] = []
    profile_readiness_non_failures = 0
    profile_readiness_candidates = 0

    for index, prospect in enumerate(selected[:sample_size]):
        profile_url = str(prospect.get("contact_linkedin", "") or "").strip()
        item: dict[str, Any] = {
            "index": index,
            "prospect_id": prospect.get("id"),
            "profile_url": profile_url,
        }
        payload["inspected"].append(item)
        if not profile_url:
            item["state"] = "missing_linkedin_url"
            continue

        profile_readiness_candidates += 1
        try:
            profile_state = session.inspect_profile_action_state(profile_url)
        except Exception as exc:
            item["state"] = "exception"
            item["error"] = _normalize_failure_key(exc)
            payload["ok"] = False
            payload["blocker"] = f"UI preflight profile inspection exception: {item['error']}"
            return payload

        live_state = str(profile_state.get("state", "unknown") or "unknown")
        item["state"] = live_state
        item["error"] = profile_state.get("error")
        item["more_button_seen"] = profile_state.get("more_button_seen")
        item["debug_dump"] = profile_state.get("debug_dump")

        if _is_profile_ui_failure(live_state, profile_state.get("error")):
            failure = {
                "prospect_id": prospect.get("id"),
                "profile_url": profile_url,
                "error": _normalize_failure_key(profile_state.get("error")),
            }
            profile_readiness_failures.append(failure)
            payload.setdefault("profile_readiness_failures", []).append(failure)
            continue

        profile_readiness_non_failures += 1

        if profile_readiness_failures:
            payload["warning"] = (
                "One or more sampled leads failed profile readiness, but another sampled "
                "profile loaded successfully; continuing preflight"
            )

        if (
            live_state in {"connect_direct", "connect_in_more"}
            and not payload["connectable_checked"]
        ):
            if not check_connect_modal:
                payload["connectable_checked"] = True
                payload["modal_check_deferred"] = True
                continue
            try:
                no_note_check = session.verify_no_note_send_ui(profile_url, profile_state)
            except Exception as exc:
                no_note_check = {"ok": False, "error": _normalize_failure_key(exc)}
            item["no_note_modal_check"] = no_note_check
            if not no_note_check.get("ok"):
                if _normalize_failure_key(no_note_check.get("error")) == EMAIL_REQUIRED_TO_CONNECT:
                    item["soft_skip"] = EMAIL_REQUIRED_TO_CONNECT
                    payload.setdefault("soft_skips", []).append(
                        {
                            "prospect_id": prospect.get("id"),
                            "profile_url": profile_url,
                            "reason": EMAIL_REQUIRED_TO_CONNECT,
                        }
                    )
                    payload["warning"] = (
                        "At least one sampled lead requires email to connect; lead will be reported and skipped"
                    )
                    continue
                payload["ok"] = False
                payload["blocker"] = (
                    f"UI preflight failed no-note invite modal check for prospect {prospect.get('id')}: "
                    f"{_normalize_failure_key(no_note_check.get('error'))}"
                )
                return payload
            payload["connectable_checked"] = True

    if (
        profile_readiness_candidates > 0
        and profile_readiness_failures
        and profile_readiness_non_failures == 0
    ):
        first_failure = profile_readiness_failures[0]
        payload["ok"] = False
        payload["blocker"] = (
            "UI preflight failed on profile readiness for all "
            f"{profile_readiness_candidates} sampled prospects; first failure "
            f"{first_failure.get('prospect_id')}: {first_failure.get('error')}"
        )
        return payload

    if not payload["connectable_checked"]:
        if payload.get("soft_skips"):
            payload["warning"] = (
                "Only email-required connectable leads were found in UI preflight sample"
            )
        else:
            payload["warning"] = (
                "No connectable lead found in UI preflight sample; send modal was not opened"
            )
    return payload


def _run_connection_modal_checks(*, session: Any, selected: list[dict[str, Any]]) -> dict[str, Any]:
    """Run fail-closed no-send checks and stop at the first unsafe result."""
    checks: list[dict[str, Any]] = []
    for prospect in selected:
        profile_url = str(prospect.get("contact_linkedin", "") or "").strip()
        profile_state: dict[str, Any] = {}
        try:
            profile_state = session.inspect_profile_action_state(profile_url)
            modal = session.verify_no_note_send_ui(profile_url, profile_state)
        except Exception as exc:
            modal = {"ok": False, "closed": False, "error": _normalize_failure_key(exc)}
        check = {
            "prospect_id": prospect.get("id"),
            "company": prospect.get("company"),
            "profile_state": profile_state,
            "modal": modal,
        }
        checks.append(check)
        if not modal.get("ok"):
            return {
                "ok": False,
                "checks": checks,
                "error": (
                    f"No-send modal check failed for prospect {prospect.get('id')}: "
                    f"{_normalize_failure_key(modal.get('error'))}"
                ),
            }
    return {"ok": True, "checks": checks}


def _session_cdp_health_check(session: Any, label: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": label,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "ok": False,
    }
    try:
        cdp = getattr(session, "cdp", None)
        if cdp is None:
            payload["error"] = "missing_cdp"
            return payload
        http_health = cdp.health_check()
        payload["http"] = http_health
        if http_health.get("status") != "ok":
            payload["error"] = http_health.get("error", "cdp_http_unhealthy")
            return payload
        eval_result = cdp.evaluate(
            "(() => ({readyState: document.readyState, href: window.location.href, ok: true}))()",
            timeout=CDP_HEALTH_EVAL_TIMEOUT_SEC,
        )
        payload["eval"] = eval_result
        payload["ok"] = True
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        return payload


def _ensure_session_healthy(
    *,
    result: dict[str, Any],
    session: Any,
    label: str,
    args: argparse.Namespace,
    allow_reconnect: bool = True,
) -> bool:
    health = _session_cdp_health_check(session, label)
    result.setdefault("browser_health", []).append(health)
    if health.get("ok"):
        _reset_browser_failure_guard(result)
        return True
    if not allow_reconnect:
        return False

    recoveries = result.setdefault("browser_recoveries", [])
    if len(recoveries) >= MAX_BROWSER_RECOVERY_ATTEMPTS:
        result["ok"] = False
        result["status"] = "blocked_browser_health"
        result["blockers"].append(
            f"Chrome/CDP unhealthy during {label}; recovery limit reached: {health.get('error', 'unknown')}"
        )
        return False

    recovery: dict[str, Any] = {
        "label": label,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "initial_error": health.get("error", "unknown"),
    }
    recoveries.append(recovery)
    try:
        try:
            session.disconnect()
        except Exception as exc:
            recovery["disconnect_error"] = str(exc)
        connect_result = session.connect(
            skip_rate_check=getattr(args, "skip_acceptance_rate_check", False)
        )
        recovery["connect_result"] = connect_result
        if not connect_result.get("ok"):
            raise RuntimeError(connect_result.get("block_reason", "LinkedIn reconnect failed"))
        post = _session_cdp_health_check(session, f"{label}_post_reconnect")
        result.setdefault("browser_health", []).append(post)
        recovery["post_health"] = post
        if not post.get("ok"):
            raise RuntimeError(post.get("error", "post_reconnect_health_failed"))
        recovery["ok"] = True
        _reset_browser_failure_guard(result)
        return True
    except Exception as exc:
        recovery["ok"] = False
        recovery["error"] = str(exc)
        result["ok"] = False
        result["status"] = "blocked_browser_health"
        result["blockers"].append(f"Chrome/CDP recovery failed during {label}: {exc}")
        return False


def _reset_send_failure_guard(result: dict[str, Any]) -> None:
    guard = result.get("failure_guard")
    if not isinstance(guard, dict):
        return
    guard["consecutive_send_failures"] = 0
    guard["same_error_streak"] = 0
    guard["last_error"] = None


def _sleep_failure_backoff(result: dict[str, Any], is_last: bool) -> None:
    if is_last:
        return
    guard = result.get("failure_guard")
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return
    min_sec = int(guard.get("backoff_min_sec", FAILURE_BACKOFF_MIN_SEC) or FAILURE_BACKOFF_MIN_SEC)
    max_sec = int(guard.get("backoff_max_sec", FAILURE_BACKOFF_MAX_SEC) or FAILURE_BACKOFF_MAX_SEC)
    if max_sec < min_sec:
        max_sec = min_sec
    seconds = random.randint(min_sec, max_sec)
    time.sleep(seconds)
    guard["backoff_count"] = int(guard.get("backoff_count", 0)) + 1
    guard["backoff_total_seconds"] = int(guard.get("backoff_total_seconds", 0)) + seconds


def _read_json(path: str | None) -> Any | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_outreach_workers(path_value: str | None = None) -> dict[str, dict[str, Any]]:
    """Return the configured lane -> account mapping for a frozen queue."""
    path = Path(path_value).expanduser() if path_value else OUTREACH_WORKERS_CONFIG_PATH
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("enabled"):
        return {}
    workers: dict[str, dict[str, Any]] = {}
    for raw_worker in payload.get("workers", []):
        if not raw_worker.get("enabled", True):
            continue
        worker = dict(raw_worker)
        worker_id = str(worker.get("id") or "").strip()
        lane = str(worker.get("primary_lane") or "").strip()
        if not worker_id or not lane:
            raise ValueError("Each enabled outreach worker needs both id and primary_lane.")
        if lane in workers:
            raise ValueError(f"More than one outreach worker is mapped to Primary Lane '{lane}'.")
        workers[lane] = worker
    if not workers:
        raise ValueError("outreach_workers.json is enabled but has no enabled workers.")
    return workers


def _assign_outreach_workers(
    queue: list[dict[str, Any]], worker_config: str | None = None
) -> dict[str, Any]:
    """Freeze account ownership in the prepared queue; never infer it at run time."""
    workers_by_lane = _read_outreach_workers(worker_config)
    if not workers_by_lane:
        return {"enabled": False, "workers": [], "counts": {}}
    counts: dict[str, int] = {}
    for prospect in queue:
        lane = str(prospect.get("primary_lane") or "").strip()
        worker = workers_by_lane.get(lane)
        if worker is None:
            raise ValueError(
                f"Prepared prospect {prospect.get('id') or prospect.get('company') or 'unknown'} "
                f"has Primary Lane '{lane or '(blank)'}', which has no enabled outreach account."
            )
        worker_id = str(worker["id"])
        prospect["worker_id"] = worker_id
        counts[worker_id] = counts.get(worker_id, 0) + 1
    return {
        "enabled": True,
        "assigned_at": datetime.now().isoformat(timespec="seconds"),
        "assignment": "primary_lane_to_account",
        "workers": [
            {"id": worker["id"], "primary_lane": lane} for lane, worker in workers_by_lane.items()
        ],
        "counts": counts,
    }


def _select_queue_for_lane_targets(
    queue: list[dict[str, Any]], lane_targets: dict[str, int]
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Keep source order while selecting an exact, explicit lane allocation."""
    normalized_targets = {
        str(lane).strip(): max(0, int(target))
        for lane, target in lane_targets.items()
        if str(lane).strip()
    }
    selected: list[dict[str, Any]] = []
    selected_counts = {lane: 0 for lane in normalized_targets}
    available_counts = {lane: 0 for lane in normalized_targets}
    for prospect in queue:
        lane = str(prospect.get("primary_lane") or "").strip()
        if lane not in normalized_targets:
            continue
        available_counts[lane] += 1
        if selected_counts[lane] < normalized_targets[lane]:
            selected.append(prospect)
            selected_counts[lane] += 1
    return selected, selected_counts, available_counts


def _auto_assign_missing_primary_lanes(
    queue: list[dict[str, Any]],
    remaining: int,
    lane_targets: dict[str, int],
    worker_config: str | None = None,
) -> dict[str, Any]:
    """Assign only eligible blank-lane prospects before a queue is frozen.

    The input comes from ``load_prospect_queue``, which already excludes used
    prospects and rows without a LinkedIn target. Existing lanes are immutable.
    With an explicit daily split we fill the largest lane deficit across the
    scanned queue. Without one, we balance only the imminent prepared batch.
    """
    workers_by_lane = _read_outreach_workers(worker_config)
    enabled_lanes = list(workers_by_lane)
    summary: dict[str, Any] = {
        "enabled": bool(enabled_lanes),
        "mode": "lane_targets" if lane_targets else "batch_balance",
        "assignments": [],
        "counts": {},
    }
    if not enabled_lanes:
        summary["skip_reason"] = "outreach_workers_not_enabled"
        return summary

    normalized_targets = {
        lane: max(0, int(target))
        for lane, target in lane_targets.items()
        if lane in workers_by_lane and int(target) > 0
    }
    scope = queue if normalized_targets else queue[: max(0, int(remaining))]
    counts = {lane: 0 for lane in enabled_lanes}
    for prospect in scope:
        lane = str(prospect.get("primary_lane") or "").strip()
        if lane in counts:
            counts[lane] += 1

    for prospect in scope:
        if str(prospect.get("primary_lane") or "").strip():
            continue
        if normalized_targets:
            deficits = {
                lane: normalized_targets[lane] - counts[lane] for lane in normalized_targets
            }
            candidates = [lane for lane in enabled_lanes if deficits.get(lane, 0) > 0]
            if not candidates:
                break
            chosen_lane = max(
                candidates, key=lambda lane: (deficits[lane], -enabled_lanes.index(lane))
            )
        else:
            chosen_lane = min(
                enabled_lanes, key=lambda lane: (counts[lane], enabled_lanes.index(lane))
            )
        prospect["primary_lane"] = chosen_lane
        counts[chosen_lane] += 1
        summary["assignments"].append(
            {
                "prospect_id": str(prospect.get("id") or ""),
                "company": str(prospect.get("company") or ""),
                "row_number": int(prospect.get("_row_number") or 0),
                "primary_lane": chosen_lane,
            }
        )

    summary["counts"] = counts
    return summary


def _persist_primary_lane_assignments(
    creds: str,
    obf_url: str,
    prospects_tab: str,
    assignments: list[dict[str, Any]],
) -> None:
    """Write prep-time lane assignments in one bounded Sheets update."""
    if not assignments:
        return
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, prospects_tab)
    headers = worksheet.row_values(1)
    lane_column = _find_first_index(headers, "Primary Lane")
    if lane_column is None:
        raise ValueError(f"{prospects_tab} is missing the required Primary Lane column.")
    updates = [
        (int(assignment["row_number"]), lane_column + 1, assignment["primary_lane"])
        for assignment in assignments
        if int(assignment.get("row_number") or 0) >= 2
    ]
    if len(updates) != len(assignments):
        raise ValueError("Cannot persist a lane assignment without a valid Prospects row number.")
    _batch_update_cells(worksheet, updates)


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


def _extract_target(task_row: dict[str, Any], default: int = DEFAULT_TARGET) -> int:
    text = " ".join(
        str(task_row.get(key, ""))
        for key in ("Task Description", "Notes")
        if str(task_row.get(key, "")).strip()
    )
    match = re.search(r"\b(\d+)\b", text)
    if match:
        return int(match.group(1))
    return default


def _find_conn_req_row(task_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in task_rows:
        if str(row.get("Progress Key", "")).strip() == "conn_req":
            return row
    for row in task_rows:
        task = str(row.get("Task Description", "")).lower()
        if "connection request" in task:
            return row
    return None


def _ensure_outreach_control_tab(creds: str, obf_url: str) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    created = False
    try:
        worksheet = get_worksheet(spreadsheet, OUTREACH_CONTROL_TAB)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=OUTREACH_CONTROL_TAB,
            rows=200,
            cols=len(OUTREACH_CONTROL_HEADERS),
        )
        created = True

    headers = worksheet.row_values(1)
    if not headers:
        worksheet.update(
            range_name=f"A1:{_col_to_a1(len(OUTREACH_CONTROL_HEADERS))}1",
            values=[OUTREACH_CONTROL_HEADERS],
            value_input_option="USER_ENTERED",
        )
        headers = OUTREACH_CONTROL_HEADERS[:]
    missing = [header for header in OUTREACH_CONTROL_HEADERS if header not in headers]
    if missing:
        existing_len = len(headers)
        start_col = existing_len + 1
        end_col = existing_len + len(missing)
        worksheet.update(
            range_name=f"{_col_to_a1(start_col)}1:{_col_to_a1(end_col)}1",
            values=[missing],
            value_input_option="USER_ENTERED",
        )
        headers = headers + missing
    return {
        "ok": True,
        "spreadsheet_title": spreadsheet.title,
        "worksheet": OUTREACH_CONTROL_TAB,
        "created": created,
        "headers": headers,
    }


def _normalize_control_status(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _control_target(row: dict[str, Any]) -> int:
    effective = safe_number(row.get("Effective Target", ""), None)
    if effective is not None and int(effective) > 0:
        return int(effective)
    base = safe_number(row.get("Base Target", ""), None)
    if base is None or int(base) <= 0:
        raise ValueError("Outreach Control row must define Effective Target or Base Target.")
    rollover = int(safe_number(row.get("Rollover", ""), 0) or 0)
    return max(0, int(base) + rollover)


def _control_prospects_start_row(row: dict[str, Any]) -> int | None:
    value = safe_number(row.get(OUTREACH_CONTROL_PROSPECTS_START_ROW, ""), None)
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed >= 2 else None


def _balanced_lane_targets(volume: int) -> dict[str, int]:
    """Split a prep volume as evenly as possible across the two offers."""
    volume = max(0, int(volume))
    if not volume:
        return {}
    automation = volume // 2
    return {"Design": volume - automation, "Automation": automation}


def _control_lane_targets(row: dict[str, Any], remaining: int | None = None) -> dict[str, int]:
    """Every two-account prep receives an explicit, target-aware lane split.

    Older Outreach Control rows did not carry a lane marker. Deriving the split
    from the remaining target keeps those rows safe too, instead of allowing an
    all-Design or all-Automation execution merely because the old note is absent.
    """
    volume = _control_target(row) if remaining is None else max(0, int(remaining))
    return _balanced_lane_targets(volume)


def _latest_successful_control_row(
    rows: list[dict[str, Any]],
    date_value: str,
) -> dict[str, Any] | None:
    """Find the newest completed control row before ``date_value``.

    A row is a successful OBF reference only once it is marked Done and its
    progress reached its effective target.  Planned/partial rows must never
    silently become the source for a new autonomous send target.
    """
    target_day = _parse_date(date_value)
    candidates: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        try:
            row_day = _parse_date(str(row.get("Date", "")))
            target = _control_target(row)
        except (TypeError, ValueError):
            continue
        progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
        if (
            row_day < target_day
            and _normalize_control_status(row.get("Status", "")) == "done"
            and progress >= target
        ):
            candidates.append((row_day, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _append_auto_created_control_row(
    worksheet: Any,
    headers: list[str],
    rows: list[dict[str, Any]],
    date_value: str,
) -> dict[str, Any]:
    """Append an approved weekday control row when the daily row is missing."""
    source = _latest_successful_control_row(rows, date_value)
    source_target: int | None = None
    source_start_row: int | None = None
    source_date = ""
    if source:
        source_target = _control_target(source)
        source_start_row = _control_prospects_start_row(source)
        source_date = format_sheet_date(source.get("Date", ""))

    target = source_target if source_target and source_target > 0 else DEFAULT_TARGET
    lane_targets = _balanced_lane_targets(target)
    lane_note = f" {DEFAULT_LANE_SPLIT_MARKER}" if lane_targets else ""
    note = (
        f"Auto-created for {date_value} from completed Outreach Control row {source_date}; "
        f"target copied as {target}.{lane_note}"
        if source_date
        else f"Auto-created for {date_value}; no completed prior row found, using default target {target}.{lane_note}"
    )
    data: dict[str, Any] = {
        "Date": date_value,
        "Base Target": str(target),
        "Rollover": "0",
        "Effective Target": str(target),
        "Current Progress": "0",
        "Status": "Planned",
        "Approved": "TRUE",
        OUTREACH_CONTROL_PROSPECTS_START_ROW: str(source_start_row) if source_start_row else "",
        "Notes": note,
    }
    worksheet.append_row(
        [data.get(header, "") for header in headers],
        value_input_option="USER_ENTERED",
    )
    return {
        "created": True,
        "source_date": source_date or None,
        "target_source": "previous_completed_row" if source else "default",
        "target": target,
        "prospects_start_row": source_start_row,
        "lane_targets": lane_targets,
        "notes": note,
    }


def _read_outreach_control(
    creds: str,
    obf_url: str,
    date_value: str,
    mock_daily: dict[str, Any] | None = None,
    auto_create_missing: bool = False,
    auto_approve_existing: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    if mock_daily:
        # Compatibility shim for existing dry/unit fixtures.
        if "control_row" in mock_daily:
            row = mock_daily["control_row"]
            row.setdefault("_row_number", 2)
            status = _normalize_control_status(row.get("Status", ""))
            approved = (
                is_checked_value(row.get("Approved", ""))
                or status in OUTREACH_CONTROL_TERMINAL_STATUSES
            )
            target = _control_target(row)
            current_progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
            remaining = max(0, target - current_progress)
            prospects_start_row = _control_prospects_start_row(row)
            approval = {
                "worksheet": OUTREACH_CONTROL_TAB,
                "date": date_value,
                "approved": approved,
                "approval_value": row.get("Approved", ""),
                "status": row.get("Status", ""),
                "row_number": row.get("_row_number"),
                "prospects_start_row": prospects_start_row,
            }
            return approval, row, target, remaining
        approval = mock_daily.get("approval_state", {})
        task_rows = mock_daily.get("task_rows", [])
        conn_req_row = _find_conn_req_row(task_rows)
        if not conn_req_row:
            raise ValueError("No conn_req Daily Actions row found in mock data.")
        target = _extract_target(conn_req_row)
        current_progress = int(safe_number(conn_req_row.get("Current Progress", ""), 0))
        return approval, conn_req_row, target, max(0, target - current_progress)

    _ensure_outreach_control_tab(creds, obf_url)
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, OUTREACH_CONTROL_TAB)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{OUTREACH_CONTROL_TAB} worksheet is empty.")
    headers = values[0]
    missing = [header for header in OUTREACH_CONTROL_HEADERS if header not in headers]
    if missing:
        raise ValueError(
            f"{OUTREACH_CONTROL_TAB} is missing required columns: {', '.join(missing)}"
        )
    date_idx = headers.index("Date")
    matches: list[dict[str, Any]] = []
    for sheet_row in range(2, len(values) + 1):
        raw = values[sheet_row - 1]
        padded = raw + [""] * (len(headers) - len(raw))
        cell = padded[date_idx] if date_idx < len(padded) else ""
        if sheet_values_equal(cell, date_value):
            item = {headers[i]: padded[i] for i in range(len(headers))}
            item["_row_number"] = sheet_row
            matches.append(item)

    auto_created: dict[str, Any] | None = None
    if not matches and auto_create_missing:
        rows = []
        for sheet_row in range(2, len(values) + 1):
            raw = values[sheet_row - 1]
            padded = raw + [""] * (len(headers) - len(raw))
            item = {headers[i]: padded[i] for i in range(len(headers))}
            item["_row_number"] = sheet_row
            rows.append(item)
        auto_created = _append_auto_created_control_row(worksheet, headers, rows, date_value)

        # Re-read after the append so formatting and row numbers come from the
        # sheet rather than from a locally assumed insertion position.
        values = worksheet.get_all_values()
        matches = []
        for sheet_row in range(2, len(values) + 1):
            raw = values[sheet_row - 1]
            padded = raw + [""] * (len(headers) - len(raw))
            cell = padded[date_idx] if date_idx < len(padded) else ""
            if sheet_values_equal(cell, date_value):
                item = {headers[i]: padded[i] for i in range(len(headers))}
                item["_row_number"] = sheet_row
                matches.append(item)

    if not matches:
        raise ValueError(
            f"No {OUTREACH_CONTROL_TAB} row found for {date_value}. "
            "Create today's row, set target, and approve it before prep."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple {OUTREACH_CONTROL_TAB} rows found for {date_value}. "
            "Keep exactly one control row per date."
        )

    row = matches[0]
    status = _normalize_control_status(row.get("Status", ""))
    auto_approved: dict[str, Any] | None = None
    if (
        auto_approve_existing
        and not is_checked_value(row.get("Approved", ""))
        and status not in OUTREACH_CONTROL_TERMINAL_STATUSES
    ):
        update = update_row(
            creds,
            obf_url,
            OUTREACH_CONTROL_TAB,
            int(row["_row_number"]),
            {"Approved": "TRUE"},
        )
        row["Approved"] = "TRUE"
        auto_approved = {
            "row_number": row["_row_number"],
            "preserved_fields": [
                "Base Target",
                "Rollover",
                "Effective Target",
                OUTREACH_CONTROL_PROSPECTS_START_ROW,
            ],
            "update": update,
        }
    target = _control_target(row)
    current_progress = int(safe_number(row.get("Current Progress", ""), 0) or 0)
    remaining = max(0, target - current_progress)
    prospects_start_row = _control_prospects_start_row(row)
    approved = (
        is_checked_value(row.get("Approved", "")) or status in OUTREACH_CONTROL_TERMINAL_STATUSES
    )
    approval = {
        "spreadsheet_title": spreadsheet.title,
        "worksheet": OUTREACH_CONTROL_TAB,
        "date": format_sheet_date(row.get("Date", date_value)),
        "approved": approved,
        "approval_value": row.get("Approved", ""),
        "status": row.get("Status", ""),
        "row_number": row.get("_row_number"),
        "target": target,
        "current_progress": current_progress,
        "remaining": remaining,
        "prospects_start_row": prospects_start_row,
    }
    if auto_created:
        approval["auto_created"] = auto_created
    if auto_approved:
        approval["auto_approved"] = auto_approved
    return approval, row, target, remaining


def _update_outreach_control_progress(
    creds: str,
    obf_url: str,
    row_number: int,
    progress: int,
    target: int,
    notes: str,
) -> dict[str, Any]:
    status = "Done" if progress >= target else "Partial"
    fields = {
        "Current Progress": str(progress),
        "Status": status,
        "Notes": notes,
    }
    result = update_row(creds, obf_url, OUTREACH_CONTROL_TAB, row_number, fields)
    result["action"] = "complete" if status == "Done" else "partial"
    return result


def configure_outreach_control(args: argparse.Namespace) -> dict[str, Any]:
    """Update the small set of daily OBF controls exposed by the local dashboard."""
    creds = str(Path(args.creds).expanduser())
    date_value = sheet_date(args.date)
    _verify_sheet_url_identity(args.obf_url, OBF_SHEET_URL, "Operation Brute Force")
    approval, control_row, target, remaining = _read_outreach_control(
        creds,
        args.obf_url,
        date_value,
    )

    fields: dict[str, str] = {}
    if args.daily_volume is not None:
        daily_volume = int(args.daily_volume)
        if daily_volume < 1 or daily_volume > DAILY_CONN_REQ_LIMIT:
            raise ValueError(f"Daily volume must be between 1 and {DAILY_CONN_REQ_LIMIT}.")
        current_progress = int(safe_number(control_row.get("Current Progress", ""), 0) or 0)
        if daily_volume < current_progress:
            raise ValueError(f"Daily volume cannot be below current progress ({current_progress}).")
        # The dashboard's volume is the effective work ceiling for the day.
        fields["Base Target"] = str(daily_volume)
        fields["Rollover"] = "0"
        fields["Effective Target"] = str(daily_volume)

    if args.prospects_start_row is not None:
        start_row = int(args.prospects_start_row)
        if start_row < 2:
            raise ValueError("Prospects start row must be 2 or greater.")
        fields[OUTREACH_CONTROL_PROSPECTS_START_ROW] = str(start_row)

    if args.approved is not None:
        fields["Approved"] = "TRUE" if str(args.approved).lower() == "true" else "FALSE"

    if not fields:
        return {
            "ok": True,
            "status": "unchanged",
            "date": date_value,
            "row_number": control_row.get("_row_number"),
            "approval_state": approval,
            "target_total": target,
            "target_remaining": remaining,
        }

    update = update_row(
        creds,
        args.obf_url,
        OUTREACH_CONTROL_TAB,
        int(control_row["_row_number"]),
        fields,
    )
    updated_target = int(fields.get("Effective Target", target))
    current_progress = int(safe_number(control_row.get("Current Progress", ""), 0) or 0)
    return {
        "ok": True,
        "status": "updated",
        "date": date_value,
        "row_number": control_row.get("_row_number"),
        "fields": fields,
        "target_total": updated_target,
        "target_remaining": max(0, updated_target - current_progress),
        "update": update,
    }


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


def _make_summary_message(result: dict[str, Any]) -> str:
    blockers = result.get("blockers") or ["None"]
    journal = result.get("journal", {}) if isinstance(result.get("journal"), dict) else {}
    return "\n".join(
        [
            "LinkedIn outreach summary",
            f"Connection requests sent: {result.get('successful_sends', 0)}",
            f"Skipped: {len(result.get('skipped', []))}",
            f"Blocked: {', '.join(blockers)}",
            f"Quota remaining: {result.get('quota_remaining', {})}",
            f"Journal path: {journal.get('path', '')}",
            f"Pending sync: {journal.get('pending_sync', 0)}",
            f"Journal confirmed sends: {journal.get('confirmed_sends', result.get('successful_sends', 0))}",
            f"Diversions executed: {result.get('runtime_enforcement', {}).get('diversions_executed', 0)}",
            f"Diversion failures: {len(result.get('runtime_enforcement', {}).get('diversion_failures', []))}",
            f"Engagement opportunities: {len(result.get('engagement_opportunities', []))}",
        ]
    )


def _make_prepare_summary_message(result: dict[str, Any]) -> str:
    blockers = result.get("blockers") or ["None"]
    lane_assignment = result.get("lane_auto_assignment") or {}
    assignments = lane_assignment.get("assignments") or []
    lane_counts = lane_assignment.get("assigned_counts") or {}
    return "\n".join(
        [
            "LinkedIn outreach prep summary",
            f"Ready: {result.get('ready', False)}",
            f"Target remaining: {result.get('target_remaining', 0)}",
            f"Prospects available: {result.get('queue_count', 0)}",
            f"Planned sends: {result.get('planned_count', 0)}",
            f"Auto-assigned blank lanes: {len(assignments)} {lane_counts if assignments else ''}".rstrip(),
            f"Blocked: {', '.join(blockers)}",
        ]
    )


def _make_acceptance_summary_message(result: dict[str, Any]) -> str:
    blockers = result.get("blockers") or ["None"]
    lines = [
        "LinkedIn acceptance summary",
        f"Status: {result.get('status', 'unknown')}",
        f"Strategy: {'subtractive (sent invitations)' if 'sent_invitations_subtractive' in result.get('checked_sources', []) else 'additive (connections page)'}",
        f"Pending prospects: {result.get('pending_count', 0)}",
        f"Sent invitations on page: {result.get('sent_invitations_count', 'n/a')}",
        f"New accepts: {len(result.get('matched_acceptances', []))}",
        f"Declines/expired: {len(result.get('declines', []))}",
        f"Still pending: {len(result.get('still_pending', []))}",
        f"Verification errors: {len(result.get('subtractive_errors', []))}",
        f"Unmatched seen: {len(result.get('unmatched_acceptances', []))}",
        f"Drafts prepared: {len(result.get('draft_messages', []))}",
        f"Next eligible check: {result.get('next_check_not_before', 'n/a')}",
        f"Blocked: {', '.join(blockers)}",
    ]
    # Show verification errors so silent drops are visible
    for err in result.get("subtractive_errors", []):
        err_name = err.get("contact_name") or err.get("name") or "?"
        lines.append(f"  ERROR: {err_name} — {err.get('error', 'unknown')}")

    for draft in result.get("draft_messages", []):
        lines.extend(
            [
                "",
                f"Draft for {draft.get('contact_name', '')} | {draft.get('company', '')}",
                f"Template: {draft.get('template_id', '')}",
                str(draft.get("message", "")).strip(),
            ]
        )
    return "\n".join(lines)


def _activity_summary(activity: dict[str, Any]) -> str:
    if not activity:
        return "No activity data returned"
    window = activity.get("activity_window_counts", {}) if isinstance(activity, dict) else {}
    within_7d = int(window.get("within_7d", len(activity.get("recent_items", []))) or 0)
    within_30d = int(window.get("within_30d", within_7d) or 0)
    parseable = int(window.get("parseable", within_30d) or 0)
    unparsed = int(window.get("unparsed", 0) or 0)
    total_visible = activity.get("total_visible", 0)
    source_tab = str(activity.get("source_tab", "")).strip() or "unknown"
    return (
        f"source_tab={source_tab}; "
        f"within_7d={within_7d}; "
        f"within_30d={within_30d}; "
        f"parseable={parseable}; "
        f"unparsed={unparsed}; "
        f"total_visible={total_visible}"
    )


def _activity_dropdown_value(activity: dict[str, Any]) -> str:
    """Map activity windows to Prospects dropdown values.

    Very active: engagement within 7 days
    Active: engagement within 30 days (but not within 7 days)
    Not active: no detected engagement within 30 days
    """
    if not activity:
        return "Not active"
    window = activity.get("activity_window_counts", {}) if isinstance(activity, dict) else {}
    within_7d = int(window.get("within_7d", 0) or 0)
    within_30d = int(window.get("within_30d", 0) or 0)
    if within_7d > 0:
        return "Very active"
    if within_30d > 0:
        return "Active"

    # Backward-compatible fallback for legacy activity payloads.
    if bool(activity.get("is_active")) or len(activity.get("recent_items", [])) > 0:
        return "Very active"
    return "Not active"


def _activity_classification_uncertain(activity: dict[str, Any]) -> bool:
    return bool(activity.get("activity_classification_uncertain"))


def _live_prospect_activity_value(
    *,
    creds: str,
    obf_url: str,
    prospect: dict[str, Any],
    activity_field_name: str,
) -> tuple[str | None, str | None]:
    try:
        row_number = int(prospect["_row_number"])
        client = get_client(creds)
        spreadsheet = open_sheet(client, obf_url)
        worksheet = get_worksheet(spreadsheet, "Prospects")
        headers = [str(header).strip() for header in worksheet.row_values(1)]
        if activity_field_name not in headers:
            return None, f"missing_activity_column:{activity_field_name}"
        col_number = headers.index(activity_field_name) + 1
        value = worksheet.cell(row_number, col_number).value
        return str(value or "").strip(), None
    except Exception as exc:
        return None, str(exc)


def _read_and_sync_activity(
    result: dict[str, Any],
    session: Any,
    prospect: dict[str, Any],
    profile_url: str,
    date_value: str,
    creds: str,
    obf_url: str,
    write_phase: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        activity = session.read_activity(profile_url, max_seconds=ACTIVITY_READ_TIMEOUT_SEC)
    except TypeError:
        activity = session.read_activity(profile_url)
    if activity.get("error"):
        danger = str(activity.get("danger", "activity_read_failed"))
        if danger in {"activity_read_timeout", "activity_load_timeout"}:
            activity_summary = (
                f"activity_read={danger}; source_tab={activity.get('source_tab', '')}; "
                f"elapsed_sec={activity.get('elapsed_sec', '')}; reason={activity.get('reason', '')}"
            )
            _record_journal_event(
                result,
                date_value,
                "prospect_activity_sync",
                "skipped_timeout",
                prospect=prospect,
                write_phase=write_phase,
                activity_summary=activity_summary,
                activity=activity,
            )
            result.setdefault("warnings", []).append(
                f"Activity read timed out {write_phase} for prospect {prospect.get('id')}; no activity field was written"
            )
            return activity, activity_summary, {}
        raise RuntimeError(activity.get("danger", "activity_read_failed"))

    activity_summary = _activity_summary(activity)
    activity_fields: dict[str, Any] = {}
    if _activity_classification_uncertain(activity):
        _record_journal_event(
            result,
            date_value,
            "prospect_activity_sync",
            "skipped_uncertain",
            prospect=prospect,
            write_phase=write_phase,
            activity_summary=activity_summary,
        )
        result.setdefault("warnings", []).append(
            f"Activity classification skipped {write_phase} for prospect {prospect.get('id')}: timestamps were not parseable"
        )
        return activity, activity_summary, activity_fields

    activity_value = _activity_dropdown_value(activity)
    activity_fields = build_prospect_activity_fields(prospect, activity_value)
    activity_field_name = next(iter(activity_fields.keys()))
    live_activity, live_error = _live_prospect_activity_value(
        creds=creds,
        obf_url=obf_url,
        prospect=prospect,
        activity_field_name=activity_field_name,
    )
    if live_error:
        _record_journal_event(
            result,
            date_value,
            "prospect_activity_sync",
            "recorded_local_only",
            prospect=prospect,
            write_phase=write_phase,
            activity_summary=activity_summary,
            activity_value=activity_value,
            activity_field=activity_field_name,
            sheet_write_skipped=True,
            skip_reason="activity_preserve_check_failed",
            error=live_error,
        )
        result.setdefault("warnings", []).append(
            f"Activity write skipped for prospect {prospect.get('id')}: could not confirm existing {activity_field_name} was blank"
        )
        return activity, activity_summary, activity_fields
    if live_activity:
        _record_journal_event(
            result,
            date_value,
            "prospect_activity_sync",
            "recorded_local_only",
            prospect=prospect,
            write_phase=write_phase,
            activity_summary=activity_summary,
            activity_value=activity_value,
            activity_field=activity_field_name,
            existing_activity=live_activity,
            sheet_write_skipped=True,
            skip_reason="manual_activity_present",
        )
        result.setdefault("activity_updates", []).append(
            {
                "updated": False,
                "row": int(prospect["_row_number"]),
                "field": activity_field_name,
                "value": live_activity,
                "detected_value": activity_value,
                "reason": "manual_activity_present",
            }
        )
        return activity, activity_summary, activity_fields
    activity_payload = _sheet_target_payload(
        target="Prospects",
        row_number=int(prospect["_row_number"]),
        fields=activity_fields,
    )
    _record_journal_event(
        result,
        date_value,
        "prospect_activity_sync",
        "recorded",
        prospect=prospect,
        write_phase=write_phase,
        sheet_payload=activity_payload,
    )
    try:
        apply_prospect_fields(
            prospect_row=int(prospect["_row_number"]),
            fields=activity_fields,
            credentials_path=creds,
            sheet_url=obf_url,
        )
        field_name, field_value = next(iter(activity_fields.items()))
        result.setdefault("activity_updates", []).append(
            {
                "updated": True,
                "row": int(prospect["_row_number"]),
                "field": field_name,
                "value": field_value,
            }
        )
        _record_journal_event(
            result,
            date_value,
            "prospect_activity_sync",
            "synced",
            prospect=prospect,
            write_phase=write_phase,
            sheet_payload=activity_payload,
        )
    except Exception as exc:
        result["sync_failures"].append(
            {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
        )
        _record_journal_event(
            result,
            date_value,
            "prospect_activity_sync",
            "pending_sync",
            prospect=prospect,
            write_phase=write_phase,
            sheet_payload=activity_payload,
            error=str(exc),
        )
    return activity, activity_summary, activity_fields


def _reconcile_profile_state(
    result: dict[str, Any],
    prospect: dict[str, Any],
    state: str,
    date_value: str,
    creds: str,
    obf_url: str,
) -> dict[str, Any]:
    if state == "already_pending":
        fields = build_connection_sent_fields(
            prospect=prospect,
            notes="reconciled from live LinkedIn pending state",
            today_value=str(prospect.get("date_queued", "")).strip() or None,
        )
        payload = _sheet_target_payload(
            target="Prospects",
            row_number=int(prospect["_row_number"]),
            fields=fields,
        )
        _record_journal_event(
            result,
            date_value,
            "prospect_reconciliation",
            "recorded",
            prospect=prospect,
            live_state=state,
            sheet_payload=payload,
        )
        try:
            apply_prospect_fields(
                prospect_row=int(prospect["_row_number"]),
                fields=fields,
                credentials_path=creds,
                sheet_url=obf_url,
            )
            _record_journal_event(
                result,
                date_value,
                "prospect_reconciliation",
                "synced",
                prospect=prospect,
                live_state=state,
                sheet_payload=payload,
            )
            return {"synced": True, "fields": fields}
        except Exception as exc:
            result["sync_failures"].append(
                {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
            )
            _record_journal_event(
                result,
                date_value,
                "prospect_reconciliation",
                "pending_sync",
                prospect=prospect,
                live_state=state,
                sheet_payload=payload,
                error=str(exc),
            )
            return {"synced": False, "fields": fields, "error": str(exc)}
    elif state == "already_connected":
        fields = {"Outreach Status": "Connected"}
        _record_journal_event(
            result,
            date_value,
            "prospect_reconciliation",
            "recorded",
            prospect=prospect,
            live_state=state,
            sheet_payload=_sheet_target_payload(
                target="Prospects",
                row_number=int(prospect["_row_number"]),
                fields=fields,
            ),
        )
        try:
            mark_connected(
                int(prospect["_row_number"]),
                credentials_path=creds,
                sheet_url=obf_url,
            )
            _record_journal_event(
                result,
                date_value,
                "prospect_reconciliation",
                "synced",
                prospect=prospect,
                live_state=state,
            )
            return {"synced": True, "fields": fields}
        except Exception as exc:
            result["sync_failures"].append(
                {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
            )
            _record_journal_event(
                result,
                date_value,
                "prospect_reconciliation",
                "pending_sync",
                prospect=prospect,
                live_state=state,
                error=str(exc),
            )
            return {"synced": False, "fields": fields, "error": str(exc)}
    return {"synced": False, "error": f"unsupported_state:{state}"}


def _build_requires_email_fields(prospect: dict[str, Any]) -> dict[str, Any]:
    existing_notes = str(prospect.get("notes", "") or "").strip()
    note_fragment = f"LinkedIn requires email to connect {date.today().isoformat()}"
    notes = f"{existing_notes} | {note_fragment}" if existing_notes else note_fragment
    return {
        "Outreach Status": OUTREACH_STATUS_REQUIRES_EMAIL,
        "Notes": notes,
    }


def _record_requires_email_status(
    *,
    result: dict[str, Any],
    prospect: dict[str, Any],
    date_value: str,
    creds: str,
    obf_url: str,
) -> dict[str, Any]:
    fields = _build_requires_email_fields(prospect)
    payload = _sheet_target_payload(
        target="Prospects",
        row_number=int(prospect["_row_number"]),
        fields=fields,
    )
    _record_journal_event(
        result,
        date_value,
        "prospect_requires_email",
        "recorded",
        prospect=prospect,
        sheet_payload=payload,
    )
    try:
        apply_prospect_fields(
            prospect_row=int(prospect["_row_number"]),
            fields=fields,
            credentials_path=creds,
            sheet_url=obf_url,
        )
        result.setdefault("prospect_updates", []).append(
            {"updated": True, "row": int(prospect["_row_number"]), "fields": fields}
        )
        _record_journal_event(
            result,
            date_value,
            "prospect_requires_email",
            "synced",
            prospect=prospect,
            sheet_payload=payload,
        )
        return {"synced": True, "fields": fields}
    except Exception as exc:
        result["sync_failures"].append(
            {"prospect_id": prospect.get("id"), "target": "Prospects", "error": str(exc)}
        )
        _record_journal_event(
            result,
            date_value,
            "prospect_requires_email",
            "pending_sync",
            prospect=prospect,
            sheet_payload=payload,
            error=str(exc),
        )
        return {"synced": False, "fields": fields, "error": str(exc)}


def _short_skip_dwell() -> None:
    time.sleep(random.uniform(2.0, 5.0))


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


def acceptance_check(args: argparse.Namespace) -> dict[str, Any]:
    creds = str(Path(args.creds).expanduser())
    date_value = sheet_date(args.date)
    now = datetime.now()
    mock_pending = _read_json(getattr(args, "mock_pending_json", None))
    mock_acceptances = _read_json(getattr(args, "mock_acceptances_json", None))
    state_path = (
        Path(getattr(args, "state_path", "")).expanduser()
        if getattr(args, "state_path", None)
        else ACCEPTANCE_STATE_FILE
    )
    state = _load_acceptance_state(state_path)
    state["seen_acceptances"] = _prune_seen_acceptances(state.get("seen_acceptances", {}), now)

    result: dict[str, Any] = {
        "ok": True,
        "date": date_value,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "blockers": [],
        "checked_sources": [],
        "matched_acceptances": [],
        "unmatched_acceptances": [],
        "duplicate_acceptances": [],
        "draft_messages": [],
        "state_path": str(state_path),
    }

    try:
        _verify_sheet_url_identity(args.obf_url, OBF_SHEET_URL, "Operation Brute Force")
    except Exception as exc:
        result.update({"ok": False, "status": "blocked_sheet_identity", "blockers": [str(exc)]})
        result["summary_message"] = _make_acceptance_summary_message(result)
        return result

    pending = _load_pending_queue(creds, args.obf_url, mock_pending)
    result["pending_count"] = len(pending)
    cadence = _schedule_acceptance_check(args, now)
    result["cadence"] = cadence
    result["next_check_not_before"] = cadence["next_check_not_before"]

    if not pending:
        result["status"] = "skipped_no_pending"
        if not result["dry_run"]:
            state.update(
                {
                    "last_checked_at": now.isoformat(timespec="seconds"),
                    "next_check_not_before": cadence["next_check_not_before"],
                    "last_status": result["status"],
                }
            )
            _save_acceptance_state(state, state_path)
        result["summary_message"] = _make_acceptance_summary_message(result)
        return result

    if not args.force:
        next_due = _parse_iso_datetime(state.get("next_check_not_before"))
        if next_due and now < next_due:
            result["status"] = "skipped_cadence"
            result["next_check_not_before"] = next_due.isoformat(timespec="seconds")
            result["summary_message"] = _make_acceptance_summary_message(result)
            return result

    by_url, by_name = _build_pending_indexes(pending)

    live_acceptances: list[dict[str, Any]] = []
    session = None
    first_message_templates: list[dict[str, Any]] = []

    try:
        if mock_acceptances is not None:
            # Mock mode — use provided acceptances directly (legacy additive path)
            live_acceptances = mock_acceptances.get("acceptances", mock_acceptances)
            result["checked_sources"].append("connections_mock")
        else:
            from linkedin_helper import LinkedInSession

            session = LinkedInSession()
            connect_result = session.connect(skip_rate_check=True)
            if not connect_result.get("ok"):
                result.update(
                    {
                        "ok": False,
                        "status": "blocked_preflight",
                        "blockers": [
                            connect_result.get("block_reason", "LinkedIn preflight failed")
                        ],
                        "preflight": connect_result,
                    }
                )
                result["summary_message"] = _make_acceptance_summary_message(result)
                return result

            # --- Primary strategy: subtractive (sent invitations page) ---
            try:
                subtractive = session.check_accepts_subtractive(pending)
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "status": "blocked_browser_action",
                        "blockers": [f"sent_invitations exception: {exc}"],
                    }
                )
                result["summary_message"] = _make_acceptance_summary_message(result)
                return result

            result["checked_sources"].append("sent_invitations_subtractive")
            result["sent_invitations_count"] = subtractive.get("sent_invitations_count", 0)
            result["still_pending"] = subtractive.get("still_pending", [])
            result["declines"] = subtractive.get("declines", [])
            result["subtractive_errors"] = subtractive.get("errors", [])

            # Debug counts so the summary always reveals what the subtractive step found
            result["subtractive_counts"] = {
                "missing_from_sent": subtractive.get("missing_from_sent_count", 0),
                "acceptances": len(subtractive.get("acceptances", [])),
                "declines": len(subtractive.get("declines", [])),
                "still_pending": len(subtractive.get("still_pending", [])),
                "errors": len(subtractive.get("errors", [])),
            }

            # Convert subtractive acceptances into the standard candidate format
            for acceptance in subtractive.get("acceptances", []):
                acceptance["source"] = "sent_page_subtractive"
                live_acceptances.append(acceptance)

    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass

    # --- Process candidates (works for both subtractive and mock paths) ---
    candidates: list[dict[str, Any]] = []
    seen_in_run: set[str] = set()
    for item in live_acceptances or []:
        candidate = dict(item)
        if "source" not in candidate:
            candidate["source"] = "unknown"
        fingerprint = _acceptance_fingerprint(candidate)
        if not fingerprint or fingerprint in seen_in_run:
            continue
        seen_in_run.add(fingerprint)
        candidate["fingerprint"] = fingerprint
        candidates.append(candidate)

    for acceptance in candidates:
        fingerprint = acceptance["fingerprint"]
        if fingerprint in state.get("seen_acceptances", {}):
            result["duplicate_acceptances"].append(acceptance)
            continue

        prospect, match_source, match_error = _match_pending_acceptance(acceptance, by_url, by_name)
        if prospect is None:
            result["unmatched_acceptances"].append(
                {
                    "name": acceptance.get("name", ""),
                    "url": acceptance.get("url", ""),
                    "source": acceptance.get("source", ""),
                    "reason": match_error,
                }
            )
            continue

        match_payload = {
            "prospect_id": prospect.get("id"),
            "outreach_log_row_number": prospect.get("_outreach_log_row_number")
            or prospect.get("_row_number"),
            "company": prospect.get("company"),
            "contact_name": prospect.get("contact_name"),
            "linkedin": prospect.get("contact_linkedin"),
            "match_source": match_source,
            "acceptance_source": acceptance.get("source", ""),
        }

        if not result["dry_run"]:
            try:
                log_row = int(prospect.get("_outreach_log_row_number") or prospect["_row_number"])
                existing_notes = str(prospect.get("notes", "")).strip()
                acceptance_note = f"accepted_source={acceptance.get('source', '')}"
                connected_notes = (
                    f"{existing_notes} | {acceptance_note}" if existing_notes else acceptance_note
                )
                log_update = mark_outreach_log_connected(
                    log_row,
                    notes=connected_notes,
                    credentials_path=creds,
                    sheet_url=args.obf_url,
                )
                pipeline_update = append_pipeline_row_for_acceptance(
                    prospect=prospect,
                    notes=connected_notes,
                    credentials_path=creds,
                    sheet_url=args.obf_url,
                )
                match_payload["outreach_log_update"] = log_update
                match_payload["pipeline_update"] = pipeline_update
            except Exception as exc:
                result["ok"] = False
                result["blockers"].append(
                    f"Acceptance sync failed for {prospect.get('contact_name') or prospect.get('id')}: {exc}"
                )
                result["status"] = "blocked_acceptance_sync"
                break
            state.setdefault("seen_acceptances", {})[fingerprint] = now.isoformat(
                timespec="seconds"
            )

        if not first_message_templates:
            first_message_templates = load_templates(
                category="FM", credentials_path=creds, sheet_url=args.obf_url
            )
        draft = _render_first_message_draft(
            prospect,
            first_message_templates,
        )
        if draft:
            result["draft_messages"].append(draft)
            match_payload["draft_template_id"] = draft.get("template_id", "")

        result["matched_acceptances"].append(match_payload)

    if not result["dry_run"]:
        state.update(
            {
                "last_checked_at": now.isoformat(timespec="seconds"),
                "next_check_not_before": cadence["next_check_not_before"],
                "last_status": "accepted_found"
                if result["matched_acceptances"]
                else "no_acceptances",
            }
        )
        _save_acceptance_state(state, state_path)

    if result.get("status") == "blocked_acceptance_sync":
        pass
    elif result["matched_acceptances"]:
        result["status"] = "accepted_found"
    elif result["unmatched_acceptances"]:
        result["status"] = "unmatched_acceptances"
    else:
        result["status"] = "no_acceptances"
    result["summary_message"] = _make_acceptance_summary_message(result)
    return result


def prepare_8_30_session(args: argparse.Namespace) -> dict[str, Any]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="8:30 AM LinkedIn outreach session runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seq = sub.add_parser("generate-sequence")
    p_seq.add_argument("--date", required=True)
    p_seq.add_argument("--target", type=int, default=DEFAULT_TARGET)
    p_seq.add_argument("--no-write", action="store_true")

    p_batch = sub.add_parser("generate-batch-sizes")
    p_batch.add_argument("--date", required=True)
    p_batch.add_argument("--creds", default=DEFAULT_CREDS)
    p_batch.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_batch.add_argument("--sequence-tab", default=OUTREACH_SEQUENCE_TAB)
    p_batch.add_argument("--no-write", action="store_true")

    p_div = sub.add_parser("generate-lead-diversions")
    p_div.add_argument("--date", required=True)
    p_div.add_argument("--creds", default=DEFAULT_CREDS)
    p_div.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_div.add_argument("--sequence-tab", default=OUTREACH_SEQUENCE_TAB)
    p_div.add_argument("--no-write", action="store_true")

    p_timing = sub.add_parser("generate-activity-timing")
    p_timing.add_argument("--date", required=True)
    p_timing.add_argument("--creds", default=DEFAULT_CREDS)
    p_timing.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_timing.add_argument("--sequence-tab", default=OUTREACH_SEQUENCE_TAB)
    p_timing.add_argument("--no-write", action="store_true")

    p_delay = sub.add_parser("generate-delay-seconds")
    p_delay.add_argument("--date", required=True)
    p_delay.add_argument("--creds", default=DEFAULT_CREDS)
    p_delay.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_delay.add_argument("--sequence-tab", default=OUTREACH_SEQUENCE_TAB)
    p_delay.add_argument("--min-sec", type=int, default=10)
    p_delay.add_argument("--max-sec", type=int, default=95)
    p_delay.add_argument("--no-write", action="store_true")

    p_div_sec = sub.add_parser("generate-lead-diversion-seconds")
    p_div_sec.add_argument("--date", required=True)
    p_div_sec.add_argument("--creds", default=DEFAULT_CREDS)
    p_div_sec.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_div_sec.add_argument("--sequence-tab", default=OUTREACH_SEQUENCE_TAB)
    p_div_sec.add_argument("--no-write", action="store_true")

    p_setup_control = sub.add_parser("setup-outreach-control")
    p_setup_control.add_argument("--creds", default=DEFAULT_CREDS)
    p_setup_control.add_argument("--obf-url", default=OBF_SHEET_URL)

    p_configure_control = sub.add_parser("configure-outreach-control")
    p_configure_control.add_argument("--date", required=True)
    p_configure_control.add_argument("--creds", default=DEFAULT_CREDS)
    p_configure_control.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_configure_control.add_argument("--daily-volume", type=int)
    p_configure_control.add_argument("--prospects-start-row", type=int)
    p_configure_control.add_argument("--approved", choices=["true", "false"])

    p_prepare = sub.add_parser("prepare-8_30-session")
    p_prepare.add_argument("--date", required=True)
    p_prepare.add_argument("--creds", default=DEFAULT_CREDS)
    p_prepare.add_argument("--task-manager-url", default=TASK_MANAGER_URL)
    p_prepare.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_prepare.add_argument("--no-write", action="store_true")
    p_prepare.add_argument("--mock-daily-json")
    p_prepare.add_argument("--mock-queue-json")
    p_prepare.add_argument("--worker-config", default=str(OUTREACH_WORKERS_CONFIG_PATH))
    p_prepare.add_argument("--prospects-tab", default="Prospects")

    p_run = sub.add_parser("run")
    p_run.add_argument("--date", required=True)
    p_run.add_argument("--creds", default=DEFAULT_CREDS)
    p_run.add_argument("--task-manager-url", default=TASK_MANAGER_URL)
    p_run.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--regenerate-sequence", action="store_true")
    p_run.add_argument("--max-sends", type=int, default=QUEUE_BUFFER_LIMIT)
    p_run.add_argument("--no-notes", action="store_true")
    p_run.add_argument("--require-prepared-session", action="store_true")
    p_run.add_argument("--skip-acceptance-rate-check", action="store_true")
    p_run.add_argument("--prepared-path")
    p_run.add_argument("--worker-id", help="Run only this immutable prepared account lane.")
    p_run.add_argument(
        "--verify-connection-modal",
        action="store_true",
        help="Open and dismiss each prepared connection modal without sending or writing reporting.",
    )
    p_run.add_argument("--mock-daily-json")
    p_run.add_argument("--mock-queue-json")
    p_run.add_argument("--mock-quotas-json")
    p_run.add_argument("--skip-warm-up", action="store_true")

    p_accept = sub.add_parser("acceptance-check")
    p_accept.add_argument("--date", required=True)
    p_accept.add_argument("--creds", default=DEFAULT_CREDS)
    p_accept.add_argument("--obf-url", default=OBF_SHEET_URL)
    p_accept.add_argument("--dry-run", action="store_true")
    p_accept.add_argument("--force", action="store_true")
    p_accept.add_argument("--no-notification-fallback", action="store_true")
    p_accept.add_argument("--state-path")
    p_accept.add_argument("--active-start-hour", type=int, default=8)
    p_accept.add_argument("--active-end-hour", type=int, default=22)
    p_accept.add_argument("--active-min-minutes", type=int, default=20)
    p_accept.add_argument("--active-max-minutes", type=int, default=45)
    p_accept.add_argument("--off-hours-min-minutes", type=int, default=90)
    p_accept.add_argument("--off-hours-max-minutes", type=int, default=150)
    p_accept.add_argument("--jitter-min-minutes", type=int, default=2)
    p_accept.add_argument("--jitter-max-minutes", type=int, default=8)
    p_accept.add_argument("--mock-pending-json")
    p_accept.add_argument("--mock-acceptances-json")
    p_accept.add_argument("--mock-notifications-json")

    args = parser.parse_args()
    try:
        if args.command == "generate-sequence":
            result = generate_sequence(sheet_date(args.date), args.target, write=not args.no_write)
        elif args.command == "generate-batch-sizes":
            result = generate_batch_sizes_from_sheet(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
                date_value=sheet_date(args.date),
                sequence_tab=args.sequence_tab,
                write=not args.no_write,
            )
        elif args.command == "generate-lead-diversions":
            result = generate_lead_diversions_from_sheet(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
                date_value=sheet_date(args.date),
                sequence_tab=args.sequence_tab,
                write=not args.no_write,
            )
        elif args.command == "generate-activity-timing":
            result = generate_activity_timing_from_sheet(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
                date_value=sheet_date(args.date),
                sequence_tab=args.sequence_tab,
                write=not args.no_write,
            )
        elif args.command == "generate-delay-seconds":
            result = generate_delay_seconds_from_sheet(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
                date_value=sheet_date(args.date),
                sequence_tab=args.sequence_tab,
                min_sec=args.min_sec,
                max_sec=args.max_sec,
                write=not args.no_write,
            )
        elif args.command == "generate-lead-diversion-seconds":
            result = generate_lead_diversion_seconds_from_sheet(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
                date_value=sheet_date(args.date),
                sequence_tab=args.sequence_tab,
                write=not args.no_write,
            )
        elif args.command == "setup-outreach-control":
            result = _ensure_outreach_control_tab(
                creds=str(Path(args.creds).expanduser()),
                obf_url=args.obf_url,
            )
        elif args.command == "configure-outreach-control":
            result = configure_outreach_control(args)
        elif args.command == "prepare-8_30-session":
            result = prepare_8_30_session(args)
        elif args.command == "acceptance-check":
            result = acceptance_check(args)
        else:
            result = run(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        print(
            json.dumps({"ok": False, "status": "fatal_exception", "error": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
