"""CLI dispatch and result presentation for the outreach workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from outbound.outreach.acceptance import acceptance_check
from outbound.outreach.control_sheet import (
    _ensure_outreach_control_tab,
    configure_outreach_control,
)
from outbound.outreach.paths import (
    DEFAULT_CREDS,
    OBF_SHEET_URL,
    OUTREACH_WORKERS_CONFIG_PATH,
    TASK_MANAGER_URL,
)
from outbound.outreach.planning import (
    generate_sequence,
)
from outbound.outreach.policy import (
    DEFAULT_TARGET,
    OUTREACH_SEQUENCE_TAB,
    QUEUE_BUFFER_LIMIT,
)
from outbound.outreach.runner import prepare_8_30_session, run
from outbound.outreach.sequence_sheet import (
    generate_activity_timing_from_sheet,
    generate_batch_sizes_from_sheet,
    generate_delay_seconds_from_sheet,
    generate_lead_diversion_seconds_from_sheet,
    generate_lead_diversions_from_sheet,
)
from outbound.shared.dates import sheet_date


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
