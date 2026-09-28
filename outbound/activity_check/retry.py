"""Durable per-profile retry accounting for activity reads.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S3). Pure move.
"""

from datetime import datetime
from typing import Any

from outbound.activity_check.text import _parse_positive_int, clean_text
from outbound.shared.queue import load_batch, update_batch_status


def next_activity_retry_record(
    previous: dict[str, Any] | None,
    *,
    target: dict[str, Any],
    reason: str,
    danger: str,
    max_attempts: int,
) -> dict[str, Any]:
    """Build the durable retry state for one profile-level activity failure."""
    prior = previous or {}
    attempts = _parse_positive_int(prior.get("attempts")) + 1
    return {
        "attempts": attempts,
        "max_attempts": max_attempts,
        "exhausted": attempts >= max_attempts,
        "last_reason": clean_text(reason),
        "last_danger": clean_text(danger),
        "profile_url": clean_text(target.get("profile_url")),
        "company": clean_text(target.get("company")),
        "person_slot": clean_text(target.get("prefix")),
        "last_attempt_at": datetime.now().isoformat(timespec="seconds"),
    }


def persist_activity_retry_attempt(
    queue_fingerprint: str,
    *,
    target: dict[str, Any],
    reason: str,
    danger: str,
    max_attempts: int,
) -> dict[str, Any]:
    """Persist retry accounting so the cap survives dates and worker restarts."""
    batch = load_batch(queue_fingerprint)
    retry_state = dict(batch.get("activity_retry_state") or {})
    record = next_activity_retry_record(
        retry_state.get(target["key"]),
        target=target,
        reason=reason,
        danger=danger,
        max_attempts=max_attempts,
    )
    retry_state[target["key"]] = record
    update_batch_status(
        queue_fingerprint,
        clean_text(batch.get("status")) or "activity_in_progress",
        activity_retry_state=retry_state,
    )
    return record


def persist_terminal_activity_issue(
    queue_fingerprint: str,
    *,
    target: dict[str, Any],
    terminal_reason: str,
    retry_record: dict[str, Any],
) -> None:
    """Make an exhausted profile immediately ineligible for future sessions."""
    batch = load_batch(queue_fingerprint)
    issues = dict(batch.get("activity_issues") or {})
    issues[target["key"]] = {
        "terminal": True,
        "terminal_reason": clean_text(terminal_reason),
        "profile_url": clean_text(target.get("profile_url")),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "attempts": _parse_positive_int(retry_record.get("attempts")),
        "last_reason": clean_text(retry_record.get("last_reason")),
    }
    update_batch_status(
        queue_fingerprint,
        clean_text(batch.get("status")) or "activity_in_progress",
        activity_issues=issues,
    )
