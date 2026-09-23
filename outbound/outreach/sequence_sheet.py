"""Sheet-bound executors for the Outreach Sequence tab.

Each executor reads the Sequence sheet, delegates to a pure planner, and
writes assignments back — the sheet-first source of truth for runtime plans.
"""

from __future__ import annotations

from typing import Any

from sheets_helper import get_client, get_worksheet, open_sheet

from outbound.outreach.planning import (
    _normalize_activity_timing,
    generate_activity_timing_plan,
    generate_batch_size_plan,
    generate_delay_seconds_plan,
)
from outbound.outreach.policy import OUTREACH_SEQUENCE_TAB
from outbound.shared.diversion import (
    generate_lead_diversion_plan,
    generate_lead_diversion_seconds_plan,
)
from outbound.shared.sheetutils import (
    _batch_update_cells,
    _find_first_index,
    _find_last_index,
    _is_enabled,
    _parse_int,
)


def _resolve_sequence_columns(headers: list[str]) -> dict[str, int]:
    slot_col = _find_first_index(headers, "Slot ID")
    batch_size_col = _find_first_index(headers, "Batch Size")
    if slot_col is None or batch_size_col is None:
        raise ValueError("Could not find required headers: Slot ID and Batch Size")

    lead_batch_col = _find_first_index(headers, "Batch #", start=slot_col + 1, end=batch_size_col)
    lead_enabled_col = _find_first_index(headers, "Enabled", start=slot_col + 1, end=batch_size_col)
    batch_batch_col = _find_last_index(
        headers, "Batch #", start=batch_size_col - 1, end=batch_size_col + 1
    )
    batch_enabled_col = _find_first_index(headers, "Enabled", start=batch_size_col + 1)

    if lead_batch_col is None or lead_enabled_col is None:
        raise ValueError("Could not resolve lead table columns: Batch # and Enabled")
    if batch_batch_col is None or batch_enabled_col is None:
        raise ValueError("Could not resolve batch table columns: Batch # and Enabled")

    return {
        "slot_col": slot_col,
        "lead_batch_col": lead_batch_col,
        "lead_enabled_col": lead_enabled_col,
        "batch_batch_col": batch_batch_col,
        "batch_size_col": batch_size_col,
        "batch_enabled_col": batch_enabled_col,
    }


def generate_batch_sizes_from_sheet(
    creds: str,
    obf_url: str,
    date_value: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
    write: bool = True,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)

    lead_rows: list[dict[str, Any]] = []
    batch_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is not None and slot_id > 0:
            lead_rows.append(
                {
                    "row_number": idx,
                    "slot_id": slot_id,
                    "enabled": _is_enabled(padded[cols["lead_enabled_col"]], default=True),
                }
            )

        batch_num = _parse_int(padded[cols["batch_batch_col"]])
        if batch_num is not None and batch_num > 0:
            batch_rows.append(
                {
                    "row_number": idx,
                    "batch_number": batch_num,
                    "enabled": _is_enabled(padded[cols["batch_enabled_col"]], default=True),
                }
            )

    lead_rows.sort(key=lambda x: x["slot_id"])
    batch_rows.sort(key=lambda x: x["batch_number"])

    enabled_leads = [r for r in lead_rows if r["enabled"]]
    enabled_batches = [r for r in batch_rows if r["enabled"]]
    plan = generate_batch_size_plan(
        date_value=date_value,
        total_slots=len(enabled_leads),
        batch_numbers=[r["batch_number"] for r in enabled_batches],
    )
    size_by_batch = {item["batch_number"]: item["size"] for item in plan["sizes"]}

    assignments: list[tuple[int, str]] = []
    remaining_rows = enabled_leads[:]
    for batch_row in enabled_batches:
        batch_number = batch_row["batch_number"]
        take = size_by_batch[batch_number]
        current_rows = remaining_rows[:take]
        remaining_rows = remaining_rows[take:]
        for lead in current_rows:
            assignments.append((lead["row_number"], str(batch_number)))

    for lead in lead_rows:
        if not lead["enabled"]:
            assignments.append((lead["row_number"], ""))

    if write:
        cell_updates: list[tuple[int, int, Any]] = []
        for batch_row in batch_rows:
            value = size_by_batch.get(batch_row["batch_number"], "")
            cell_updates.append((batch_row["row_number"], cols["batch_size_col"] + 1, value))
        for row_number, value in assignments:
            cell_updates.append((row_number, cols["lead_batch_col"] + 1, value))
        _batch_update_cells(worksheet, cell_updates)

    return {
        "ok": True,
        "date": plan["date"],
        "sequence_tab": sequence_tab,
        "write": write,
        "enabled_slots": len(enabled_leads),
        "enabled_batches": len(enabled_batches),
        "sizes": plan["sizes"],
        "assigned_leads": len([x for x in assignments if x[1] != ""]),
    }


