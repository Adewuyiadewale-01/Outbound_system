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
import sys
from pathlib import Path
from typing import Any

from runtime_environment import load_repo_env

load_repo_env()

HELPERS_DIR = Path(__file__).resolve().parent
ROOT_DIR = HELPERS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

sys.path.insert(0, str(HELPERS_DIR))


from outbound.outreach.acceptance import (  # noqa: F401
    _acceptance_fingerprint,
    _build_pending_indexes,
    _is_active_hour,
    _load_acceptance_state,
    _load_pending_queue,
    _match_pending_acceptance,
    _pick_first_message_template,
    _prune_seen_acceptances,
    _render_first_message_draft,
    _save_acceptance_state,
    _schedule_acceptance_check,
    acceptance_check,
)
from outbound.outreach.activity import (  # noqa: F401
    _activity_classification_uncertain,
    _activity_dropdown_value,
    _activity_summary,
    _build_requires_email_fields,
    _live_prospect_activity_value,
    _read_and_sync_activity,
    _reconcile_profile_state,
    _record_requires_email_status,
    _short_skip_dwell,
)
from outbound.outreach.control_sheet import (  # noqa: F401
    _append_auto_created_control_row,
    _control_lane_targets,
    _control_prospects_start_row,
    _control_target,
    _ensure_outreach_control_tab,
    _extract_target,
    _find_conn_req_row,
    _latest_successful_control_row,
    _normalize_control_status,
    _read_outreach_control,
    _update_outreach_control_progress,
    configure_outreach_control,
)
from outbound.outreach.guards import (  # noqa: F401
    _ensure_session_healthy,
    _is_profile_ui_failure,
    _normalize_failure_key,
    _outreach_ui_sample_size,
    _record_early_profile_guard,
    _register_browser_failure,
    _register_send_failure,
    _reset_browser_failure_guard,
    _reset_send_failure_guard,
    _run_connection_modal_checks,
    _run_outreach_ui_preflight,
    _session_cdp_health_check,
    _sleep_failure_backoff,
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
from outbound.outreach.lanes import (  # noqa: F401
    _assign_outreach_workers,
    _auto_assign_missing_primary_lanes,
    _balanced_lane_targets,
    _persist_primary_lane_assignments,
    _read_outreach_workers,
    _select_queue_for_lane_targets,
)
from outbound.outreach.lead_machine import (  # noqa: F401
    _process_outreach_lead_state_machine,
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
from outbound.outreach.runner import (  # noqa: F401
    _approval_and_task,
    _extract_sheet_id,
    _is_obf_weekend,
    _load_prepared_session,
    _load_queue,
    _load_quotas,
    _prospect_reporting_missing_fields,
    _quota_capacity,
    _read_json,
    _recover_confirmed_send_sheet_sync,
    _verify_sheet_url_identity,
    _weekend_hold_message,
    prepare_8_30_session,
    run,
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
