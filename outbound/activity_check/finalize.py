"""Final upsert/sort, ready-row selection, and cross-script bridges.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S12). Pure move.
"""

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from prefinal_queue import QUEUE_ROW_COLUMNS, load_batch, update_batch_status

from outbound.activity_check.analysis import final_sort_key, rank_row_for_final
from outbound.activity_check.config import FINAL_REQUIRED_COLUMNS
from outbound.activity_check.state import (
    activity_journal_path,
    activity_session_path,
    emit_progress,
    journal_event,
    save_session_state,
)
from outbound.activity_check.targets import extract_targets
from outbound.activity_check.text import clean_text, normalize_activity_value, target_key
from outbound.shared.sheets import get_client, normalize_rows, open_sheet, require_columns


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
    from outbound.leads.bridge import bridge_prefinal_to_prospects
    from outbound.leads.runs import ensure_dirs

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
