"""Lane assignment and account ownership for the outreach queue.

Lanes are frozen at prep time — assigned, balanced, and persisted to the
Prospects sheet — never inferred at run time. Existing lanes are immutable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sheets_helper import get_client, get_worksheet, open_sheet

from outbound.outreach.paths import OUTREACH_WORKERS_CONFIG_PATH
from outbound.shared.sheetutils import _batch_update_cells, _find_first_index


def _read_outreach_workers(path_value: str | None = None) -> dict[str, dict[str, Any]]:
    """Return the configured lane -> account mapping for a frozen queue."""
    path = Path(path_value).expanduser() if path_value else OUTREACH_WORKERS_CONFIG_PATH
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("enabled"):
        return {}
    workers: dict[str, dict[str, Any]] = {}
    for raw_worker in payload.get("workers", []):
        if not raw_worker.get("enabled", True):
            continue
        worker = dict(raw_worker)
        worker_id = str(worker.get("id") or "").strip()
        lane = str(worker.get("primary_lane") or "").strip()
        if not worker_id or not lane:
            raise ValueError("Each enabled outreach worker needs both id and primary_lane.")
        if lane in workers:
            raise ValueError(f"More than one outreach worker is mapped to Primary Lane '{lane}'.")
        workers[lane] = worker
    if not workers:
        raise ValueError("outreach_workers.json is enabled but has no enabled workers.")
    return workers


def _assign_outreach_workers(
    queue: list[dict[str, Any]], worker_config: str | None = None
) -> dict[str, Any]:
    """Freeze account ownership in the prepared queue; never infer it at run time."""
    workers_by_lane = _read_outreach_workers(worker_config)
    if not workers_by_lane:
        return {"enabled": False, "workers": [], "counts": {}}
    counts: dict[str, int] = {}
    for prospect in queue:
        lane = str(prospect.get("primary_lane") or "").strip()
        worker = workers_by_lane.get(lane)
        if worker is None:
            raise ValueError(
                f"Prepared prospect {prospect.get('id') or prospect.get('company') or 'unknown'} "
                f"has Primary Lane '{lane or '(blank)'}', which has no enabled outreach account."
            )
        worker_id = str(worker["id"])
        prospect["worker_id"] = worker_id
        counts[worker_id] = counts.get(worker_id, 0) + 1
    return {
        "enabled": True,
        "assigned_at": datetime.now().isoformat(timespec="seconds"),
        "assignment": "primary_lane_to_account",
        "workers": [
            {"id": worker["id"], "primary_lane": lane} for lane, worker in workers_by_lane.items()
        ],
        "counts": counts,
    }


def _select_queue_for_lane_targets(
    queue: list[dict[str, Any]], lane_targets: dict[str, int]
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Keep source order while selecting an exact, explicit lane allocation."""
    normalized_targets = {
        str(lane).strip(): max(0, int(target))
        for lane, target in lane_targets.items()
        if str(lane).strip()
    }
    selected: list[dict[str, Any]] = []
    selected_counts = {lane: 0 for lane in normalized_targets}
    available_counts = {lane: 0 for lane in normalized_targets}
    for prospect in queue:
        lane = str(prospect.get("primary_lane") or "").strip()
        if lane not in normalized_targets:
            continue
        available_counts[lane] += 1
        if selected_counts[lane] < normalized_targets[lane]:
            selected.append(prospect)
            selected_counts[lane] += 1
    return selected, selected_counts, available_counts


def _auto_assign_missing_primary_lanes(
    queue: list[dict[str, Any]],
    remaining: int,
    lane_targets: dict[str, int],
    worker_config: str | None = None,
) -> dict[str, Any]:
    """Assign only eligible blank-lane prospects before a queue is frozen.

    The input comes from ``load_prospect_queue``, which already excludes used
    prospects and rows without a LinkedIn target. Existing lanes are immutable.
    With an explicit daily split we fill the largest lane deficit across the
    scanned queue. Without one, we balance only the imminent prepared batch.
    """
    workers_by_lane = _read_outreach_workers(worker_config)
    enabled_lanes = list(workers_by_lane)
    summary: dict[str, Any] = {
        "enabled": bool(enabled_lanes),
        "mode": "lane_targets" if lane_targets else "batch_balance",
        "assignments": [],
        "counts": {},
    }
    if not enabled_lanes:
        summary["skip_reason"] = "outreach_workers_not_enabled"
        return summary

    normalized_targets = {
        lane: max(0, int(target))
        for lane, target in lane_targets.items()
        if lane in workers_by_lane and int(target) > 0
    }
    scope = queue if normalized_targets else queue[: max(0, int(remaining))]
    counts = {lane: 0 for lane in enabled_lanes}
    for prospect in scope:
        lane = str(prospect.get("primary_lane") or "").strip()
        if lane in counts:
            counts[lane] += 1

    for prospect in scope:
        if str(prospect.get("primary_lane") or "").strip():
            continue
        if normalized_targets:
            deficits = {
                lane: normalized_targets[lane] - counts[lane] for lane in normalized_targets
            }
            candidates = [lane for lane in enabled_lanes if deficits.get(lane, 0) > 0]
            if not candidates:
                break
            chosen_lane = max(
                candidates, key=lambda lane: (deficits[lane], -enabled_lanes.index(lane))
            )
        else:
            chosen_lane = min(
                enabled_lanes, key=lambda lane: (counts[lane], enabled_lanes.index(lane))
            )
        prospect["primary_lane"] = chosen_lane
        counts[chosen_lane] += 1
        summary["assignments"].append(
            {
                "prospect_id": str(prospect.get("id") or ""),
                "company": str(prospect.get("company") or ""),
                "row_number": int(prospect.get("_row_number") or 0),
                "primary_lane": chosen_lane,
            }
        )

    summary["counts"] = counts
    return summary


def _persist_primary_lane_assignments(
    creds: str,
    obf_url: str,
    prospects_tab: str,
    assignments: list[dict[str, Any]],
) -> None:
    """Write prep-time lane assignments in one bounded Sheets update."""
    if not assignments:
        return
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, prospects_tab)
    headers = worksheet.row_values(1)
    lane_column = _find_first_index(headers, "Primary Lane")
    if lane_column is None:
        raise ValueError(f"{prospects_tab} is missing the required Primary Lane column.")
    updates = [
        (int(assignment["row_number"]), lane_column + 1, assignment["primary_lane"])
        for assignment in assignments
        if int(assignment.get("row_number") or 0) >= 2
    ]
    if len(updates) != len(assignments):
        raise ValueError("Cannot persist a lane assignment without a valid Prospects row number.")
    _batch_update_cells(worksheet, updates)


def _balanced_lane_targets(volume: int) -> dict[str, int]:
    """Split a prep volume as evenly as possible across the two offers."""
    volume = max(0, int(volume))
    if not volume:
        return {}
    automation = volume // 2
    return {"Design": volume - automation, "Automation": automation}
