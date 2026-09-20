#!/usr/bin/env python3
"""Standalone Pre-final activity checker with staged Final and Prospects bridges."""

import argparse
import json
import os
import random
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from linkedin_helper import (  # noqa: E402
    LinkedInSession,
    canonicalize_linkedin_profile_url,
    relative_days_from_time_text,
)
from linkedin_outreach_session import (  # noqa: E402
    OBF_SHEET_URL,
    _batch_update_cells,
    _find_first_index,
    _find_last_index,
    _is_enabled,
    _parse_int,
    _random_positive_partition,
    _run_diversion,
    sequence_date_key,
    sheet_date,
)
from prefinal_queue import (  # noqa: E402
    QUEUE_ROW_COLUMNS,
    enqueue_batch,
    load_batch,
    next_activity_batch,
    update_batch_status,
)
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import get_client, normalize_rows, open_sheet, require_columns  # noqa: E402

load_repo_env()


DEFAULT_LEADS_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
DEFAULT_PREFINAL_TAB = "Pre-final"
DEFAULT_FINAL_TAB = "Final"
DEFAULT_ACTIVITY_SEQUENCE_TAB = "Activity Sequence"
REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS

STATE_DIR = ROOT / "state" / "activity_sessions"
JOURNAL_DIR = ROOT / "state" / "activity_journal"

BASE_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "Use",
]
PERSON_COLUMNS = ["Name", "Title", "LinkedIn", "Email"]
P1_COLUMNS = [f"P1 {column}" for column in PERSON_COLUMNS]
P2_COLUMNS = [f"P2 {column}" for column in PERSON_COLUMNS]
ACTIVITY_COLUMNS = ["P1 Activity", "P2 Activity"]
FINAL_REQUIRED_COLUMNS = (
    BASE_COLUMNS
    + P1_COLUMNS
    + ACTIVITY_COLUMNS[:1]
    + P2_COLUMNS
    + ACTIVITY_COLUMNS[1:]
    + ["Category"]
)
CATEGORY_PRIORITY = {"Hyper": 0, "High": 1, "Alpha-medium": 2, "Medium": 3, "Low": 4}
ACTIVITY_SCORE = {"Very active": 2, "Active": 1, "Not active": 0, "": 0}
ACTIVITY_VALUES = {"Very active", "Active", "Not active", ""}
DIVERSION_OPTIONS = [
    ("none", 35),
    ("feed_scroll", 23),
    ("engagement_trail", 14),
    ("profile_drill", 10),
    ("company_page_browse", 9),
    ("recent_post_read", 9),
]
NAVIGATION_TYPE_OPTIONS = [
    ("Direct Url", 55),
    ("Selector-based", 45),
]
BLOCKING_DANGER_PATTERNS = (
    "captcha",
    "restriction",
    "login",
    "logged out",
    "email_verify",
    "email verify",
    "email verification",
    "robot_check",
    "robot check",
    "checkpoint",
    "security challenge",
    "danger detected",
    "unknown_danger",
    "chrome cdp not responding",
    "cdp connection failed",
)
HARD_READ_DANGERS = {"activity_read_timeout"}
HARD_READ_REASONS = {
    "navigation_failed_or_timed_out",
    "activity_page_not_ready",
    "activity_feed_not_hydrated",
    "activity_extract_failed_or_timed_out",
    "insufficient_budget_before_tab",
}
# A LinkedIn route can occasionally land on a visually blank shell: URL is loaded,
# CDP is reachable, but the app never hydrates. That is a per-profile/page load
# problem, not a reason to kill the whole activity lane.
DEFER_ACTIVITY_REASONS = {
    "activity_feed_not_hydrated",
    "activity_classification_uncertain",
    "activity_blank_page_not_ready",
    "activity_fresh_tab_retry_failed",
}
SKIP_ACTIVITY_REASONS = {"invalid_profile_or_404"}
PENDING_404_STATUS = "retry_pending_404"
DEFAULT_MAX_TARGET_ATTEMPTS = 4


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _parse_positive_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


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


def normalize_url(value: Any) -> str:
    raw = clean_text(value).replace(" ", "")
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    return raw


def is_linkedin_profile_url(value: Any) -> bool:
    return bool(canonical_linkedin_profile_url(value))


def normalized_profile_key(value: Any) -> str:
    return canonical_linkedin_profile_url(value).lower()


def canonical_linkedin_profile_url(value: Any) -> str:
    """Use one neutral LinkedIn profile URL at prep and execution boundaries."""
    return canonicalize_linkedin_profile_url(value)


