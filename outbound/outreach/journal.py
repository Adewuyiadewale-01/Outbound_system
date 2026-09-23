"""Append-only outreach journal: the write-ahead log for sheet syncs.

Every sheet write is journal-first (recorded -> synced | pending_sync), making
interruptions recoverable and progress reconciliation deterministic.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.outreach.paths import JOURNAL_DIR, STATE_DIR
from outbound.shared.dates import sequence_date_key, sheet_date
from outbound.shared.sheetutils import _normalize_profile_url


def sequence_path(date_value: str) -> Path:
    return STATE_DIR / f"{sequence_date_key(date_value)}.json"


def prepared_session_path(date_value: str) -> Path:
    return STATE_DIR / f"{sequence_date_key(date_value)}-prepared.json"


def journal_path(date_value: str) -> Path:
    return JOURNAL_DIR / f"{sequence_date_key(date_value)}.jsonl"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return str(path)


def _journal_event(
    date_value: str,
    event_type: str,
    status: str,
    prospect: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    payload: dict[str, Any] = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "date": sheet_date(date_value),
        "event_type": event_type,
        "status": status,
    }
    if prospect is not None:
        payload.update(
            {
                "prospect_id": str(prospect.get("id", "")).strip(),
                "prospect_row": prospect.get("_row_number"),
                "company": str(prospect.get("company", "")).strip(),
                "contact_name": str(prospect.get("contact_name", "")).strip(),
                "engaged_person": str(prospect.get("engaged_person", "Person 1")).strip()
                or "Person 1",
                "contact_linkedin": str(prospect.get("contact_linkedin", "")).strip(),
            }
        )
    payload.update(extra)
    return _append_jsonl(journal_path(date_value), payload)


def _record_journal_event(
    result: dict[str, Any],
    date_value: str,
    event_type: str,
    status: str,
    prospect: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    path = _journal_event(date_value, event_type, status, prospect=prospect, **extra)
    result["journal"]["events_written"] += 1
    if status == "pending_sync":
        result["journal"]["pending_sync"] += 1
    return path


def _record_breadcrumb(
    result: dict[str, Any],
    date_value: str,
    prospect: dict[str, Any],
    stage: str,
    lead_started_at: float,
    status: str = "ok",
    **extra: Any,
) -> str:
    return _record_journal_event(
        result,
        date_value,
        "lead_breadcrumb",
        status,
        prospect=prospect,
        stage=stage,
        stage_recorded_at=datetime.now().isoformat(timespec="seconds"),
        elapsed_sec=round(time.time() - lead_started_at, 1),
        **extra,
    )


def _emit_outreach_progress(
    result: dict[str, Any],
    *,
    date_value: str,
    stage: str,
    processed: int,
    selected_count: int,
    prospect: dict[str, Any] | None = None,
) -> None:
    """Emit machine-readable progress without contaminating final stdout JSON."""
    try:
        journal = result.get("journal", {}) if isinstance(result.get("journal"), dict) else {}
        runtime = result.get("runtime_enforcement", {})
        payload: dict[str, Any] = {
            "event": "outreach_progress",
            "emitted_at": datetime.now().isoformat(timespec="seconds"),
            "date": sheet_date(date_value),
            "stage": stage,
            "processed": processed,
            "selected_count": selected_count,
            "successful_sends": int(result.get("successful_sends", 0) or 0),
            "skipped": len(result.get("skipped", [])),
            "reconciled": len(result.get("reconciled", [])),
            "blocked": bool(result.get("blockers")),
            "blockers": result.get("blockers", []),
            "pending_sync": int(journal.get("pending_sync", 0) or 0),
            "journal_path": journal.get("path", ""),
            "diversions_executed": int(runtime.get("diversions_executed", 0) or 0)
            if isinstance(runtime, dict)
            else 0,
            "engagement_opportunities": len(result.get("engagement_opportunities", [])),
        }
        if prospect is not None:
            payload["prospect_id"] = prospect.get("id")
            payload["prospect_row"] = prospect.get("_row_number")
        print(json.dumps(payload, ensure_ascii=True), file=sys.stderr, flush=True)
    except Exception:
        return


def _prospect_identity(prospect: dict[str, Any]) -> dict[str, str]:
    return {
        "prospect_id": str(prospect.get("id", "")).strip(),
        "company": str(prospect.get("company", "")).strip(),
        "person_engaged": str(prospect.get("engaged_person", "Person 1")).strip() or "Person 1",
        "contact_name": str(prospect.get("contact_name", "")).strip(),
    }


def _journal_event_matches_prospect(event: dict[str, Any], prospect: dict[str, Any]) -> bool:
    prospect_id = str(prospect.get("id", "")).strip()
    event_id = str(event.get("prospect_id", "")).strip()
    if prospect_id and event_id and prospect_id == event_id:
        return True
    prospect_url = _normalize_profile_url(prospect.get("contact_linkedin", ""))
    event_url = _normalize_profile_url(event.get("contact_linkedin", ""))
    return bool(prospect_url and event_url and prospect_url == event_url)


def _read_journal_events_for_prospect(
    date_value: str, prospect: dict[str, Any]
) -> list[dict[str, Any]]:
    path = journal_path(date_value)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _journal_event_matches_prospect(event, prospect):
                    events.append(event)
    except OSError:
        return []
    return events


def _read_journal_events(date_value: str) -> list[dict[str, Any]]:
    path = journal_path(date_value)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


def _confirmed_send_key(event: dict[str, Any]) -> str:
    prospect_id = str(event.get("prospect_id", "")).strip()
    if prospect_id:
        return f"id:{prospect_id}"
    profile_url = _normalize_profile_url(event.get("contact_linkedin", ""))
    if profile_url:
        return f"url:{profile_url}"
    company = str(event.get("company", "")).strip().lower()
    contact = str(event.get("contact_name", "")).strip().lower()
    return f"name:{company}|{contact}"


def _journal_has_confirmed_send(date_value: str, prospect: dict[str, Any]) -> bool:
    return any(
        event.get("event_type") == "connection_request_confirmed"
        and event.get("status") == "recorded"
        for event in _read_journal_events_for_prospect(date_value, prospect)
    )


def _journal_confirmed_send_records(date_value: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for event in _read_journal_events(date_value):
        if (
            event.get("event_type") != "connection_request_confirmed"
            or event.get("status") != "recorded"
        ):
            continue
        key = _confirmed_send_key(event)
        if key:
            records[key] = event
    return list(records.values())


def _journal_pending_sync_count(date_value: str) -> int:
    pending: set[tuple[str, str]] = set()
    synced: set[tuple[str, str]] = set()
    for event in _read_journal_events(date_value):
        event_type = str(event.get("event_type", "")).strip()
        if not event_type.endswith("_sync") and event_type != "prospect_reconciliation":
            continue
        key = (_confirmed_send_key(event), event_type)
        if event.get("status") == "pending_sync":
            pending.add(key)
        elif event.get("status") == "synced":
            synced.add(key)
    return len(pending - synced)


def _reconcile_result_from_journal(
    result: dict[str, Any],
    date_value: str,
    starting_progress: int | None = None,
    target: int | None = None,
) -> None:
    confirmed = _journal_confirmed_send_records(date_value)
    journal_count = len(confirmed)
    previous_count = int(result.get("successful_sends", 0) or 0)
    result["journal"]["confirmed_sends"] = journal_count
    result["journal"]["pending_sync"] = _journal_pending_sync_count(date_value)
    result["journal_reconciliation"] = {
        "confirmed_sends": journal_count,
        "previous_successful_sends": previous_count,
        "source": "journal_connection_request_confirmed",
    }
    if journal_count > previous_count:
        result["successful_sends"] = journal_count
        result["journal_reconciliation"]["successful_sends_adjusted"] = True
    if starting_progress is not None:
        reconciled_progress = max(int(starting_progress or 0), journal_count)
        if target is not None and target > 0:
            reconciled_progress = min(reconciled_progress, int(target))
        result.setdefault("outreach_control", {})["reconciled_progress"] = reconciled_progress


def _pending_sync_payload_for_confirmed_send(
    date_value: str,
    prospect: dict[str, Any],
    sync_event_type: str,
) -> dict[str, Any] | None:
    events = _read_journal_events_for_prospect(date_value, prospect)
    has_confirmed_send = any(
        event.get("event_type") == "connection_request_confirmed"
        and event.get("status") == "recorded"
        for event in events
    )
    if not has_confirmed_send:
        return None
    for event in reversed(events):
        if event.get("event_type") != sync_event_type:
            continue
        if event.get("status") == "synced":
            return None
        if event.get("status") == "pending_sync":
            payload = event.get("sheet_payload")
            return payload if isinstance(payload, dict) else None
    return None


def _sheet_target_payload(
    target: str,
    fields: dict[str, Any],
    row_number: int | None = None,
    insert_mode: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target": target,
        "fields": fields,
    }
    if row_number is not None:
        payload["row_number"] = row_number
    if insert_mode:
        payload["insert_mode"] = insert_mode
    return payload
