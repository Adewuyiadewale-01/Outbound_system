#!/usr/bin/env python3
"""Prepare a local queue of overdue LinkedIn connection requests to withdraw.

Source of truth:
- Operation Brute Force -> Outreach Log
- Current Progress must be Conn Request / Connection Request
- Days Left must be numeric and <= 0, or calculable from Sent At

This script does not open LinkedIn and does not write withdrawal results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from outreach_helper import CREDS_PATH, OBF_SHEET_URL, OUTREACH_LOG_TAB, PROSPECTS_TAB  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

SPREADSHEET_SERIAL_EPOCH = datetime(1899, 12, 30)
DEFAULT_TIMEZONE = "Africa/Lagos"
WITHDRAWAL_WINDOW_DAYS = 14
WITHDRAWAL_SEQUENCE_TAB = "Withdrwal Sequence"
DEFAULT_CAP = 50
MAX_CAP = 50
MIN_SELECTOR_BASED_PERCENT = 65
MAX_SELECTOR_BASED_PERCENT = 90
LEAD_DELAY_RANGE_SEC = (4, 12)
INTER_BATCH_DELAY_RANGE_SEC = (45, 120)
BATCH_DIVERSION_SEC_RANGE = (20, 60)
BATCH_DIVERSIONS = [
    "engagement_trail",
    "profile_drill",
    "none",
    "feed_scroll",
    "company_page_browse",
]
WITHDRAWAL_LOG_TIMINGS = ["before_withdrawal", "after_withdrawal"]
SLOT_HEADERS = ["Slot ID", "Batch #", "Withdrawal Log Timing", "Delay Sec", "Navigation type"]
BATCH_HEADERS = [
    "Batch #",
    "Batch Size",
    "Batch Diversion",
    "Inter Batch Delay Sec",
    "Diversion Sec",
    "Enabled",
]


def normalize_progress(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"conn request", "connection request"}:
        return "conn_request"
    if text == "connected":
        return "connected"
    if text in {"withdrawn", "request withdrawn"}:
        return "withdrawn"
    return text.replace(" ", "_") if text else ""


def parse_sheet_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None and numeric >= 20000:
        try:
            return (SPREADSHEET_SERIAL_EPOCH + timedelta(days=numeric)).date()
        except OverflowError:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_days_left(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def calculate_days_left(sent_at: date, today: date) -> int:
    return WITHDRAWAL_WINDOW_DAYS - (today - sent_at).days


def normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("linkedin.com/"):
        text = "https://www." + text
    elif text.startswith("www.linkedin.com/"):
        text = "https://" + text
    return text


def canonical_linkedin_profile(value: Any) -> str:
    text = normalize_url(value)
    if not text:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(text)
        path_match = re.match(r"^/in/[^/?#]+", parsed.path, re.IGNORECASE)
        if not path_match:
            return text.rstrip("/")
        path = path_match.group(0).rstrip("/") + "/"
        return urlunparse(("https", "www.linkedin.com", path, "", "", ""))
    except Exception:
        return text.rstrip("/") + "/"


def is_valid_linkedin_profile(value: Any) -> bool:
    url = canonical_linkedin_profile(value).lower()
    if not url:
        return False
    if "linkedin.com/in/" not in url:
        return False
    if any(bad in url for bad in ("/company/", "/school/", "/jobs/", "/feed/")):
        return False
    return True


def header_index(headers: Iterable[Any]) -> dict[str, int]:
    return {str(header or "").strip(): i for i, header in enumerate(headers)}


def require_columns(index: dict[str, int], columns: Iterable[str], tab_name: str) -> None:
    missing = [column for column in columns if column not in index]
    if missing:
        raise ValueError(f"{tab_name} is missing required columns: {', '.join(missing)}")


def cell(row: list[Any], index: dict[str, int], column: str) -> str:
    position = index.get(column)
    if position is None or position >= len(row):
        return ""
    return str(row[position] or "").strip()


def read_worksheet_rows(worksheet: Any) -> tuple[list[str], list[list[Any]]]:
    values = worksheet.get_all_values()
    if not values:
        return [], []
    headers = [str(header or "").strip() for header in values[0]]
    rows = []
    for row in values[1:]:
        rows.append(row + [""] * max(0, len(headers) - len(row)))
    return headers, rows


def a1(row_number: int, col_number: int) -> str:
    letters = ""
    n = col_number
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_number}"


def batch_update_cells(worksheet: Any, updates: list[tuple[int, int, Any]]) -> None:
    if not updates:
        return
    payload = [
        {"range": a1(row_number, col_number), "values": [[value]]}
        for row_number, col_number, value in updates
    ]
    worksheet.batch_update(payload, value_input_option="USER_ENTERED")


def normalize_navigation_type(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if text in {"selector based", "selector"}:
        return "selector_based"
    if text in {"direct url", "direct"}:
        return "direct_url"
    return "selector_based"


def display_navigation_type(value: str) -> str:
    return "Direct Url" if value == "direct_url" else "Selector-based"


def normalize_withdrawal_log_timing(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"after_withdrawal", "after withdrawal", "after_diversion", "after diversion"}:
        return "after_withdrawal"
    return "before_withdrawal"


def display_withdrawal_log_timing(value: str) -> str:
    return "after_withdrawal" if value == "after_withdrawal" else "before_withdrawal"


def normalize_batch_diversion(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in BATCH_DIVERSIONS else "none"


def engaged_linkedin_column(person_engaged: str) -> str:
    value = str(person_engaged or "").strip().lower()
    if value in {"person 2", "p2", "2"}:
        return "P2 LinkedIn"
    return "P1 LinkedIn"


def build_prospect_lookup(
    rows: list[list[Any]], index: dict[str, int]
) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for row in rows:
        prospect_id = cell(row, index, "ID")
        if not prospect_id:
            continue
        lookup[prospect_id] = {
            "p1_linkedin": canonical_linkedin_profile(cell(row, index, "P1 LinkedIn")),
            "p2_linkedin": canonical_linkedin_profile(cell(row, index, "P2 LinkedIn")),
            "engaged_person": cell(row, index, "Engaged Person"),
        }
    return lookup


def resolve_contact_linkedin(
    row: list[Any],
    outreach_index: dict[str, int],
    prospect_lookup: dict[str, dict[str, str]],
) -> tuple[str, str]:
    outreach_value = canonical_linkedin_profile(cell(row, outreach_index, "Contact Linkedin"))
    if is_valid_linkedin_profile(outreach_value):
        return outreach_value, "outreach_log_contact_linkedin"

    prospect_id = cell(row, outreach_index, "Prospect ID")
    person_engaged = cell(row, outreach_index, "Person Engaged")
    prospect = prospect_lookup.get(prospect_id, {})
    column = engaged_linkedin_column(person_engaged or prospect.get("engaged_person", ""))
    key = "p2_linkedin" if column == "P2 LinkedIn" else "p1_linkedin"
    prospect_value = canonical_linkedin_profile(prospect.get(key, ""))
    if is_valid_linkedin_profile(prospect_value):
        return prospect_value, f"prospects_{column}"
    return outreach_value or prospect_value, "missing_or_invalid"


def build_available_targets(
    rows: list[list[Any]],
    index: dict[str, int],
    prospect_lookup: dict[str, dict[str, str]],
    today: date,
) -> dict[str, Any]:
    available: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    counts = {
        "scanned": len(rows),
        "conn_request_rows": 0,
        "available": 0,
        "upcoming": 0,
        "skipped_non_conn_request": 0,
        "skipped_invalid_linkedin": 0,
        "blocked_missing_sent_at": 0,
        "blocked_non_numeric_days_left": 0,
    }

    for offset, row in enumerate(rows, start=2):
        progress = cell(row, index, "Current Progress")
        progress_key = normalize_progress(progress)
        prospect_id = cell(row, index, "Prospect ID")
        company = cell(row, index, "Company")
        contact_name = cell(row, index, "Contact Name")
        linkedin, linkedin_source = resolve_contact_linkedin(row, index, prospect_lookup)

        if progress_key != "conn_request":
            counts["skipped_non_conn_request"] += 1
            continue
        counts["conn_request_rows"] += 1

        if not is_valid_linkedin_profile(linkedin):
            counts["skipped_invalid_linkedin"] += 1
            skipped.append(
                {
                    "reason": "invalid_linkedin",
                    "row_number": offset,
                    "prospect_id": prospect_id,
                    "company": company,
                    "contact_name": contact_name,
                    "contact_linkedin": linkedin,
                    "linkedin_source": linkedin_source,
                }
            )
            continue

        days_left_raw = cell(row, index, "Days Left")
        days_left = parse_days_left(days_left_raw)
        sent_at_raw = cell(row, index, "Sent At")
        sent_at = parse_sheet_date(sent_at_raw)
        days_left_source = "days_left"

        if days_left is None:
            if days_left_raw:
                counts["blocked_non_numeric_days_left"] += 1
                skipped.append(
                    {
                        "reason": "non_numeric_days_left",
                        "row_number": offset,
                        "prospect_id": prospect_id,
                        "days_left": days_left_raw,
                    }
                )
                continue
            if not sent_at:
                counts["blocked_missing_sent_at"] += 1
                skipped.append(
                    {
                        "reason": "missing_sent_at",
                        "row_number": offset,
                        "prospect_id": prospect_id,
                        "company": company,
                        "contact_name": contact_name,
                    }
                )
                continue
            days_left = calculate_days_left(sent_at, today)
            days_left_source = "sent_at_calculated"

        if days_left > 0:
            counts["upcoming"] += 1
            continue

        available.append(
            {
                "row_number": offset,
                "prospect_id": prospect_id,
                "company": company,
                "person_engaged": cell(row, index, "Person Engaged"),
                "contact_name": contact_name,
                "primary_lane": cell(row, index, "Primary Lane"),
                "contact_linkedin": linkedin,
                "contact_linkedin_source": linkedin_source,
                "current_progress": progress,
                "touch_method": cell(row, index, "Touch Method"),
                "outcome": cell(row, index, "Outcome"),
                "last_action_date": cell(row, index, "Last Action Date"),
                "sent_at": sent_at_raw,
                "days_left": days_left,
                "days_left_source": days_left_source,
                "notes": cell(row, index, "Notes"),
            }
        )

    counts["available"] = len(available)
    return {"available_targets": available, "skipped": skipped, "counts": counts}


def build_seed(run_date: date, targets: list[dict[str, Any]], user_seed: str = "") -> str:
    material = "|".join(
        [run_date.isoformat(), user_seed] + [str(t.get("prospect_id", "")) for t in targets]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def determine_batch_count(count: int, rng: random.Random) -> int:
    if count <= 0:
        return 0
    if count == 1:
        return 1
    if count < 25:
        low, high = 2, 5
    else:
        low, high = 5, 10
    high = min(high, count)
    low = max(low, math.ceil(count / 15))
    low = min(low, high)
    return rng.randint(low, high)


def distribute_batches(
    count: int, batch_count: int, rng: random.Random, max_batch_size: int = 15
) -> list[int]:
    if count <= 0 or batch_count <= 0:
        return []
    sizes = [1] * batch_count
    remaining = count - batch_count
    while remaining > 0:
        candidates = [i for i, size in enumerate(sizes) if size < max_batch_size]
        if not candidates:
            raise ValueError(
                f"Cannot distribute {count} targets into {batch_count} batches capped at {max_batch_size}."
            )
        idx = rng.choice(candidates)
        add = rng.randint(1, min(remaining, max_batch_size - sizes[idx]))
        sizes[idx] += add
        remaining -= add
    rng.shuffle(sizes)
    return sizes


def choose_navigation_types(count: int, rng: random.Random) -> list[str]:
    if count <= 0:
        return []
    selector_percent = rng.randint(MIN_SELECTOR_BASED_PERCENT, MAX_SELECTOR_BASED_PERCENT)
    selector_count = max(
        math.ceil(count * MIN_SELECTOR_BASED_PERCENT / 100), round(count * selector_percent / 100)
    )
    selector_count = min(count, selector_count)
    values = ["selector_based"] * selector_count + ["direct_url"] * (count - selector_count)
    rng.shuffle(values)
    return values


def prepare_runtime_plan(
    targets: list[dict[str, Any]],
    run_date: date,
    cap: int,
    user_seed: str = "",
) -> dict[str, Any]:
    selected = targets[:cap]
    seed = build_seed(run_date, selected, user_seed)
    rng = random.Random(seed)
    shuffled = list(selected)
    rng.shuffle(shuffled)

    batch_count = determine_batch_count(len(shuffled), rng)
    batch_sizes = distribute_batches(len(shuffled), batch_count, rng)
    navigation_types = choose_navigation_types(len(shuffled), rng)
    timing_values = list(WITHDRAWAL_LOG_TIMINGS)

    batches: list[dict[str, Any]] = []
    cursor = 0
    for batch_number, batch_size in enumerate(batch_sizes, start=1):
        batch_targets = shuffled[cursor : cursor + batch_size]
        batch_navigation = navigation_types[cursor : cursor + batch_size]
        cursor += batch_size
        diversion_type = rng.choice(BATCH_DIVERSIONS)
        diversion_target_index = (
            rng.randrange(batch_size) if batch_size and diversion_type != "none" else None
        )
        planned_targets = []
        for target_index, target in enumerate(batch_targets, start=1):
            planned = dict(target)
            planned.update(
                {
                    "batch_number": batch_number,
                    "target_index": target_index,
                    "delay_sec": rng.randint(*LEAD_DELAY_RANGE_SEC),
                    "navigation_type": batch_navigation[target_index - 1],
                    "withdrawal_log_timing": rng.choice(timing_values),
                    "activity_check": {
                        "enabled": True,
                        "basis": "activity_since_sent_at",
                        "sent_at": target.get("sent_at", ""),
                        "purpose": "retargeting_rank",
                        "blocks_withdrawal": False,
                    },
                    "batch_diversion_target": diversion_target_index == target_index - 1,
                    "batch_diversion": diversion_type
                    if diversion_target_index == target_index - 1
                    else "none",
                }
            )
            planned_targets.append(planned)

        batches.append(
            {
                "batch_number": batch_number,
                "batch_size": batch_size,
                "inter_batch_delay_sec": rng.randint(*INTER_BATCH_DELAY_RANGE_SEC)
                if batch_number < batch_count
                else 0,
                "diversion": diversion_type,
                "diversion_sec": rng.randint(*BATCH_DIVERSION_SEC_RANGE)
                if diversion_type != "none"
                else 0,
                "diversion_target_index": None
                if diversion_target_index is None
                else diversion_target_index + 1,
                "targets": planned_targets,
            }
        )

    flattened = [target for batch in batches for target in batch["targets"]]
    selector_based_count = sum(
        1 for target in flattened if target["navigation_type"] == "selector_based"
    )
    direct_url_count = sum(1 for target in flattened if target["navigation_type"] == "direct_url")

    return {
        "seed": seed,
        "cap": cap,
        "selected_count": len(flattened),
        "unselected_due_count": max(0, len(targets) - len(flattened)),
        "batch_count": len(batches),
        "navigation_mix": {
            "selector_based": selector_based_count,
            "direct_url": direct_url_count,
            "selector_based_percent": round((selector_based_count / len(flattened)) * 100, 2)
            if flattened
            else 0,
            "minimum_selector_based_percent": MIN_SELECTOR_BASED_PERCENT,
        },
        "batches": batches,
        "queue": flattened,
    }


def sequence_columns(headers: list[str]) -> dict[str, int]:
    def find_all(name: str) -> list[int]:
        return [i for i, header in enumerate(headers) if str(header or "").strip() == name]

    batch_cols = find_all("Batch #")
    if len(batch_cols) < 2:
        raise ValueError(
            "Withdrawal sequence needs two 'Batch #' columns: slot table and batch table."
        )

    cols = {
        "slot_id": headers.index("Slot ID"),
        "slot_batch": batch_cols[0],
        "withdrawal_log_timing": headers.index("Withdrawal Log Timing"),
        "slot_delay_sec": headers.index("Delay Sec"),
        "navigation_type": headers.index("Navigation type"),
        "batch_batch": batch_cols[1],
        "batch_size": headers.index("Batch Size"),
        "batch_diversion": headers.index("Batch Diversion"),
        "inter_batch_delay_sec": headers.index("Inter Batch Delay Sec"),
        "enabled": headers.index("Enabled"),
    }
    delay_cols = find_all("Delay Sec")
    diversion_candidates = find_all("Diversion Sec")
    if diversion_candidates:
        cols["diversion_sec"] = diversion_candidates[0]
    elif len(delay_cols) >= 2:
        # Existing sheet briefly used "Delay Sec" as the batch diversion seconds header.
        cols["diversion_sec"] = delay_cols[1]
    else:
        raise ValueError("Withdrawal sequence needs batch 'Diversion Sec' column.")
    return cols


def ensure_sequence_headers(worksheet: Any) -> list[str]:
    values = worksheet.get_all_values()
    headers = [str(header or "").strip() for header in values[0]] if values else []
    updates: list[tuple[int, int, Any]] = []
    for index, header in enumerate(SLOT_HEADERS, start=1):
        if len(headers) < index or headers[index - 1] != header:
            updates.append((1, index, header))
    start = 8
    for offset, header in enumerate(BATCH_HEADERS):
        col = start + offset
        if len(headers) < col or headers[col - 1] != header:
            updates.append((1, col, header))
    batch_update_cells(worksheet, updates)
    return [str(header or "").strip() for header in worksheet.row_values(1)]


def write_sequence_plan(worksheet: Any, plan: dict[str, Any]) -> None:
    headers = ensure_sequence_headers(worksheet)
    cols = sequence_columns(headers)
    updates: list[tuple[int, int, Any]] = []
    queue = plan["queue"]
    batches = plan["batches"]
    rows_to_clear = max(worksheet.row_count, len(queue) + 1, len(batches) + 1)
    clear_cols = [
        cols["slot_id"],
        cols["slot_batch"],
        cols["withdrawal_log_timing"],
        cols["slot_delay_sec"],
        cols["navigation_type"],
        cols["batch_batch"],
        cols["batch_size"],
        cols["batch_diversion"],
        cols["inter_batch_delay_sec"],
        cols["diversion_sec"],
        cols["enabled"],
    ]
    for row_number in range(2, rows_to_clear + 1):
        for col in clear_cols:
            updates.append((row_number, col + 1, ""))

    for slot_index, target in enumerate(queue, start=1):
        row_number = slot_index + 1
        updates.extend(
            [
                (row_number, cols["slot_id"] + 1, slot_index),
                (row_number, cols["slot_batch"] + 1, target["batch_number"]),
                (
                    row_number,
                    cols["withdrawal_log_timing"] + 1,
                    display_withdrawal_log_timing(target["withdrawal_log_timing"]),
                ),
                (row_number, cols["slot_delay_sec"] + 1, target["delay_sec"]),
                (
                    row_number,
                    cols["navigation_type"] + 1,
                    display_navigation_type(target["navigation_type"]),
                ),
            ]
        )

    for batch in batches:
        row_number = int(batch["batch_number"]) + 1
        updates.extend(
            [
                (row_number, cols["batch_batch"] + 1, batch["batch_number"]),
                (row_number, cols["batch_size"] + 1, batch["batch_size"]),
                (row_number, cols["batch_diversion"] + 1, batch["diversion"]),
                (row_number, cols["inter_batch_delay_sec"] + 1, batch["inter_batch_delay_sec"]),
                (row_number, cols["diversion_sec"] + 1, batch["diversion_sec"]),
                (row_number, cols["enabled"] + 1, "yes"),
            ]
        )

    batch_update_cells(worksheet, updates)


def read_sequence_plan(
    worksheet: Any, selected_targets: list[dict[str, Any]], generated_plan: dict[str, Any]
) -> dict[str, Any]:
    headers, rows = read_worksheet_rows(worksheet)
    cols = sequence_columns(headers)
    targets_by_slot = {i + 1: dict(target) for i, target in enumerate(selected_targets)}
    batch_config: dict[int, dict[str, Any]] = {}
    slot_config: dict[int, dict[str, Any]] = {}

    for row in rows:
        batch_raw = row[cols["batch_batch"]] if cols["batch_batch"] < len(row) else ""
        if str(batch_raw).strip():
            batch_number = int(float(str(batch_raw).strip()))
            enabled = (
                str(row[cols["enabled"]] if cols["enabled"] < len(row) else "").strip().lower()
            )
            if enabled in {"", "yes", "y", "true", "1"}:
                batch_config[batch_number] = {
                    "batch_number": batch_number,
                    "batch_size": int(float(row[cols["batch_size"]] or 0)),
                    "diversion": normalize_batch_diversion(row[cols["batch_diversion"]]),
                    "inter_batch_delay_sec": int(float(row[cols["inter_batch_delay_sec"]] or 0)),
                    "diversion_sec": int(float(row[cols["diversion_sec"]] or 0)),
                    "enabled": True,
                }

        slot_raw = row[cols["slot_id"]] if cols["slot_id"] < len(row) else ""
        if str(slot_raw).strip():
            slot_id = int(float(str(slot_raw).strip()))
            batch_number = int(float(row[cols["slot_batch"]] or 0))
            if batch_number in batch_config or not batch_config:
                slot_config[slot_id] = {
                    "slot_id": slot_id,
                    "batch_number": batch_number,
                    "withdrawal_log_timing": normalize_withdrawal_log_timing(
                        row[cols["withdrawal_log_timing"]]
                    ),
                    "delay_sec": int(float(row[cols["slot_delay_sec"]] or 0)),
                    "navigation_type": normalize_navigation_type(row[cols["navigation_type"]]),
                }

    batches: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for batch_number in sorted(batch_config):
        batch = batch_config[batch_number]
        slots = [
            slot
            for slot in sorted(slot_config.values(), key=lambda x: x["slot_id"])
            if slot["batch_number"] == batch_number
        ]
        planned_targets = []
        diversion_target_slot = (
            generated_plan["batches"][batch_number - 1].get("diversion_target_index")
            if batch_number - 1 < len(generated_plan["batches"])
            else None
        )
        if diversion_target_slot and diversion_target_slot > len(slots):
            diversion_target_slot = len(slots) if slots else None
        for local_index, slot in enumerate(slots, start=1):
            target = targets_by_slot.get(slot["slot_id"])
            if not target:
                continue
            planned = dict(target)
            planned.update(
                {
                    "batch_number": batch_number,
                    "target_index": local_index,
                    "slot_id": slot["slot_id"],
                    "delay_sec": slot["delay_sec"],
                    "navigation_type": slot["navigation_type"],
                    "withdrawal_log_timing": slot["withdrawal_log_timing"],
                    "activity_check": {
                        "enabled": True,
                        "basis": "activity_since_sent_at",
                        "sent_at": target.get("sent_at", ""),
                        "purpose": "retargeting_rank",
                        "blocks_withdrawal": False,
                    },
                    "batch_diversion_target": diversion_target_slot == local_index,
                    "batch_diversion": batch["diversion"]
                    if diversion_target_slot == local_index
                    else "none",
                }
            )
            planned_targets.append(planned)
            queue.append(planned)
        batches.append(
            {
                "batch_number": batch_number,
                "batch_size": len(planned_targets),
                "configured_batch_size": batch["batch_size"],
                "inter_batch_delay_sec": batch["inter_batch_delay_sec"],
                "diversion": batch["diversion"],
                "diversion_sec": batch["diversion_sec"],
                "diversion_target_index": diversion_target_slot,
                "targets": planned_targets,
            }
        )

    selector_based_count = sum(
        1 for target in queue if target["navigation_type"] == "selector_based"
    )
    direct_url_count = sum(1 for target in queue if target["navigation_type"] == "direct_url")
    return {
        **generated_plan,
        "selected_count": len(queue),
        "batch_count": len(batches),
        "navigation_mix": {
            "selector_based": selector_based_count,
            "direct_url": direct_url_count,
            "selector_based_percent": round((selector_based_count / len(queue)) * 100, 2)
            if queue
            else 0,
            "minimum_selector_based_percent": MIN_SELECTOR_BASED_PERCENT,
        },
        "batches": batches,
        "queue": queue,
        "sequence_source": worksheet.title,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def append_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare overdue connection withdrawals from Outreach Log."
    )
    parser.add_argument("--credentials", default=CREDS_PATH)
    parser.add_argument("--sheet-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--date", default="", help="Run date as YYYY-MM-DD. Defaults to current date in timezone."
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP,
        help=f"Max queued targets for this run. Hard capped at {MAX_CAP}.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Alias for --cap for test runs.")
    parser.add_argument(
        "--all-due",
        action="store_true",
        help="Queue every due target for an intentional catch-up run, bypassing the normal cap.",
    )
    parser.add_argument("--seed", default="", help="Optional deterministic seed salt.")
    parser.add_argument("--sequence-tab", default=WITHDRAWAL_SEQUENCE_TAB)
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not write local session/journal files."
    )
    parser.add_argument("--session", default="", help="Override local session JSON path.")
    parser.add_argument("--journal", default="", help="Override local journal JSONL path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_date = (
        parse_sheet_date(args.date) if args.date else datetime.now(ZoneInfo(args.timezone)).date()
    )
    if not run_date:
        raise ValueError(f"Invalid --date: {args.date}")

    session_path = (
        Path(args.session)
        if args.session
        else ROOT / "state" / "withdrawal_sessions" / f"{run_date.isoformat()}.json"
    )
    journal_path = (
        Path(args.journal)
        if args.journal
        else ROOT / "state" / "withdrawal_journal" / f"{run_date.isoformat()}.jsonl"
    )

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(spreadsheet, OUTREACH_LOG_TAB)
    prospects_worksheet = get_worksheet(spreadsheet, PROSPECTS_TAB)
    sequence_worksheet = get_worksheet(spreadsheet, args.sequence_tab)
    headers, rows = read_worksheet_rows(worksheet)
    prospects_headers, prospects_rows = read_worksheet_rows(prospects_worksheet)
    index = header_index(headers)
    prospects_index = header_index(prospects_headers)
    require_columns(
        index,
        [
            "Prospect ID",
            "Company",
            "Person Engaged",
            "Contact Name",
            "Contact Linkedin",
            "Current Progress",
            "Touch Method",
            "Outcome",
            "Last Action Date",
            "Sent At",
            "Days Left",
            "Notes",
        ],
        OUTREACH_LOG_TAB,
    )
    require_columns(index, ["Primary Lane"], OUTREACH_LOG_TAB)
    require_columns(
        prospects_index, ["ID", "P1 LinkedIn", "P2 LinkedIn", "Engaged Person"], PROSPECTS_TAB
    )

    prospect_lookup = build_prospect_lookup(prospects_rows, prospects_index)
    assembled = build_available_targets(rows, index, prospect_lookup, run_date)
    requested_cap = (
        len(assembled["available_targets"])
        if args.all_due
        else (args.limit if args.limit > 0 else args.cap)
    )
    cap = max(1, requested_cap if args.all_due else min(requested_cap, MAX_CAP))
    generated_plan = prepare_runtime_plan(assembled["available_targets"], run_date, cap, args.seed)
    if not args.dry_run:
        write_sequence_plan(sequence_worksheet, generated_plan)
        plan = read_sequence_plan(sequence_worksheet, generated_plan["queue"], generated_plan)
    else:
        plan = generated_plan
    summary = {
        **assembled["counts"],
        "cap": cap,
        "selected_count": plan["selected_count"],
        "unselected_due_count": plan["unselected_due_count"],
        "batch_count": plan["batch_count"],
        "navigation_mix": plan["navigation_mix"],
    }
    payload = {
        "ok": True,
        "prepared_at": datetime.now(ZoneInfo(args.timezone)).isoformat(),
        "date": run_date.isoformat(),
        "source": {
            "sheet_url": args.sheet_url,
            "tab": OUTREACH_LOG_TAB,
            "sequence_tab": args.sequence_tab,
            "rule": "Current Progress = Conn Request and numeric Days Left <= 0; missing Contact Linkedin resolved from Prospects P1/P2 LinkedIn",
        },
        "cap": cap,
        "available_count": assembled["counts"]["available"],
        "selected_count": plan["selected_count"],
        "skipped": assembled["skipped"],
        "summary": summary,
        "runtime_plan": plan,
    }

    if not args.dry_run:
        write_json(session_path, payload)
        append_jsonl(
            journal_path,
            [
                {
                    "event": "withdrawal_queue_prepared",
                    "date": run_date.isoformat(),
                    "session": str(session_path),
                    "summary": summary,
                },
                *(
                    {
                        "event": "withdrawal_batch_prepared",
                        **{k: v for k, v in batch.items() if k != "targets"},
                    }
                    for batch in plan["batches"]
                ),
                *({"event": "withdrawal_target_prepared", **target} for target in plan["queue"]),
            ],
        )

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": bool(args.dry_run),
                "session": str(session_path),
                "journal": str(journal_path),
                "summary": summary,
                "first_targets": plan["queue"][:5],
                "batch_preview": [
                    {k: v for k, v in batch.items() if k != "targets"}
                    for batch in plan["batches"][:5]
                ],
                "skipped_sample": assembled["skipped"][:5],
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