def activity_session_path(date_value: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{sequence_date_key(date_value)}.json"


def activity_journal_path(date_value: str) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR / f"{sequence_date_key(date_value)}.jsonl"


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def journal_event(date_value: str, event_type: str, **payload: Any) -> None:
    append_jsonl(
        activity_journal_path(date_value),
        {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "date": sheet_date(date_value),
            "event_type": event_type,
            **payload,
        },
    )


def emit_progress(date_value: str, stage: str, **payload: Any) -> None:
    out = {
        "event": "prefinal_activity_progress",
        "emitted_at": datetime.now().isoformat(timespec="seconds"),
        "date": sheet_date(date_value),
        "stage": stage,
        **payload,
    }
    print(json.dumps(out, ensure_ascii=True), file=sys.stderr, flush=True)


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


def load_session_state(date_value: str) -> dict[str, Any]:
    path = activity_session_path(date_value)
    if not path.exists():
        return {"date": sheet_date(date_value), "targets": {}, "bridged_rows": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"date": sheet_date(date_value), "targets": {}, "bridged_rows": {}}
    payload.setdefault("targets", {})
    payload.setdefault("bridged_rows", {})
    return payload


def save_session_state(date_value: str, state: dict[str, Any]) -> None:
    state["date"] = sheet_date(date_value)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    activity_session_path(date_value).write_text(
        json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def prepare_session_state(
    *,
    date_value: str,
    state: dict[str, Any],
    targets: list[dict[str, Any]],
    runtime_plan: list[dict[str, Any]],
    sequence: dict[str, Any],
    queue_fingerprint: str,
    dry_run: bool,
) -> dict[str, Any]:
    prepared_targets = []
    for index, target in enumerate(targets):
        plan_item = runtime_plan[index] if index < len(runtime_plan) else {}
        original_profile_url = clean_text(target.get("original_profile_url")) or clean_text(
            target.get("profile_url")
        )
        profile_url = canonical_linkedin_profile_url(target.get("profile_url"))
        prepared_targets.append(
            {
                **target,
                "original_profile_url": original_profile_url,
                "profile_url": profile_url,
                "profile_url_normalized": profile_url != original_profile_url.rstrip("/"),
                "slot_id": plan_item.get("slot_id"),
                "batch_number": plan_item.get("batch_number"),
                "activity_log_timing": plan_item.get("activity_log_timing"),
                "delay_sec": plan_item.get("delay_sec"),
                "lead_diversion": plan_item.get("lead_diversion"),
                "activity_diversion_sec": plan_item.get("activity_diversion_sec"),
                "navigation_type": plan_item.get("navigation_type"),
                "batch_gap_sec": plan_item.get("batch_gap_sec"),
            }
        )
    state["prepared_at"] = state.get("prepared_at") or datetime.now().isoformat(timespec="seconds")
    state["status"] = "prepared"
    state["target_count"] = len(targets)
    state["queue_fingerprint"] = queue_fingerprint
    state["prepared_targets"] = prepared_targets
    state["runtime_plan"] = runtime_plan
    state["sequence_summary"] = {key: value for key, value in sequence.items() if key != "plan"}
    if not dry_run:
        save_session_state(date_value, state)
        journal_event(
            date_value,
            "session_prepared",
            target_count=len(targets),
            session_path=str(activity_session_path(date_value)),
            sequence_summary=state["sequence_summary"],
        )
    return state


def read_worksheet(
    credentials_path: Path, sheet_url: str, tab_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = spreadsheet.worksheet(tab_name)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{tab_name} is empty")
    return values[0], normalize_rows(values)


def person_from_row(row: dict[str, Any], prefix: str) -> dict[str, str]:
    return {
        "name": clean_text(row.get(f"{prefix} Name")),
        "title": clean_text(row.get(f"{prefix} Title")),
        "linkedin": normalize_url(row.get(f"{prefix} LinkedIn")),
        "email": clean_text(row.get(f"{prefix} Email")),
        "activity": clean_text(row.get(f"{prefix} Activity")),
    }


def set_person(row: dict[str, Any], prefix: str, person: dict[str, str]) -> None:
    row[f"{prefix} Name"] = clean_text(person.get("name"))
    row[f"{prefix} Title"] = clean_text(person.get("title"))
    row[f"{prefix} LinkedIn"] = normalize_url(person.get("linkedin"))
    row[f"{prefix} Email"] = clean_text(person.get("email"))
    row[f"{prefix} Activity"] = normalize_activity_value(person.get("activity", ""))


def normalize_activity_value(value: Any) -> str:
    text = clean_text(value)
    return text if text in ACTIVITY_VALUES else ""


def normalize_navigation_type(value: Any) -> str:
    text = clean_text(value).lower().replace("_", " ").replace("-", " ")
    if text in {"selector based", "selector", "click through", "clickthrough"}:
        return "Selector-based"
    if text in {"direct url", "direct", "url"}:
        return "Direct Url"
    return "Direct Url"


def navigation_type_key(value: Any) -> str:
    return (
        "selector_based" if normalize_navigation_type(value) == "Selector-based" else "direct_url"
    )


def target_key(row: dict[str, Any], prefix: str) -> str:
    lead_id = clean_text(row.get("ID")) or f"row:{row.get('_row_number')}"
    return f"{lead_id}:{prefix}"


def extract_targets(
    rows: list[dict[str, Any]],
    recorded_activity: dict[str, Any] | None = None,
    recorded_issues: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    recorded_activity = recorded_activity or {}
    recorded_issues = recorded_issues or {}
    targets: list[dict[str, Any]] = []
    for row in rows:
        if not clean_text(row.get("ID")) and not clean_text(row.get("Company")):
            continue
        for prefix in ("P1", "P2"):
            person = person_from_row(row, prefix)
            original_profile_url = clean_text(person.get("linkedin"))
            profile_url = canonical_linkedin_profile_url(original_profile_url)
            if not profile_url:
                continue
            # Once a durable queue decision exists, this profile is no longer
            # eligible for another Activity Check preparation.
            if normalize_activity_value(
                (recorded_activity.get(target_key(row, prefix)) or {}).get("activity_value", "")
            ):
                continue
            issue = recorded_issues.get(target_key(row, prefix)) or {}
            if (
                issue.get("terminal")
                and canonical_linkedin_profile_url(issue.get("profile_url")) == profile_url
            ):
                continue
            targets.append(
                {
                    "key": target_key(row, prefix),
                    "lead_id": clean_text(row.get("ID")),
                    "row_number": row.get("_row_number"),
                    "company": clean_text(row.get("Company")),
                    "prefix": prefix,
                    "profile_url": profile_url,
                    "original_profile_url": original_profile_url,
                    "profile_url_normalized": profile_url != original_profile_url.rstrip("/"),
                }
            )
    return targets


def is_aggregate_activity_container(activity: dict[str, Any]) -> bool:
    sample = clean_text(activity.get("card_text_sample")).lower()
    return sample.startswith("all activity posts comments") and "loaded " in sample


def non_aggregate_entries(tab: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in tab.get("activities", []) or []
        if not is_aggregate_activity_container(item)
    ]


def count_entries_within(tab: dict[str, Any], days_limit: int) -> int:
    count = 0
    for activity in non_aggregate_entries(tab):
        days = relative_days_from_time_text(activity.get("time_text", ""))
        if days is not None and days <= days_limit:
            count += 1
    return count


def activity_detail_should_defer(activity_detail: dict[str, Any]) -> bool:
    return activity_detail_empty_success_reason(activity_detail) == "activity_feed_not_hydrated"


def detail_contains_text(value: Any, needles: Sequence[str]) -> bool:
    lowered_needles = [needle.lower() for needle in needles if needle]
    if not lowered_needles:
        return False
    if isinstance(value, str):
        text = value.lower()
        return any(needle in text for needle in lowered_needles)
    if isinstance(value, dict):
        return any(detail_contains_text(item, lowered_needles) for item in value.values())
    if isinstance(value, list):
        return any(detail_contains_text(item, lowered_needles) for item in value)
    return False


def activity_detail_empty_success_reason(activity_detail: dict[str, Any]) -> str:
    if not isinstance(activity_detail, dict) or activity_detail.get("error"):
        return ""
    tabs = activity_detail.get("tabs", {}) or {}
    checked_tabs = [
        tabs.get(name, {}) or {} for name in ("posts", "reactions", "comments") if name in tabs
    ]
    if detail_contains_text(activity_detail, ("invalid_profile_or_404", "/404/")):
        return "invalid_profile_or_404"
    if not checked_tabs:
        return "activity_feed_not_hydrated"
    if not activity_level(activity_detail) and any(
        tab.get("activity_classification_uncertain") for tab in checked_tabs
    ):
        return "activity_classification_uncertain"
    if any(
        int(tab.get("total_visible", 0) or 0) > 0 or non_aggregate_entries(tab)
        for tab in checked_tabs
    ):
        return ""
    if all(
        clean_text((tab.get("feed_state") or {}).get("reason")) == "explicit_empty_state"
        for tab in checked_tabs
    ):
        return ""
    return "activity_feed_not_hydrated"


def activity_level(activity_detail: dict[str, Any]) -> str:
    if not activity_detail or activity_detail.get("error"):
        return ""
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    posts = tabs.get("posts", {}) or {}
    comments = tabs.get("comments", {}) or {}
    reactions = tabs.get("reactions", {}) or {}
    if not any(non_aggregate_entries(tab) for tab in (posts, comments, reactions)):
        checked_tabs = [
            tabs.get(name, {}) or {} for name in ("posts", "reactions", "comments") if name in tabs
        ]
        all_explicitly_empty = len(checked_tabs) == 3 and all(
            clean_text((tab.get("feed_state") or {}).get("reason")) == "explicit_empty_state"
            for tab in checked_tabs
        )
        if all_explicitly_empty:
            return "Not active"
        return ""
    if (
        count_entries_within(posts, 7) >= 1
        or count_entries_within(comments, 7) >= 2
        or count_entries_within(reactions, 7) >= 2
    ):
        return "Very active"
    if (
        count_entries_within(posts, 14) >= 1
        or count_entries_within(comments, 30) + count_entries_within(reactions, 30) >= 5
    ):
        return "Active"
    if any(tab.get("activity_classification_uncertain") for tab in (posts, comments, reactions)):
        return ""
    return "Not active"


def activity_evidence(profile_url: str, activity_detail: dict[str, Any]) -> dict[str, Any]:
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    evidence: dict[str, Any] = {
        "profile_url": profile_url,
        "original_profile_url": activity_detail.get("original_profile_url", "")
        if isinstance(activity_detail, dict)
        else "",
        "canonical_profile_url": activity_detail.get("canonical_profile_url", profile_url)
        if isinstance(activity_detail, dict)
        else profile_url,
        "profile_url_normalized": bool(activity_detail.get("profile_url_normalized"))
        if isinstance(activity_detail, dict)
        else False,
        "navigation_type_used": activity_detail.get("navigation_type_used", "")
        if isinstance(activity_detail, dict)
        else "",
        "fresh_tab_recovery": bool(activity_detail.get("fresh_tab_recovery"))
        if isinstance(activity_detail, dict)
        else False,
        "page_url": activity_detail.get("page_url", "")
        if isinstance(activity_detail, dict)
        else "",
        "reason": activity_detail.get("reason", "") if isinstance(activity_detail, dict) else "",
        "level": activity_level(activity_detail),
        "error": bool(activity_detail.get("error")) if isinstance(activity_detail, dict) else True,
        "danger": activity_detail.get("danger", "") if isinstance(activity_detail, dict) else "",
        "tabs": {},
    }
    for tab_name in ("posts", "comments", "reactions"):
        tab = tabs.get(tab_name, {}) or {}
        entries = []
        for item in non_aggregate_entries(tab)[:8]:
            time_text = clean_text(item.get("time_text"))
            entries.append(
                {
                    "time_text": time_text,
                    "days": relative_days_from_time_text(time_text),
                    "sample": clean_text(item.get("card_text_sample"))[:160],
                }
            )
        evidence["tabs"][tab_name] = {
            "total_visible": int(tab.get("total_visible", 0) or 0),
            "real_entries": len(non_aggregate_entries(tab)),
            "uncertain": bool(tab.get("activity_classification_uncertain")),
            "entries": entries,
        }
    return evidence


def activity_score(activity: str) -> int:
    return ACTIVITY_SCORE.get(normalize_activity_value(activity), 0)


def should_swap(p1_activity: str, p2_activity: str) -> bool:
    p2_score = activity_score(p2_activity)
    return p2_score > 0 and p2_score > activity_score(p1_activity)


def category_for_row(row: dict[str, Any]) -> str:
    activities = [
        normalize_activity_value(row.get("P1 Activity")),
        normalize_activity_value(row.get("P2 Activity")),
    ]
    if not any(activities):
        return ""
    linkedin_count = sum(
        1
        for url in (row.get("P1 LinkedIn"), row.get("P2 LinkedIn"))
        if is_linkedin_profile_url(url)
    )
    if linkedin_count >= 2:
        if "Very active" in activities:
            return "Hyper"
        if "Active" in activities:
            return "High"
        return "Low"
    if linkedin_count == 1:
        if "Very active" in activities:
            return "Alpha-medium"
        if "Active" in activities:
            return "Medium"
        return "Low"
    return ""


def rank_row_for_final(
    source_row: dict[str, Any], activity_by_prefix: dict[str, str]
) -> dict[str, Any]:
    ranked = {column: clean_text(source_row.get(column)) for column in BASE_COLUMNS}
    p1 = person_from_row(source_row, "P1")
    p2 = person_from_row(source_row, "P2")
    p1["activity"] = normalize_activity_value(activity_by_prefix.get("P1"))
    p2["activity"] = normalize_activity_value(activity_by_prefix.get("P2"))
    if should_swap(p1.get("activity", ""), p2.get("activity", "")):
        p1, p2 = p2, p1
    set_person(ranked, "P1", p1)
    set_person(ranked, "P2", p2)
    ranked["Category"] = category_for_row(ranked)
    ranked["Engaged Person"] = clean_text(source_row.get("Engaged Person")) or (
        "Person 1" if is_linkedin_profile_url(ranked.get("P1 LinkedIn")) else "Person 2"
    )
    return ranked


def final_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        CATEGORY_PRIORITY.get(clean_text(row.get("Category")), 99),
        0 if is_linkedin_profile_url(row.get("P2 LinkedIn")) else 1,
        clean_text(row.get("Company")).lower(),
    )


def is_cdp_transport_exception(exc: Exception) -> bool:
    message = clean_text(exc).lower()
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or "cdp command" in message
        or "websocket" in message
    )


class LiveActivityReader:
    def __init__(self, timeout: float, retries: int):
        self.timeout = timeout
        self.retries = retries
        self.session = LinkedInSession()
        self.connected = False
        self.force_selector_on_next_call = False
        self.last_recovery: dict[str, Any] = {}

    def connect(self) -> dict[str, Any]:
        if self.connected:
            return {"ok": True, "status": "already_connected"}
        result = self.session.connect(skip_rate_check=True)
        if not result.get("ok"):
            return result
        self.connected = True
        return result

    def close(self) -> None:
        if self.connected:
            self.session.disconnect()
            self.connected = False

    def recover_connection(self) -> dict[str, Any]:
        """Discard a frozen page target and reconnect through a clean tab."""
        cdp = self.session.cdp
        health = cdp.health_check()
        if health.get("status") == "ok":
            try:
                replacement = cdp.replace_page_target("about:blank")
            except Exception as exc:
                self.connected = False
                return {
                    "ok": False,
                    "action": "fresh_tab_replacement_failed",
                    "cdp_health": health,
                    "error": str(exc),
                }
            self.connected = False
            self.session = LinkedInSession()
            result = self.connect()
            self.force_selector_on_next_call = bool(result.get("ok"))
            self.last_recovery = {
                "ok": bool(result.get("ok")),
                "action": "frozen_tab_replaced",
                "cdp_health": health,
                "replacement": replacement,
                "preflight": result,
                "next_navigation_type": "selector_based",
            }
            return self.last_recovery

        # A worker must not restart Chrome itself.  The lane coordinator owns
        # handoffs between profiles and decides when a resting profile returns.
        return {"ok": False, "action": "cdp_unhealthy", "cdp_health": health}

    def __call__(self, profile_url: str, plan_item: dict[str, Any] | None = None) -> dict[str, Any]:
        original_profile_url = clean_text(profile_url)
        profile_url = canonical_linkedin_profile_url(profile_url)
        if not profile_url:
            return {
                "error": True,
                "danger": "invalid_profile_url",
                "reason": "invalid_profile_url",
                "original_profile_url": original_profile_url,
            }
        attempts: list[dict[str, Any]] = []
        best_detail: dict[str, Any] = {}
        best_score = -2
        forced_selector = self.force_selector_on_next_call
        self.force_selector_on_next_call = False
        navigation_type = (
            "selector_based"
            if forced_selector
            else navigation_type_key((plan_item or {}).get("navigation_type"))
        )
        connect_result = self.connect()
        if not connect_result.get("ok"):
            return {
                "error": True,
                "danger": connect_result.get("block_reason", "LinkedIn preflight failed"),
            }
        for _ in range(max(0, self.retries) + 1):
            detail = self.session.read_activity_detail(
                profile_url, max_seconds=self.timeout, navigation_type=navigation_type
            )
            detail = {
                **(detail if isinstance(detail, dict) else {}),
                "original_profile_url": original_profile_url,
                "canonical_profile_url": profile_url,
                "profile_url_normalized": profile_url != original_profile_url.rstrip("/"),
                "navigation_type_used": navigation_type,
                "fresh_tab_recovery": forced_selector,
            }
            post_read_danger = self.session._check_danger()
            if post_read_danger and not activity_level(detail):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": True,
                    "danger": post_read_danger,
                    "reason": post_read_danger,
                }
            if navigation_type == "direct_url" and self._should_try_selector_fallback(detail):
                selector_detail = self.session.read_activity_detail(
                    profile_url,
                    max_seconds=self.timeout,
                    navigation_type="selector_based",
                )
                selector_detail = {
                    **(selector_detail if isinstance(selector_detail, dict) else {}),
                    "fallback_from_navigation_type": "direct_url",
                    "fallback_navigation_type": "selector_based",
                    "fallback_trigger": clean_text(detail.get("reason"))
                    or clean_text(detail.get("danger")),
                    "direct_url_attempt": activity_evidence(profile_url, detail),
                }
                if activity_level(selector_detail) or not hard_read_failure_reason(selector_detail):
                    detail = selector_detail
                else:
                    detail = {
                        **detail,
                        "selector_fallback_attempt": activity_evidence(
                            profile_url, selector_detail
                        ),
                    }
            try:
                page_state = self.session.cdp and self.session.cdp.evaluate(
                    "JSON.stringify({url: window.location.href, text: (document.body && document.body.innerText || '').slice(0, 500)})"
                )
                page_state = json.loads(page_state) if page_state else {}
            except Exception:
                page_state = {}
            page_url = clean_text(page_state.get("url"))
            page_text_raw = clean_text(page_state.get("text"))
            page_text = page_text_raw.lower()
            if not activity_level(detail) and (
                "/404" in page_url
                or "this page doesn't exist" in page_text
                or "this page doesn’t exist" in page_text
            ):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": True,
                    "danger": "invalid_profile_or_404",
                    "reason": "invalid_profile_or_404",
                    "page_url": page_url,
                }
            elif (
                not activity_level(detail)
                and "/recent-activity/" in page_url
                and len(page_text_raw) < 20
            ):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": False,
                    "danger": "",
                    "reason": "activity_blank_page_not_ready",
                    "page_url": page_url,
                    "page_text_length": len(page_text_raw),
                    "deferred_empty_extract": True,
                }
            attempts.append(detail)
            level = activity_level(detail)
            score = activity_score(level) if level else -1
            if score > best_score:
                best_detail = detail
                best_score = score
            if score > 0:
                break
        if best_detail:
            best_detail["_attempts"] = [activity_evidence(profile_url, item) for item in attempts]
        return best_detail

    @staticmethod
    def _should_try_selector_fallback(detail: dict[str, Any]) -> bool:
        if not isinstance(detail, dict) or activity_level(detail):
            return False
        reason = clean_text(detail.get("reason"))
        danger = clean_text(detail.get("danger"))
        if reason == "activity_blank_page_not_ready":
            return True
        if danger == "activity_read_timeout" and reason in {
            "navigation_failed_or_timed_out",
            "activity_page_not_ready",
            "activity_feed_not_hydrated",
            "activity_extract_failed_or_timed_out",
            "insufficient_budget_before_tab",
        }:
            return True
        return False