def generate_lead_diversions_from_sheet(
    creds: str,
    obf_url: str,
    date_value: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
    write: bool = True,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)
    lead_diversion_col = _find_first_index(
        headers,
        "Lead Diversion",
        start=cols["slot_col"] + 1,
        end=cols["batch_size_col"],
    )
    if lead_diversion_col is None:
        raise ValueError("Could not resolve lead table column: Lead Diversion")

    lead_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is None or slot_id <= 0:
            continue
        lead_rows.append(
            {
                "row_number": idx,
                "slot_id": slot_id,
                "enabled": _is_enabled(padded[cols["lead_enabled_col"]], default=True),
            }
        )

    lead_rows.sort(key=lambda x: x["slot_id"])
    enabled_rows = [r for r in lead_rows if r["enabled"]]
    plan = generate_lead_diversion_plan(
        date_value=date_value,
        slot_ids=[r["slot_id"] for r in enabled_rows],
    )
    diversion_by_slot = {item["slot_id"]: item["lead_diversion"] for item in plan["entries"]}

    updates: list[tuple[int, str]] = []
    for row in lead_rows:
        if row["enabled"]:
            updates.append((row["row_number"], diversion_by_slot[row["slot_id"]]))
        else:
            updates.append((row["row_number"], ""))

    if write:
        _batch_update_cells(
            worksheet,
            [(row_number, lead_diversion_col + 1, value) for row_number, value in updates],
        )

    distribution: dict[str, int] = {}
    for _, value in updates:
        if not value:
            continue
        distribution[value] = distribution.get(value, 0) + 1

    return {
        "ok": True,
        "date": plan["date"],
        "sequence_tab": sequence_tab,
        "write": write,
        "enabled_slots": len(enabled_rows),
        "distribution": distribution,
        "preview": plan["entries"][:10],
    }


def generate_activity_timing_from_sheet(
    creds: str,
    obf_url: str,
    date_value: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
    write: bool = True,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)
    timing_col = _find_first_index(
        headers,
        "Activity Log Timing",
        start=cols["slot_col"] + 1,
        end=cols["batch_size_col"],
    )
    if timing_col is None:
        raise ValueError("Could not resolve lead table column: Activity Log Timing")

    lead_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is None or slot_id <= 0:
            continue
        lead_rows.append(
            {
                "row_number": idx,
                "slot_id": slot_id,
                "enabled": _is_enabled(padded[cols["lead_enabled_col"]], default=True),
            }
        )

    lead_rows.sort(key=lambda x: x["slot_id"])
    enabled_rows = [r for r in lead_rows if r["enabled"]]
    plan = generate_activity_timing_plan(
        date_value=date_value,
        slot_ids=[r["slot_id"] for r in enabled_rows],
    )
    timing_by_slot = {item["slot_id"]: item["activity_log_timing"] for item in plan["entries"]}

    updates: list[tuple[int, str]] = []
    for row in lead_rows:
        if row["enabled"]:
            updates.append((row["row_number"], timing_by_slot[row["slot_id"]]))
        else:
            updates.append((row["row_number"], ""))

    if write:
        _batch_update_cells(
            worksheet,
            [(row_number, timing_col + 1, value) for row_number, value in updates],
        )

    distribution: dict[str, int] = {}
    for _, value in updates:
        if not value:
            continue
        distribution[value] = distribution.get(value, 0) + 1

    return {
        "ok": True,
        "date": plan["date"],
        "sequence_tab": sequence_tab,
        "write": write,
        "enabled_slots": len(enabled_rows),
        "distribution": distribution,
        "preview": plan["entries"][:10],
    }


def generate_delay_seconds_from_sheet(
    creds: str,
    obf_url: str,
    date_value: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
    min_sec: int = 10,
    max_sec: int = 95,
    write: bool = True,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)
    delay_col = _find_first_index(
        headers,
        "Delay Sec",
        start=cols["slot_col"] + 1,
        end=cols["batch_size_col"],
    )
    if delay_col is None:
        raise ValueError("Could not resolve lead table column: Delay Sec")

    lead_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is None or slot_id <= 0:
            continue
        lead_rows.append(
            {
                "row_number": idx,
                "slot_id": slot_id,
                "enabled": _is_enabled(padded[cols["lead_enabled_col"]], default=True),
            }
        )

    lead_rows.sort(key=lambda x: x["slot_id"])
    enabled_rows = [r for r in lead_rows if r["enabled"]]
    plan = generate_delay_seconds_plan(
        date_value=date_value,
        slot_ids=[r["slot_id"] for r in enabled_rows],
        min_sec=min_sec,
        max_sec=max_sec,
    )
    delay_by_slot = {item["slot_id"]: item["delay_sec"] for item in plan["entries"]}

    updates: list[tuple[int, str]] = []
    for row in lead_rows:
        if row["enabled"]:
            updates.append((row["row_number"], str(delay_by_slot[row["slot_id"]])))
        else:
            updates.append((row["row_number"], ""))

    if write:
        _batch_update_cells(
            worksheet,
            [(row_number, delay_col + 1, value) for row_number, value in updates],
        )

    delay_values = [int(value) for _, value in updates if value.strip()]
    return {
        "ok": True,
        "date": plan["date"],
        "sequence_tab": sequence_tab,
        "write": write,
        "enabled_slots": len(enabled_rows),
        "bounds": {"min_sec": min_sec, "max_sec": max_sec},
        "actual": {
            "min": min(delay_values) if delay_values else None,
            "max": max(delay_values) if delay_values else None,
            "avg": (sum(delay_values) / len(delay_values)) if delay_values else None,
        },
        "preview": plan["entries"][:10],
    }


