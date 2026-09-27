"""Activity-sequence planning (deterministic per-date randomness).

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S10). Pure move.
"""

import json
import random
from collections.abc import Sequence
from typing import Any

from outbound.activity_check.config import DIVERSION_OPTIONS, NAVIGATION_TYPE_OPTIONS
from outbound.activity_check.text import navigation_type_key
from outbound.outreach.planning import _random_positive_partition
from outbound.shared.dates import sequence_date_key
from outbound.shared.sheets import get_client, open_sheet
from outbound.shared.sheetutils import (
    _batch_update_cells,
    _find_first_index,
    _find_last_index,
    _is_enabled,
    _parse_int,
)


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
