"""LinkedIn activity classification and Prospects sync.

Classifies profile activity into the Prospects dropdown taxonomy, syncs it
sheet-write-ahead through the journal, and preserves manual entries: never
overwrite a human-entered value, never write when the blank-check fails.
"""

from __future__ import annotations

import random
import time
from datetime import date
from typing import Any

from outreach_helper import (
    apply_prospect_fields,
    build_connection_sent_fields,
    build_prospect_activity_fields,
    mark_connected,
)
from sheets_helper import get_client, get_worksheet, open_sheet

from outbound.outreach.journal import _record_journal_event, _sheet_target_payload
from outbound.outreach.policy import ACTIVITY_READ_TIMEOUT_SEC, OUTREACH_STATUS_REQUIRES_EMAIL


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