def build_fixture_reader(fixture_path: str) -> Callable[..., dict[str, Any]]:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8")) if fixture_path else {}
    normalized = {normalized_profile_key(key): value for key, value in fixture.items()}
    return lambda profile_url, _plan_item=None: normalized.get(
        normalized_profile_key(profile_url), {}
    )


def build_synthetic_activity_reader() -> Callable[..., dict[str, Any]]:
    """Deterministic test-only activity evidence, varied by profile URL."""

    def reader(profile_url: str, _plan_item: dict[str, Any] | None = None) -> dict[str, Any]:
        bucket = sum(ord(char) for char in normalized_profile_key(profile_url)) % 3

        def entry(time_text):
            return {
                "time_text": time_text,
                "card_text_sample": "Synthetic test activity",
            }

        if bucket == 0:
            return {
                "tabs": {
                    "posts": {"activities": [entry("1d")], "total_visible": 1},
                    "comments": {"activities": []},
                    "reactions": {"activities": []},
                }
            }
        if bucket == 1:
            return {
                "tabs": {
                    "posts": {"activities": []},
                    "comments": {"activities": [entry("10d")] * 5, "total_visible": 5},
                    "reactions": {"activities": []},
                }
            }
        return {
            "tabs": {
                "posts": {"activities": []},
                "comments": {"activities": [entry("180d")], "total_visible": 1},
                "reactions": {"activities": []},
            }
        }

    return reader