def generate_lead_diversion_seconds_from_sheet(
    creds: str,
    obf_url: str,
    date_value: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
    write: bool = True,
) -> dict[str, Any]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)
    lead_diversion_col = _find_first_index(
        headers,
        "Lead Diversion",
        start=cols["slot_col"] + 1,
        end=cols["batch_size_col"],
    )
    lead_diversion_sec_col = _find_first_index(
        headers,
        "Lead Diversion Sec",
        start=cols["slot_col"] + 1,
        end=cols["batch_size_col"],
    )
    if lead_diversion_col is None:
        raise ValueError("Could not resolve lead table column: Lead Diversion")
    if lead_diversion_sec_col is None:
        raise ValueError("Could not resolve lead table column: Lead Diversion Sec")

    lead_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is None or slot_id <= 0:
            continue
        lead_rows.append(
            {
                "row_number": idx,
                "slot_id": slot_id,
                "enabled": _is_enabled(padded[cols["lead_enabled_col"]], default=True),
                "lead_diversion": str(padded[lead_diversion_col]).strip().lower(),
            }
        )

    lead_rows.sort(key=lambda x: x["slot_id"])
    enabled_rows = [r for r in lead_rows if r["enabled"]]
    plan = generate_lead_diversion_seconds_plan(
        date_value=date_value,
        slot_diversions=[
            {"slot_id": row["slot_id"], "lead_diversion": row["lead_diversion"]}
            for row in enabled_rows
        ],
    )
    seconds_by_slot = {item["slot_id"]: item["lead_diversion_sec"] for item in plan["entries"]}

    updates: list[tuple[int, str]] = []
    for row in lead_rows:
        if not row["enabled"]:
            updates.append((row["row_number"], ""))
            continue
        seconds = seconds_by_slot[row["slot_id"]]
        updates.append((row["row_number"], "" if seconds is None else str(seconds)))

    if write:
        _batch_update_cells(
            worksheet,
            [(row_number, lead_diversion_sec_col + 1, value) for row_number, value in updates],
        )

    sec_values = [int(v) for _, v in updates if str(v).strip()]
    return {
        "ok": True,
        "date": plan["date"],
        "sequence_tab": sequence_tab,
        "write": write,
        "enabled_slots": len(enabled_rows),
        "with_diversion_count": len(sec_values),
        "actual": {
            "min": min(sec_values) if sec_values else None,
            "max": max(sec_values) if sec_values else None,
            "avg": (sum(sec_values) / len(sec_values)) if sec_values else None,
        },
        "preview": plan["entries"][:10],
    }


def _load_runtime_plan_from_sequence_sheet(
    creds: str,
    obf_url: str,
    sequence_tab: str = OUTREACH_SEQUENCE_TAB,
) -> list[dict[str, Any]]:
    client = get_client(creds)
    spreadsheet = open_sheet(client, obf_url)
    worksheet = get_worksheet(spreadsheet, sequence_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("Outreach Sequence sheet is empty")

    headers = values[0]
    cols = _resolve_sequence_columns(headers)
    timing_col = _find_first_index(
        headers, "Activity Log Timing", start=cols["slot_col"] + 1, end=cols["batch_size_col"]
    )
    delay_col = _find_first_index(
        headers, "Delay Sec", start=cols["slot_col"] + 1, end=cols["batch_size_col"]
    )
    diversion_col = _find_first_index(
        headers, "Lead Diversion", start=cols["slot_col"] + 1, end=cols["batch_size_col"]
    )
    diversion_sec_col = _find_first_index(
        headers, "Lead Diversion Sec", start=cols["slot_col"] + 1, end=cols["batch_size_col"]
    )
    if (
        timing_col is None
        or delay_col is None
        or diversion_col is None
        or diversion_sec_col is None
    ):
        raise ValueError("Outreach Sequence is missing one or more runtime columns")

    out: list[dict[str, Any]] = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        slot_id = _parse_int(padded[cols["slot_col"]])
        if slot_id is None or slot_id <= 0:
            continue
        enabled = _is_enabled(padded[cols["lead_enabled_col"]], default=True)
        if not enabled:
            continue
        delay_sec = _parse_int(padded[delay_col]) or 0
        diversion = str(padded[diversion_col]).strip().lower() or "none"
        diversion_sec = _parse_int(padded[diversion_sec_col])
        out.append(
            {
                "slot_id": slot_id,
                "activity_log_timing": _normalize_activity_timing(padded[timing_col]),
                "delay_sec": max(0, delay_sec),
                "lead_diversion": diversion,
                "lead_diversion_sec": diversion_sec
                if diversion_sec is not None and diversion_sec >= 0
                else None,
            }
        )
    out.sort(key=lambda x: x["slot_id"])
    return out
