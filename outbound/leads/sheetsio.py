"""Sheet IO and source-row canonicalization for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S3). Pure move.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sheets_helper import get_client, get_worksheet, normalize_rows, open_sheet

from outbound.leads.config import SOURCE_ALIASES
from outbound.leads.text import clean_text


def require_columns(headers: Sequence[str], required: Sequence[str], tab_name: str) -> None:
    missing = [column for column in required if column not in headers]
    if missing:
        raise ValueError(f"{tab_name} is missing required columns: {', '.join(missing)}")


def source_value(row: dict[str, Any], canonical_column: str) -> str:
    for candidate in SOURCE_ALIASES.get(canonical_column, [canonical_column]):
        value = clean_text(row.get(candidate))
        if value:
            return value
    return ""


def canonicalize_source_rows(
    headers: Sequence[str], rows: list[dict[str, Any]], source_tab: str
) -> list[dict[str, Any]]:
    available = set(headers)
    if not any(column in available for column in SOURCE_ALIASES["Company Name"]):
        raise ValueError(
            f"{source_tab} must contain a company name column. "
            f"Accepted names: {', '.join(SOURCE_ALIASES['Company Name'])}"
        )

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        for canonical_column in SOURCE_ALIASES:
            normalized[canonical_column] = source_value(row, canonical_column)
        if not normalized["ID"] and normalized["Company Name"]:
            normalized["ID"] = f"{source_tab}-{row.get('_row_number')}"
        if not normalized["Class"]:
            normalized["Class"] = source_tab
        normalized_rows.append(normalized)
    return normalized_rows


def read_worksheet(
    credentials_path: Path, sheet_url: str, tab_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, tab_name)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{tab_name} is empty.")
    headers = values[0]
    rows = normalize_rows(values)
    return headers, rows


def load_destination_ids(credentials_path: Path, sheet_url: str, destination_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, destination_tab)
    except Exception:
        return set()
    if "ID" not in headers:
        return set()
    return {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}


def load_destination_company_by_id(
    credentials_path: Path, sheet_url: str, destination_tab: str
) -> dict[str, str]:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, destination_tab)
    except Exception:
        return {}
    if "ID" not in headers:
        return {}
    company_column = (
        "Company" if "Company" in headers else "Company Name" if "Company Name" in headers else ""
    )
    if not company_column:
        return {}
    return {
        clean_text(row.get("ID")): clean_text(row.get(company_column))
        for row in rows
        if clean_text(row.get("ID"))
    }


def load_reviewed_ids(credentials_path: Path, sheet_url: str, review_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, review_tab)
    except Exception:
        return set()
    if "Run ID" not in headers:
        return set()
    return {clean_text(row.get("Run ID")) for row in rows if clean_text(row.get("Run ID"))}
