"""Sheet IO helpers for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S6). Pure move.
"""

from pathlib import Path
from typing import Any

from outbound.activity_check.text import clean_text, normalize_activity_value, normalize_url
from outbound.shared.sheets import get_client, normalize_rows, open_sheet


def read_worksheet(
    credentials_path: Path, sheet_url: str, tab_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = spreadsheet.worksheet(tab_name)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{tab_name} is empty")
    return values[0], normalize_rows(values)


def person_from_row(row: dict[str, Any], prefix: str) -> dict[str, str]:
    return {
        "name": clean_text(row.get(f"{prefix} Name")),
        "title": clean_text(row.get(f"{prefix} Title")),
        "linkedin": normalize_url(row.get(f"{prefix} LinkedIn")),
        "email": clean_text(row.get(f"{prefix} Email")),
        "activity": clean_text(row.get(f"{prefix} Activity")),
    }


def set_person(row: dict[str, Any], prefix: str, person: dict[str, str]) -> None:
    row[f"{prefix} Name"] = clean_text(person.get("name"))
    row[f"{prefix} Title"] = clean_text(person.get("title"))
    row[f"{prefix} LinkedIn"] = normalize_url(person.get("linkedin"))
    row[f"{prefix} Email"] = clean_text(person.get("email"))
    row[f"{prefix} Activity"] = normalize_activity_value(person.get("activity", ""))