def resolve_activity_sequence_columns(headers: list[str]) -> dict[str, int]:
    slot_col = _find_first_index(headers, "Slot ID")
    batch_size_col = _find_first_index(headers, "Batch Size")
    if slot_col is None or batch_size_col is None:
        raise ValueError("Activity Sequence must include Slot ID and Batch Size")
    lead_batch_col = _find_first_index(headers, "Batch #", start=slot_col + 1, end=batch_size_col)
    timing_col = _find_first_index(
        headers, "Activity Log Timing", start=slot_col + 1, end=batch_size_col
    )
    delay_col = _find_first_index(headers, "Delay Sec", start=slot_col + 1, end=batch_size_col)
    diversion_col = _find_first_index(
        headers, "Lead Diversion", start=slot_col + 1, end=batch_size_col
    )
    diversion_sec_col = _find_first_index(
        headers, "Activity Diversion Sec", start=slot_col + 1, end=batch_size_col
    )
    navigation_type_col = _find_first_index(
        headers, "Navigation type", start=slot_col + 1, end=batch_size_col
    )
    batch_batch_col = _find_last_index(
        headers, "Batch #", start=batch_size_col - 1, end=batch_size_col + 1
    )
    batch_enabled_col = _find_first_index(headers, "Enabled", start=batch_size_col + 1)
    missing = [
        name
        for name, value in {
            "Batch #": lead_batch_col,
            "Activity Log Timing": timing_col,
            "Delay Sec": delay_col,
            "Lead Diversion": diversion_col,
            "Activity Diversion Sec": diversion_sec_col,
            "Batch table Batch #": batch_batch_col,
            "Enabled": batch_enabled_col,
        }.items()
        if value is None
    ]
    if missing:
        raise ValueError(f"Activity Sequence missing required columns: {', '.join(missing)}")
    return {
        "slot_col": int(slot_col),
        "lead_batch_col": int(lead_batch_col),
        "timing_col": int(timing_col),
        "delay_col": int(delay_col),
        "diversion_col": int(diversion_col),
        "diversion_sec_col": int(diversion_sec_col),
        "navigation_type_col": -1 if navigation_type_col is None else int(navigation_type_col),
        "batch_batch_col": int(batch_batch_col),
        "batch_size_col": int(batch_size_col),
        "batch_enabled_col": int(batch_enabled_col),
    }


