"""Connection-acceptance monitoring: the subtractive sent-invitations strategy.

Reads the sent-invitations page, infers acceptances from who's missing,
matches them against the pending queue, syncs Outreach Log and Pipeline, and
prepares first-message drafts. Cadence is jittered; duplicates are pruned
after 14 days.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from outreach_helper import (
    append_pipeline_row_for_acceptance,
    build_template_variables,
    load_pending_outreach_log_connections,
    load_templates,
    mark_outreach_log_connected,
    render_template,
)

from outbound.outreach.paths import ACCEPTANCE_STATE_FILE, OBF_SHEET_URL
from outbound.shared.dates import sheet_date
from outbound.shared.sheetutils import (
    _normalize_person_name,
    _normalize_profile_url,
    _parse_iso_datetime,
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


def acceptance_check(args: argparse.Namespace) -> dict[str, Any]:
    from linkedin_outreach_session import (
        _make_acceptance_summary_message,
        _read_json,
        _verify_sheet_url_identity,
    )

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
