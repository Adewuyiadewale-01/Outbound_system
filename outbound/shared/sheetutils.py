"""Generic sheet, parsing, and normalization utilities shared across workflows."""

from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _normalize_profile_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    normalized = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    # Canonicalize LinkedIn country subdomains (nl., de., uk., etc.) to www.
    normalized = re.sub(
        r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", normalized
    )
    return normalized


def _normalize_person_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _clamped_gauss(rng: random.Random, mean: int, stddev: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(rng.gauss(mean, stddev))))


def _parse_int(value: Any) -> int | None:
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _is_enabled(value: Any, default: bool = True) -> bool:
    raw = str(value).strip().lower()
    if raw == "":
        return default
    if raw in {"yes", "true", "1", "enabled", "y"}:
        return True
    if raw in {"no", "false", "0", "disabled", "n"}:
        return False
    return default


def _find_first_index(
    headers: list[str], label: str, start: int = 0, end: int | None = None
) -> int | None:
    needle = str(label).strip().lower()
    stop = len(headers) if end is None else end
    for i in range(start, stop):
        if str(headers[i]).strip().lower() == needle:
            return i
    return None


def _find_last_index(
    headers: list[str], label: str, start: int = 0, end: int | None = None
) -> int | None:
    needle = str(label).strip().lower()
    stop = len(headers) if end is None else end
    for i in range(stop - 1, start - 1, -1):
        if str(headers[i]).strip().lower() == needle:
            return i
    return None


def _col_to_a1(col_number: int) -> str:
    if col_number < 1:
        raise ValueError(f"Invalid column number for A1 conversion: {col_number}")
    chars: list[str] = []
    n = col_number
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _a1_cell(row_number: int, col_number: int) -> str:
    return f"{_col_to_a1(col_number)}{row_number}"


def _batch_update_cells(worksheet: Any, updates: list[tuple[int, int, Any]]) -> None:
    if not updates:
        return
    payload = [
        {
            "range": _a1_cell(row_number, col_number),
            "values": [[("" if value is None else value)]],
        }
        for row_number, col_number, value in updates
    ]
    worksheet.batch_update(payload, value_input_option="USER_ENTERED")
