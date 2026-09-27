"""Session and journal state for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S4). Pure move.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.activity_check.config import JOURNAL_DIR, STATE_DIR
from outbound.activity_check.text import canonical_linkedin_profile_url, clean_text
from outbound.shared.dates import sequence_date_key, sheet_date


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
