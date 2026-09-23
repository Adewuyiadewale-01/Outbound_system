"""Pure, seeded plan generators for the outreach workflow.

Every planner is deterministic: same date + same inputs produce the same plan,
which is what makes runs replayable and the sequence sheet auditable.
"""

from __future__ import annotations

import json
import random
from typing import Any

from outbound.outreach.journal import sequence_path
from outbound.outreach.paths import STATE_DIR
from outbound.shared.dates import sequence_date_key, sheet_date
from outbound.shared.sheetutils import _clamped_gauss, _parse_int


def _random_positive_partition(total: int, parts: int, rng: random.Random) -> list[int]:
    if parts <= 0:
        return []
    if total < parts:
        raise ValueError(f"total ({total}) must be >= enabled batches ({parts})")
    if parts == 1:
        return [total]

    out: list[int] = []
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, total), parts - 1))
        out = []
        prev = 0
        for cut in cuts:
            out.append(cut - prev)
            prev = cut
        out.append(total - prev)
        # Prefer uneven split for natural cadence.
        if len(set(out)) > 1:
            return out
    return out


def generate_batch_size_plan(
    date_value: str, total_slots: int, batch_numbers: list[int]
) -> dict[str, Any]:
    if total_slots < 0:
        raise ValueError("total_slots must be >= 0")

    ordered_batches = sorted(int(x) for x in batch_numbers)
    if not ordered_batches:
        raise ValueError("No enabled batches found")

    if total_slots == 0:
        return {
            "date": sheet_date(date_value),
            "date_key": sequence_date_key(date_value),
            "total_slots": 0,
            "batch_count": len(ordered_batches),
            "sizes": [{"batch_number": b, "size": 0} for b in ordered_batches],
        }

    rng = random.Random(
        f"linkedin-outreach-batch-size:{sequence_date_key(date_value)}:{total_slots}:{','.join(map(str, ordered_batches))}"
    )
    chunks = _random_positive_partition(total_slots, len(ordered_batches), rng)
    return {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "total_slots": total_slots,
        "batch_count": len(ordered_batches),
        "sizes": [
            {"batch_number": batch, "size": size} for batch, size in zip(ordered_batches, chunks)
        ],
    }


def generate_activity_timing_plan(date_value: str, slot_ids: list[int]) -> dict[str, Any]:
    ordered_slots = sorted(int(x) for x in slot_ids)
    options = [
        ("before_conn", 58),
        ("after_conn", 42),
    ]
    values = [item[0] for item in options]
    weights = [item[1] for item in options]
    rng = random.Random(
        f"linkedin-outreach-activity-timing:{sequence_date_key(date_value)}:{','.join(map(str, ordered_slots))}"
    )

    entries = []
    for slot_id in ordered_slots:
        timing = rng.choices(values, weights=weights, k=1)[0]
        entries.append({"slot_id": slot_id, "activity_log_timing": timing})

    return {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "slot_count": len(ordered_slots),
        "entries": entries,
    }


def generate_delay_seconds_plan(
    date_value: str,
    slot_ids: list[int],
    min_sec: int = 10,
    max_sec: int = 95,
) -> dict[str, Any]:
    if min_sec < 1 or max_sec < 1 or min_sec > max_sec:
        raise ValueError("Invalid delay bounds: require 1 <= min_sec <= max_sec")

    ordered_slots = sorted(int(x) for x in slot_ids)
    rng = random.Random(
        f"linkedin-outreach-delay-sec:{sequence_date_key(date_value)}:{min_sec}:{max_sec}:{','.join(map(str, ordered_slots))}"
    )

    entries = []
    for slot_id in ordered_slots:
        delay = rng.randint(min_sec, max_sec)
        entries.append({"slot_id": slot_id, "delay_sec": delay})

    return {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "slot_count": len(ordered_slots),
        "min_sec": min_sec,
        "max_sec": max_sec,
        "entries": entries,
    }


def generate_sequence(date_value: str, target: int, write: bool = True) -> dict[str, Any]:
    """Generate a stable per-date outreach behavior sequence."""
    if target < 0:
        raise ValueError("target must be >= 0")

    rng = random.Random(f"linkedin-outreach:{sequence_date_key(date_value)}:{target}")
    remaining = target
    batches: list[dict[str, Any]] = []
    lead_plan: list[dict[str, Any]] = []
    lead_index = 1
    diversion_choices = ["feed_scroll", "engagement_trail", "reaction_scan", "post_like"]

    while remaining > 0:
        batch_size = min(remaining, rng.choice([3, 4, 5, 6]))
        batch_index = len(batches) + 1
        batch_leads = []
        for _ in range(batch_size):
            report_timing = rng.choices(["instant", "post_conn"], weights=[2, 3], k=1)[0]
            item = {
                "lead_index": lead_index,
                "batch_index": batch_index,
                "activity_report_timing": report_timing,
                "delay_after_seconds": rng.randint(15, 45),
            }
            batch_leads.append(lead_index)
            lead_plan.append(item)
            lead_index += 1

        remaining -= batch_size
        diversion = None
        if remaining > 0:
            diversion = {
                "type": rng.choices(diversion_choices, weights=[3, 2, 2, 2], k=1)[0],
                "duration_seconds": _clamped_gauss(rng, 210, 40, 120, 300),
            }
        batches.append(
            {
                "batch_index": batch_index,
                "size": batch_size,
                "lead_indexes": batch_leads,
                "after_batch_diversion": diversion,
            }
        )

    sequence = {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "target": target,
        "warm_up_seconds": rng.randint(120, 300),
        "cool_down_seconds": rng.randint(45, 180),
        "batches": batches,
        "lead_plan": lead_plan,
    }

    if write:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sequence_path(date_value).write_text(json.dumps(sequence, indent=2), encoding="utf-8")

    return sequence


def load_or_generate_sequence(
    date_value: str, target: int, regenerate: bool = False
) -> dict[str, Any]:
    path = sequence_path(date_value)
    if path.exists() and not regenerate:
        return json.loads(path.read_text(encoding="utf-8"))
    return generate_sequence(date_value, target, write=True)


def _normalize_activity_timing(value: Any) -> str:
    raw = str(value).strip().lower()
    if raw in {"before_conn", "before", "instant"}:
        return "before_conn"
    if raw in {"after_conn", "after", "post_conn", "post"}:
        return "after_conn"
    return "after_conn"


def _runtime_plan_from_generated_sequence(sequence: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lead_plan = sequence.get("lead_plan", [])
    for idx, item in enumerate(lead_plan, start=1):
        timing = _normalize_activity_timing(item.get("activity_report_timing"))
        delay_sec = _parse_int(item.get("delay_after_seconds")) or 0
        out.append(
            {
                "slot_id": _parse_int(item.get("lead_index")) or idx,
                "activity_log_timing": timing,
                "delay_sec": max(0, delay_sec),
                "lead_diversion": "none",
                "lead_diversion_sec": None,
            }
        )
    return out
