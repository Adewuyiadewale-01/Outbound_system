"""Review-tab rows and review-status updates for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S10). Pure move.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import WorksheetNotFound

from outbound.leads.archive import MATCH_AVAILABLE, MATCH_CONFLICT, MATCH_CONSUMED, MATCH_FRESH
from outbound.leads.config import REVIEW_COLUMNS, REVIEW_OVERLAP_COLUMNS
from outbound.leads.sheetsio import read_worksheet, require_columns
from outbound.leads.text import clean_text
from outbound.shared.sheets import get_client, get_worksheet, open_sheet


def get_or_create_worksheet(spreadsheet, tab_name: str, rows: int = 1000, cols: int = 26):
    try:
        return spreadsheet.worksheet(tab_name)
    except WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)


def ensure_review_tab(credentials_path: Path, sheet_url: str, review_tab: str):
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    headers = worksheet.row_values(1)
    require_columns(headers, REVIEW_COLUMNS, review_tab)
    if "Research Source" in headers and "Overlap Status" not in headers:
        column_index = headers.index("Research Source") + 1
        worksheet.update_cell(1, column_index, "Overlap Status")
        headers[column_index - 1] = "Overlap Status"
    missing_overlap_columns = [column for column in REVIEW_OVERLAP_COLUMNS if column not in headers]
    if missing_overlap_columns:
        required_column_count = len(headers) + len(missing_overlap_columns)
        if worksheet.col_count < required_column_count:
            worksheet.add_cols(required_column_count - worksheet.col_count)
        start_column = len(headers) + 1
        end_column = start_column + len(missing_overlap_columns) - 1
        worksheet.update(
            range_name=(
                f"{gspread.utils.rowcol_to_a1(1, start_column)}:"
                f"{gspread.utils.rowcol_to_a1(1, end_column)}"
            ),
            values=[missing_overlap_columns],
            value_input_option="USER_ENTERED",
        )
        headers.extend(missing_overlap_columns)
    return worksheet


def build_review_row(run: dict[str, Any], run_file: Path, lead: dict[str, Any]) -> dict[str, Any]:
    employees = lead.get("employees_from_sheet", [])
    archive_match = lead.get("archive_match", {})
    overlap_status = {
        MATCH_AVAILABLE: "Archive Match",
        MATCH_CONFLICT: "Possible Match",
        MATCH_CONSUMED: "Already Consumed",
        MATCH_FRESH: "Fresh",
    }.get(archive_match.get("status"), "Fresh")
    return {
        "Run ID": lead.get("id", ""),
        "Primary Lane": lead.get("primary_lane", ""),
        "Company Name": lead.get("company", {}).get("name", ""),
        "Company Website": lead.get("company", {}).get("website", ""),
        "Emp Count": lead.get("company", {}).get("employee_count") or len(employees),
        "Approved": False,
        "Use": "",
        "Prep Wave": lead.get("prep_wave", "Base"),
        "Overlap Status": overlap_status,
        "Archive Entry ID": archive_match.get("archive_entry_id", ""),
    }


def review_group_date_value(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.month}/{now.day}/{now.year}"


def parse_review_group_date(value: Any):
    raw = clean_text(value)
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def last_nonempty_review_row(headers: list[str], values: list[list[Any]]) -> int:
    content_columns = ["Date", "Run ID", "Company Name", "Company Website"]
    indexes = [headers.index(column) for column in content_columns if column in headers]
    last_row = 1
    for row_number, row in enumerate(values, start=1):
        padded = row + [""] * (len(headers) - len(row))
        if any(clean_text(padded[index]) for index in indexes):
            last_row = row_number
    return last_row


def existing_successful_review_group_row(
    headers: list[str],
    values: list[list[Any]],
    target_date,
) -> int | None:
    date_idx = headers.index("Date")
    run_id_idx = headers.index("Run ID")
    group_row: int | None = None
    saw_lead_in_group = False

    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        row_date = parse_review_group_date(padded[date_idx])
        row_run_id = clean_text(padded[run_id_idx])
        if row_date:
            if group_row is not None and saw_lead_in_group:
                return group_row
            group_row = row_number if row_date == target_date else None
            saw_lead_in_group = False
            continue
        if group_row is not None and row_run_id:
            saw_lead_in_group = True

    if group_row is not None and saw_lead_in_group:
        return group_row
    return None


def find_successful_review_group_row_for_today(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
) -> int | None:
    worksheet = ensure_review_tab(credentials_path, sheet_url, review_tab)
    headers = worksheet.row_values(1)
    if "Date" not in headers or "Run ID" not in headers:
        return None
    return existing_successful_review_group_row(
        headers, worksheet.get_all_values(), datetime.now().date()
    )


def write_review_rows(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run: dict[str, Any],
    run_file: Path,
) -> int:
    worksheet = ensure_review_tab(credentials_path, sheet_url, review_tab)
    rows = [build_review_row(run, run_file, lead) for lead in run.get("leads", [])]
    if not rows:
        return 0
    headers = worksheet.row_values(1)
    if "Date" not in headers:
        raise ValueError(f"{review_tab} is missing required Date column for daily group rows.")
    if "Run ID" not in headers:
        raise ValueError(f"{review_tab} is missing required Run ID column for daily group rows.")
    payload = [[row.get(header, "") for header in headers] for row in rows]
    group_date = review_group_date_value()
    values = worksheet.get_all_values()
    existing_group_row = existing_successful_review_group_row(
        headers, values, datetime.now().date()
    )
    if existing_group_row is not None:
        raise ValueError(
            f"{review_tab} already has a successful review queue for {group_date} "
            f"at group row {existing_group_row}. Refusing to prepare a second queue for the same day."
        )
    group_row = last_nonempty_review_row(headers, values) + 1
    start_row = group_row + 1
    end_row = start_row + len(payload) - 1
    if worksheet.row_count < end_row:
        raise ValueError(
            f"{review_tab} has {worksheet.row_count} total rows, but writing the daily group at row "
            f"{group_row} plus {len(payload)} leads needs through row {end_row}. "
            "Add rows manually before running prepare-review."
        )
    worksheet.update(
        range_name=gspread.utils.rowcol_to_a1(group_row, headers.index("Date") + 1),
        values=[[group_date]],
        value_input_option="USER_ENTERED",
    )
    worksheet.update(range_name=f"A{start_row}", values=payload, value_input_option="USER_ENTERED")
    run["review_write"] = {
        "group_row": group_row,
        "start_row": start_row,
        "end_row": end_row,
        "date": group_date,
    }
    return len(payload)


def read_review_rows(
    credentials_path: Path, sheet_url: str, review_tab: str, run_id: str
) -> list[dict[str, Any]]:
    headers, rows = read_worksheet(credentials_path, sheet_url, review_tab)
    require_columns(headers, REVIEW_COLUMNS, review_tab)
    return [row for row in rows if clean_text(row.get("Run ID"))]


def remove_review_group_for_run(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    values = worksheet.get_all_values()
    if not values:
        return {"removed": False, "reason": "review_tab_empty"}
    headers = values[0]
    require_columns(headers, ["Date", "Run ID"], review_tab)
    date_index = headers.index("Date")
    run_id_index = headers.index("Run ID")
    review_complete_index = (
        headers.index("Design Review Complete") if "Design Review Complete" in headers else None
    )
    expected_ids = {
        clean_text(lead.get("id")) for lead in run.get("leads", []) if clean_text(lead.get("id"))
    }
    target_date = parse_review_group_date(
        run.get("review", {}).get("write", {}).get("date")
        or run.get("review_write", {}).get("date")
    )
    groups = []
    current = None
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        parsed_date = parse_review_group_date(padded[date_index])
        if parsed_date:
            if current:
                current["end_row"] = row_number - 1
                groups.append(current)
            current = {
                "group_row": row_number,
                "end_row": row_number,
                "date": parsed_date,
                "review_complete": (
                    checkbox_truthy(padded[review_complete_index])
                    if review_complete_index is not None
                    else False
                ),
                "lead_ids": set(),
            }
            continue
        if current and clean_text(padded[run_id_index]):
            current["lead_ids"].add(clean_text(padded[run_id_index]))
            current["end_row"] = row_number
    if current:
        groups.append(current)

    candidates = [
        group
        for group in groups
        if (target_date is None or group["date"] == target_date)
        and expected_ids
        and expected_ids.issubset(group["lead_ids"])
    ]
    if len(candidates) != 1:
        return {
            "removed": False,
            "reason": "review_group_not_uniquely_resolved",
            "candidate_count": len(candidates),
            "target_date": target_date.isoformat() if target_date else "",
        }
    group = candidates[0]
    if group["review_complete"]:
        raise ValueError("Refusing to remove a review-complete date group as unreviewed.")
    start_index = group["group_row"] - 1
    end_index = group["end_row"]
    spreadsheet.batch_update(
        {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "ROWS",
                            "startIndex": start_index,
                            "endIndex": end_index,
                        }
                    }
                }
            ]
        }
    )
    return {
        "removed": True,
        "group_row": group["group_row"],
        "end_row": group["end_row"],
        "rows_removed": end_index - start_index,
        "date": group["date"].isoformat(),
        "lead_count": len(group["lead_ids"]),
    }


def checkbox_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "y", "1", "checked"}


def update_review_statuses(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run_id: str,
    status_by_id: dict[str, str],
) -> None:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    values = worksheet.get_all_values()
    if not values:
        return
    headers = values[0]
    if "Run ID" not in headers or "ID" not in headers or "Status" not in headers:
        return
    run_idx = headers.index("Run ID")
    id_idx = headers.index("ID")
    status_idx = headers.index("Status") + 1
    updates = []
    for row_number, row in enumerate(values[1:], start=2):
        row += [""] * (len(headers) - len(row))
        if clean_text(row[run_idx]) != run_id:
            continue
        lead_id = clean_text(row[id_idx])
        if lead_id in status_by_id:
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, status_idx),
                    "values": [[status_by_id[lead_id]]],
                }
            )
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