def generated_activity_timing(date_value: str, slot_ids: list[int]) -> dict[int, str]:
    rng = random.Random(
        f"prefinal-activity-timing:{sequence_date_key(date_value)}:{','.join(map(str, slot_ids))}"
    )
    return {
        slot_id: rng.choices(["before_diversion", "after_diversion"], weights=[55, 45], k=1)[0]
        for slot_id in slot_ids
    }


def generated_delay_seconds(
    date_value: str, slot_ids: list[int], min_sec: int, max_sec: int
) -> dict[int, int]:
    rng = random.Random(
        f"prefinal-activity-delay:{sequence_date_key(date_value)}:{min_sec}:{max_sec}:{','.join(map(str, slot_ids))}"
    )
    return {slot_id: rng.randint(min_sec, max_sec) for slot_id in slot_ids}


def generated_diversions(date_value: str, slot_ids: list[int]) -> dict[int, str]:
    rng = random.Random(
        f"prefinal-activity-diversion:{sequence_date_key(date_value)}:{','.join(map(str, slot_ids))}"
    )
    values = [item[0] for item in DIVERSION_OPTIONS]
    weights = [item[1] for item in DIVERSION_OPTIONS]
    return {slot_id: rng.choices(values, weights=weights, k=1)[0] for slot_id in slot_ids}


def generated_diversion_seconds(
    date_value: str, diversions: dict[int, str]
) -> dict[int, int | None]:
    rng = random.Random(
        f"prefinal-activity-diversion-sec:{sequence_date_key(date_value)}:{json.dumps(diversions, sort_keys=True)}"
    )
    bounds = {
        "feed_scroll": (12, 45),
        "engagement_trail": (25, 90),
        "profile_drill": (35, 120),
        "company_page_browse": (20, 80),
        "recent_post_read": (15, 65),
    }
    return {
        slot_id: (
            None if diversion in {"", "none"} else rng.randint(*bounds.get(diversion, (20, 75)))
        )
        for slot_id, diversion in diversions.items()
    }


def generated_navigation_types(date_value: str, slot_ids: list[int]) -> dict[int, str]:
    rng = random.Random(
        f"prefinal-activity-navigation:{sequence_date_key(date_value)}:{','.join(map(str, slot_ids))}"
    )
    values = [item[0] for item in NAVIGATION_TYPE_OPTIONS]
    weights = [item[1] for item in NAVIGATION_TYPE_OPTIONS]
    return {slot_id: rng.choices(values, weights=weights, k=1)[0] for slot_id in slot_ids}


def generated_batch_gaps(
    date_value: str, batch_numbers: list[int], min_sec: int, max_sec: int
) -> dict[int, int]:
    rng = random.Random(
        f"prefinal-activity-batch-gap:{sequence_date_key(date_value)}:{min_sec}:{max_sec}:{','.join(map(str, batch_numbers))}"
    )
    return {batch: rng.randint(min_sec, max_sec) for batch in batch_numbers}


def unique_ints_in_order(values: Sequence[int]) -> list[int]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def distribute_slots_to_batches(
    target_count: int, batch_numbers: Sequence[int], date_value: str
) -> dict[int, int]:
    unique_batches = unique_ints_in_order([int(batch) for batch in batch_numbers if int(batch) > 0])
    if target_count <= 0:
        return {}
    if not unique_batches:
        raise ValueError("Activity Sequence has no enabled batches")
    active_batches = unique_batches[:target_count]
    if len(active_batches) >= target_count:
        sizes = {
            batch: (1 if idx < target_count else 0) for idx, batch in enumerate(unique_batches)
        }
    else:
        chunks = _random_positive_partition(
            target_count,
            len(active_batches),
            random.Random(
                f"prefinal-activity-batches:{sequence_date_key(date_value)}:{target_count}:{active_batches}"
            ),
        )
        sizes = {batch: 0 for batch in unique_batches}
        sizes.update({batch: size for batch, size in zip(active_batches, chunks)})
    return sizes


