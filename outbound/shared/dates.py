"""Date parsing and normalization shared across workflows."""

from __future__ import annotations

from datetime import date, datetime


def _parse_date(value: str) -> date:
    value = str(value).strip()
    if not value:
        return date.today()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value}")


def sheet_date(value: str | None) -> str:
    parsed = _parse_date(value) if value else date.today()
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def sequence_date_key(value: str) -> str:
    return _parse_date(value).isoformat()
