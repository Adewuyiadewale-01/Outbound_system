"""Danger classification and blocked-result shaping for activity reads.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S5). Pure move.
"""

import argparse
from datetime import datetime
from typing import Any

from outbound.activity_check.config import (
    BLOCKING_DANGER_PATTERNS,
    DEFER_ACTIVITY_REASONS,
    HARD_READ_DANGERS,
    HARD_READ_REASONS,
)
from outbound.activity_check.state import (
    activity_journal_path,
    activity_session_path,
    emit_progress,
    journal_event,
    save_session_state,
)
from outbound.activity_check.text import clean_text


def is_blocking_danger(value: Any) -> bool:
    text = clean_text(value).lower()
    return bool(text and any(pattern in text for pattern in BLOCKING_DANGER_PATTERNS))


def hard_read_failure_reason(detail: dict[str, Any]) -> str:
    if not isinstance(detail, dict) or not detail.get("error"):
        return ""
    danger = clean_text(detail.get("danger"))
    reason = clean_text(detail.get("reason"))
    if reason in DEFER_ACTIVITY_REASONS or danger in DEFER_ACTIVITY_REASONS:
        return ""
    if "invalid_profile_or_404" in danger or "invalid_profile_or_404" in reason:
        return "invalid_profile_or_404"
    if is_blocking_danger(danger) or is_blocking_danger(reason):
        return danger or reason or "blocking_danger"
    if danger in HARD_READ_DANGERS:
        return danger
    if danger:
        return danger
    if reason in HARD_READ_REASONS:
        return reason
    return ""


def blocked_result(
    *,
    args: argparse.Namespace,
    date_value: str,
    targets: list[dict[str, Any]],
    completed: int,
    skipped_resume: int,
    bridged: int,
    failures: list[dict[str, Any]],
    sequence: dict[str, Any],
    state: dict[str, Any] | None = None,
    status: str = "blocked",
) -> dict[str, Any]:
    result = {
        "ok": False,
        "status": status,
        "dry_run": bool(args.dry_run),
        "date": date_value,
        "source_tab": args.prefinal_tab,
        "final_tab": args.final_tab,
        "activity_sequence_tab": args.activity_sequence_tab,
        "targets": len(targets),
        "completed_this_run": completed,
        "resume_skips": skipped_resume,
        "bridged_rows": bridged,
        "failures": failures,
        "session_path": str(activity_session_path(date_value)),
        "journal_path": str(activity_journal_path(date_value)),
        "sequence": {k: v for k, v in sequence.items() if k != "plan"},
    }
    if state is not None and not args.dry_run:
        state["status"] = status
        state["blocked_at"] = datetime.now().isoformat(timespec="seconds")
        state["completed_this_run"] = completed
        state["resume_skips"] = skipped_resume
        state["bridged_rows_count"] = bridged
        state["failures"] = failures
        save_session_state(date_value, state)
        journal_event(
            date_value, "run_paused" if status.startswith("paused_") else "run_blocked", **result
        )
    emit_progress(date_value, status, **result)
    return result


def is_cdp_transport_exception(exc: Exception) -> bool:
    message = clean_text(exc).lower()
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or "cdp command" in message
        or "websocket" in message
    )