def ensure_activity_sequence(
    *,
    creds: str,
    obf_url: str,
    sequence_tab: str,
    date_value: str,
    target_count: int,
    delay_min_sec: int,
    delay_max_sec: int,
    batch_gap_min_sec: int,
    batch_gap_max_sec: int,
    dry_run: bool,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = spreadsheet.worksheet(sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{sequence_tab} is empty")
    headers = values[0]
    cols = resolve_activity_sequence_columns(headers)

    existing_slots: list[dict[str, Any]] = []
    batch_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is not None and slot_id > 0:
            existing_slots.append({"row_number": idx, "slot_id": slot_id})
        batch_number = _parse_int(padded[cols["batch_batch_col"]])
        if batch_number is not None and batch_number > 0:
            batch_rows.append(
                {
                    "row_number": idx,
                    "batch_number": batch_number,
                    "enabled": _is_enabled(padded[cols["batch_enabled_col"]], default=True),
                }
            )
    existing_slots.sort(key=lambda item: item["slot_id"])
    batch_rows.sort(key=lambda item: item["batch_number"])
    enabled_batches = [row for row in batch_rows if row["enabled"]]
    if target_count > 0 and not enabled_batches:
        raise ValueError("Activity Sequence has no enabled batches")

    updates: list[tuple[int, int, Any]] = []
    append_rows: list[list[Any]] = []
    slot_rows = list(existing_slots)
    next_row = len(values) + 1
    next_slot = (max([row["slot_id"] for row in existing_slots]) + 1) if existing_slots else 1
    while len(slot_rows) < target_count:
        row_values = [""] * len(headers)
        row_values[cols["slot_col"]] = next_slot
        append_rows.append(row_values)
        slot_rows.append({"row_number": next_row, "slot_id": next_slot})
        next_row += 1
        next_slot += 1

    active_slots = slot_rows[:target_count]
    slot_ids = [row["slot_id"] for row in active_slots]
    batch_numbers = unique_ints_in_order([row["batch_number"] for row in enabled_batches])
    if target_count == 0:
        sizes = {batch: 0 for batch in batch_numbers}
    else:
        sizes = distribute_slots_to_batches(target_count, batch_numbers, date_value)

    timing_by_slot = generated_activity_timing(date_value, slot_ids)
    delay_by_slot = generated_delay_seconds(date_value, slot_ids, delay_min_sec, delay_max_sec)
    diversion_by_slot = generated_diversions(date_value, slot_ids)
    diversion_sec_by_slot = generated_diversion_seconds(date_value, diversion_by_slot)
    navigation_by_slot = generated_navigation_types(date_value, slot_ids)
    batch_gaps = generated_batch_gaps(
        date_value, batch_numbers, batch_gap_min_sec, batch_gap_max_sec
    )

    slot_to_batch: dict[int, int] = {}
    slot_index = 0
    for batch in batch_numbers:
        for _ in range(sizes.get(batch, 0)):
            if slot_index >= len(slot_ids):
                break
            slot_to_batch[slot_ids[slot_index]] = batch
            slot_index += 1
    if target_count > 0 and len(slot_to_batch) != len(slot_ids):
        raise ValueError(
            f"Activity Sequence failed to map every target slot: mapped {len(slot_to_batch)} of {len(slot_ids)}"
        )

    for batch_row in batch_rows:
        updates.append(
            (
                batch_row["row_number"],
                cols["batch_size_col"] + 1,
                str(sizes.get(batch_row["batch_number"], "" if batch_row["enabled"] else "")),
            )
        )
    for row in active_slots:
        slot_id = row["slot_id"]
        updates.extend(
            [
                (row["row_number"], cols["lead_batch_col"] + 1, slot_to_batch.get(slot_id, "")),
                (row["row_number"], cols["timing_col"] + 1, timing_by_slot[slot_id]),
                (row["row_number"], cols["delay_col"] + 1, delay_by_slot[slot_id]),
                (row["row_number"], cols["diversion_col"] + 1, diversion_by_slot[slot_id]),
                (
                    row["row_number"],
                    cols["diversion_sec_col"] + 1,
                    ""
                    if diversion_sec_by_slot[slot_id] is None
                    else diversion_sec_by_slot[slot_id],
                ),
            ]
        )
        if cols["navigation_type_col"] >= 0:
            updates.append(
                (row["row_number"], cols["navigation_type_col"] + 1, navigation_by_slot[slot_id])
            )
    for row in slot_rows[target_count:]:
        updates.extend(
            [
                (row["row_number"], cols["lead_batch_col"] + 1, ""),
                (row["row_number"], cols["timing_col"] + 1, ""),
                (row["row_number"], cols["delay_col"] + 1, ""),
                (row["row_number"], cols["diversion_col"] + 1, ""),
                (row["row_number"], cols["diversion_sec_col"] + 1, ""),
            ]
        )
        if cols["navigation_type_col"] >= 0:
            updates.append((row["row_number"], cols["navigation_type_col"] + 1, ""))

    if not dry_run:
        if append_rows:
            worksheet.append_rows(append_rows, value_input_option="USER_ENTERED")
        _batch_update_cells(worksheet, updates)

    plan = []
    for row in active_slots:
        slot_id = row["slot_id"]
        batch_number = slot_to_batch.get(slot_id)
        plan.append(
            {
                "slot_id": slot_id,
                "batch_number": batch_number,
                "activity_log_timing": timing_by_slot[slot_id],
                "delay_sec": delay_by_slot[slot_id],
                "lead_diversion": diversion_by_slot[slot_id],
                "activity_diversion_sec": diversion_sec_by_slot[slot_id],
                "navigation_type": navigation_by_slot[slot_id],
                "navigation_type_key": navigation_type_key(navigation_by_slot[slot_id]),
                "batch_gap_sec": batch_gaps.get(batch_number, 0),
            }
        )
    return {
        "ok": True,
        "sequence_tab": sequence_tab,
        "target_count": target_count,
        "existing_slots": len(existing_slots),
        "slots_added": max(0, target_count - len(existing_slots)),
        "enabled_batches": len(enabled_batches),
        "batch_sizes": [
            {
                "batch_number": batch,
                "size": sizes.get(batch, 0),
                "gap_sec": batch_gaps.get(batch, 0),
            }
            for batch in batch_numbers
        ],
        "navigation_type_column": cols["navigation_type_col"] >= 0,
        "navigation_counts": {
            "Direct Url": sum(1 for value in navigation_by_slot.values() if value == "Direct Url"),
            "Selector-based": sum(
                1 for value in navigation_by_slot.values() if value == "Selector-based"
            ),
        },
        "plan": plan,
        "dry_run": dry_run,
    }


def apply_diversion(
    session: Any | None, plan_item: dict[str, Any], activity_detail: dict[str, Any], dry_run: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "diversion_executed": False}
    if dry_run:
        result["dry_run"] = True
        return result
    diversion = clean_text(plan_item.get("lead_diversion")).lower() or "none"
    diversion_sec = plan_item.get("activity_diversion_sec")
    if session is not None and diversion != "none":
        diversion_result = _run_diversion(
            session=session,
            diversion=diversion,
            diversion_sec=diversion_sec,
            activity=activity_detail,
        )
        result["diversion"] = diversion_result
        result["diversion_executed"] = bool(diversion_result.get("executed"))
        if not diversion_result.get("ok", True):
            result["ok"] = False
            result["error"] = diversion_result.get("error", "diversion_failed")
            return result
    return result


def write_final_batch_upsert_and_sort(
    credentials_path: Path,
    sheet_url: str,
    final_tab: str,
    ranked_rows: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = spreadsheet.worksheet(final_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{final_tab} is empty")
    headers = values[0]
    require_columns(headers, FINAL_REQUIRED_COLUMNS, final_tab)
    rows = normalize_rows(values)
    final_rows = [
        dict(row) for row in rows if clean_text(row.get("ID")) or clean_text(row.get("Company"))
    ]
    updated_existing_ids: list[str] = []
    inserted_ids: list[str] = []
    positions_by_id = {
        clean_text(row.get("ID")): index
        for index, row in enumerate(final_rows)
        if clean_text(row.get("ID"))
    }
    for ranked_row in ranked_rows:
        lead_id = clean_text(ranked_row.get("ID"))
        if not lead_id:
            raise ValueError("Cannot bridge a Final row without an ID")
        existing_index = positions_by_id.get(lead_id)
        if existing_index is None:
            positions_by_id[lead_id] = len(final_rows)
            final_rows.append(ranked_row)
            inserted_ids.append(lead_id)
        else:
            final_rows[existing_index] = {**final_rows[existing_index], **ranked_row}
            updated_existing_ids.append(lead_id)
    final_rows.sort(key=final_sort_key)
    payload = [[row.get(header, "") for header in headers] for row in final_rows]
    if not dry_run:
        if payload:
            end_cell = gspread.utils.rowcol_to_a1(1 + len(payload), len(headers))
            worksheet.update(
                range_name=f"A2:{end_cell}", values=payload, value_input_option="USER_ENTERED"
            )
    final_positions = {
        clean_text(row.get("ID")): index + 2
        for index, row in enumerate(final_rows)
        if clean_text(row.get("ID"))
    }
    return {
        "rows_requested": len(ranked_rows),
        "rows_written": len(ranked_rows),
        "updated_existing_ids": updated_existing_ids,
        "inserted_ids": inserted_ids,
        "final_row_numbers": {
            lead_id: final_positions.get(lead_id) for lead_id in inserted_ids + updated_existing_ids
        },
        "rows_in_final": len(final_rows),
    }


def row_activity_from_state(row: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    out = {"P1": "", "P2": ""}
    for prefix in ("P1", "P2"):
        key = target_key(row, prefix)
        target_state = state.get("targets", {}).get(key, {})
        out[prefix] = normalize_activity_value(target_state.get("activity_value", ""))
    return out


def row_targets(row: dict[str, Any], target_keys: set) -> list[str]:
    return [
        target_key(row, prefix) for prefix in ("P1", "P2") if target_key(row, prefix) in target_keys
    ]


def target_has_activity_decision(state: dict[str, Any], key: str) -> bool:
    value = normalize_activity_value(
        state.get("targets", {}).get(key, {}).get("activity_value", "")
    )
    return bool(value)


def ready_final_rows(
    *,
    source_rows: list[dict[str, Any]],
    state: dict[str, Any],
    all_target_keys: set,
    planned_target_keys: set,
) -> list[dict[str, Any]]:
    """Return fully planned rows whose activity targets are all stored locally."""
    ranked_rows: list[dict[str, Any]] = []
    seen_lead_ids = set()
    for row in source_rows:
        keys = row_targets(row, all_target_keys)
        if (
            not keys
            or not set(keys).issubset(planned_target_keys)
            or any(not target_has_activity_decision(state, key) for key in keys)
        ):
            continue
        lead_id = clean_text(row.get("ID")) or f"row:{row.get('_row_number')}"
        if lead_id in seen_lead_ids:
            continue
        seen_lead_ids.add(lead_id)
        ranked_rows.append(rank_row_for_final(row, row_activity_from_state(row, state)))
    return ranked_rows


def final_source_row_count(
    source_rows: list[dict[str, Any]], all_target_keys: set, planned_target_keys: set
) -> int:
    return sum(
        1
        for row in source_rows
        if row_targets(row, all_target_keys)
        and set(row_targets(row, all_target_keys)).issubset(planned_target_keys)
    )


def bridge_ready_rows_to_final(
    *,
    args: argparse.Namespace,
    date_value: str,
    state: dict[str, Any],
    ranked_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    result = write_final_batch_upsert_and_sort(
        Path(args.credentials), args.sheet_url, args.final_tab, ranked_rows, args.dry_run
    )
    bridge_records = {}
    for ranked in ranked_rows:
        lead_id = clean_text(ranked.get("ID"))
        bridge_records[lead_id] = {
            "lead_id": lead_id,
            "company": clean_text(ranked.get("Company")),
            "category": ranked.get("Category", ""),
            "p1_activity": ranked.get("P1 Activity", ""),
            "p2_activity": ranked.get("P2 Activity", ""),
            "final_row_number": result.get("final_row_numbers", {}).get(lead_id),
            "dry_run": bool(args.dry_run),
            "bridged_at": datetime.now().isoformat(timespec="seconds"),
        }
    bridge_summary = {**result, "rows": bridge_records}
    if not args.dry_run:
        state["bridged_rows"] = bridge_records
        state["final_bridge"] = {
            "ok": True,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            **result,
        }
        save_session_state(date_value, state)
        journal_event(date_value, "final_bridge_batch", **state["final_bridge"])
    return bridge_summary


def bridge_final_to_prospects(args: argparse.Namespace, date_value: str) -> dict[str, Any]:
    """Reuse the idempotent Final -> Prospects bridge without spawning another process."""
    from lead_exec_research import bridge_prefinal_to_prospects, ensure_dirs

    ensure_dirs()
    bridge_args = argparse.Namespace(
        credentials=args.credentials,
        sheet_url=args.sheet_url,
        source_sheet_url=args.sheet_url,
        source_tab=args.final_tab,
        prospects_sheet_url=args.obf_url,
        prospects_tab=args.prospects_tab,
        target_date=date_value,
        force=False,
        dry_run=args.dry_run,
        skip_outreach_control=bool(args.test_synthetic_activity),
    )
    return bridge_prefinal_to_prospects(bridge_args)


def queue_source_rows(batch: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    rows = []
    for row_number, row in enumerate(batch.get("rows", []), start=2):
        rows.append({**row, "_row_number": row_number})
    return list(QUEUE_ROW_COLUMNS), rows


def no_queued_batch_result(args: argparse.Namespace, date_value: str) -> dict[str, Any]:
    result = {
        "ok": True,
        "status": "idle_no_queued_batch",
        "dry_run": bool(args.dry_run),
        "date": date_value,
        "source_tab": args.prefinal_tab,
        "targets": 0,
        "message": "No queued Pre-final batch is waiting for activity work.",
    }
    emit_progress(date_value, "idle_no_queued_batch", **result)
    return result


def resume_final_bridged_batch(
    args: argparse.Namespace, date_value: str, queue_batch: dict[str, Any]
) -> dict[str, Any]:
    fingerprint = clean_text(queue_batch.get("fingerprint"))
    try:
        bridge = bridge_final_to_prospects(args, date_value)
    except Exception as exc:
        bridge = {"ok": False, "error": str(exc)}
    if not args.dry_run and bridge.get("ok"):
        update_batch_status(
            fingerprint,
            "prospects_bridged",
            prospects_bridge={
                "recovered_at": datetime.now().isoformat(timespec="seconds"),
                "result": bridge,
            },
        )
    result = {
        "ok": bool(bridge.get("ok")),
        "status": "prospects_recovered" if bridge.get("ok") else "prospects_recovery_blocked",
        "dry_run": bool(args.dry_run),
        "date": date_value,
        "queue_fingerprint": fingerprint,
        "prospects_bridge": bridge,
    }
    emit_progress(date_value, "prospects_recovery", **result)
    return result


def finalize_prepared_session(
    *,
    args: argparse.Namespace,
    date_value: str,
    state: dict[str, Any],
    source_rows: list[dict[str, Any]],
    all_targets: list[dict[str, Any]],
    queue_fingerprint: str,
) -> dict[str, Any]:
    """Bridge a fully prepared session after its sequential worker lanes finish.

    This path never connects to Chrome.  It is deliberately the only place a
    split session can write Final/Prospects, which avoids a lane racing a
    second lane's state or publishing a partial batch.
    """
    target_keys = {target["key"] for target in all_targets}
    planned_target_keys = {
        target.get("key") for target in state.get("prepared_targets", []) if target.get("key")
    }
    source_target_keys = {
        target["key"]
        for target in extract_targets(source_rows, recorded_activity={}, recorded_issues={})
    }
    persisted_issues = (
        (load_batch(queue_fingerprint).get("activity_issues") or {}) if queue_fingerprint else {}
    )
    failures: list[dict[str, Any]] = []
    unexpected_targets = target_keys - planned_target_keys
    decided_target_keys = {
        key for key in source_target_keys if target_has_activity_decision(state, key)
    }
    current_terminal_issue_keys = {
        key
        for key in source_target_keys
        if state.get("targets", {}).get(key, {}).get("status") == "terminal_error"
    }
    terminal_issue_keys = {
        key
        for key, issue in persisted_issues.items()
        if key in source_target_keys and issue.get("terminal")
    } | current_terminal_issue_keys
    unaccounted_planned_targets = (
        planned_target_keys - target_keys - decided_target_keys - terminal_issue_keys
    )
    if unexpected_targets or unaccounted_planned_targets:
        failures.append(
            {
                "error": "prepared_session_target_mismatch",
                "current_unresolved_targets": len(target_keys),
                "prepared_targets": len(planned_target_keys),
                "unexpected_targets": sorted(unexpected_targets),
                "unaccounted_planned_targets": sorted(unaccounted_planned_targets),
            }
        )
    resolved_targets = len(decided_target_keys)
    completion_ratio = (resolved_targets / len(source_target_keys)) if source_target_keys else 1.0
    ranked_rows = ready_final_rows(
        source_rows=source_rows,
        state=state,
        all_target_keys=source_target_keys,
        planned_target_keys=source_target_keys,
    )
    source_row_count = final_source_row_count(source_rows, source_target_keys, source_target_keys)
    ready_row_ratio = (len(ranked_rows) / source_row_count) if source_row_count else 1.0
    final_bridge_eligible = not failures and bool(ranked_rows)
    final_bridge: dict[str, Any] = {
        "attempted": False,
        "eligible": final_bridge_eligible,
        "threshold": args.final_bridge_threshold,
        "resolved_targets": resolved_targets,
        "target_count": len(source_target_keys),
        "prepared_target_count": len(planned_target_keys),
        "completion_ratio": completion_ratio,
        "ready_rows": len(ranked_rows),
        "source_rows": source_row_count,
        "ready_row_ratio": ready_row_ratio,
    }
    prospects_bridge: dict[str, Any] = {"attempted": False}
    bridged = 0
    if final_bridge_eligible:
        final_bridge["attempted"] = True
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
        final_bridge["skip_reason"] = "not_enough_resolved_activity_for_final_bridge"

    if final_bridge.get("attempted") and not failures and not args.no_prospects_bridge:
        delay_seconds = max(0, args.prospects_bridge_delay_sec)
        prospects_bridge = {"attempted": True, "delay_seconds": delay_seconds}
        if delay_seconds and not args.dry_run:
            emit_progress(date_value, "prospects_bridge_wait_start", delay_seconds=delay_seconds)
            time.sleep(delay_seconds)
        try:
            emit_progress(date_value, "prospects_bridge_start")
            prospects_bridge["result"] = bridge_final_to_prospects(args, date_value)
            if not prospects_bridge["result"].get("ok", False):
                failures.append(
                    {"error": "prospects_bridge_failed", "detail": prospects_bridge["result"]}
                )
            emit_progress(date_value, "prospects_bridge_done", result=prospects_bridge["result"])
        except Exception as exc:
            failures.append({"error": "prospects_bridge_failed", "detail": str(exc)})
            prospects_bridge["error"] = str(exc)

    retryable_unresolved_keys = source_target_keys - decided_target_keys - terminal_issue_keys
    unresolved_count = len(retryable_unresolved_keys)
    if retryable_unresolved_keys:
        failures.append(
            {
                "error": "unresolved_activity_targets",
                "unresolved_targets": unresolved_count,
                "unresolved_keys": sorted(retryable_unresolved_keys),
                "resolved_targets": resolved_targets,
                "source_targets": len(source_target_keys),
                "prepared_targets": len(planned_target_keys),
                "retryable": True,
            }
        )

    structural_failures = [
        item for item in failures if item.get("error") != "unresolved_activity_targets"
    ]
    status = (
        "blocked"
        if structural_failures
        else (
            "partial"
            if retryable_unresolved_keys
            else ("completed_with_exceptions" if terminal_issue_keys else "completed")
        )
    )
    result = {
        "ok": not failures,
        "status": status,
        "finalize_only": True,
        "dry_run": bool(args.dry_run),
        "date": date_value,
        "queue_fingerprint": queue_fingerprint,
        "targets": len(source_target_keys),
        "prepared_targets": len(planned_target_keys),
        "completed_this_run": 0,
        "bridged_rows": bridged,
        "final_bridge": final_bridge,
        "prospects_bridge": prospects_bridge,
        "failures": failures,
        "terminal_issues": [
            state.get("targets", {}).get(key, {}) for key in sorted(terminal_issue_keys)
        ],
        "session_path": str(activity_session_path(date_value)),
        "journal_path": str(activity_journal_path(date_value)),
    }
    if not args.dry_run:
        if structural_failures:
            queue_status = "activity_blocked"
        elif retryable_unresolved_keys:
            queue_status = "activity_in_progress"
        elif terminal_issue_keys:
            queue_status = "activity_needs_attention"
        else:
            queue_status = (
                "prospects_bridged"
                if prospects_bridge.get("result", {}).get("ok", False)
                else "final_bridged"
            )
        update_batch_status(
            queue_fingerprint,
            queue_status,
            activity={
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "completion_ratio": completion_ratio,
                "ready_row_ratio": ready_row_ratio,
                "session_path": str(activity_session_path(date_value)),
            },
            activity_issues={
                key: (
                    {
                        "terminal": True,
                        "terminal_reason": state.get("targets", {})
                        .get(key, {})
                        .get("terminal_reason", ""),
                        "profile_url": state.get("targets", {}).get(key, {}).get("profile_url", ""),
                        "recorded_at": state.get("targets", {}).get(key, {}).get("recorded_at", ""),
                        "attempts": state.get("targets", {}).get(key, {}).get("attempt", ""),
                        "max_attempts": state.get("targets", {})
                        .get(key, {})
                        .get("max_attempts", ""),
                    }
                    if key in current_terminal_issue_keys
                    else persisted_issues.get(key, {})
                )
                for key in sorted(terminal_issue_keys)
            },
            final_bridge=final_bridge,
            prospects_bridge=prospects_bridge,
        )
        state.update(
            {
                "status": status,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "bridged_rows_count": bridged,
                "final_bridge_run": final_bridge,
                "prospects_bridge": prospects_bridge,
                "failures": failures,
            }
        )
        save_session_state(date_value, state)
    emit_progress(date_value, "final", **result)
    return result


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
