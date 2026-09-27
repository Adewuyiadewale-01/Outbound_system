"""Source-row grouping and primary-lane assignment for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S4). Pure move.
"""

from collections.abc import Sequence
from typing import Any

from outbound.leads.text import clean_role_for_person, clean_text, normalize_url


def looks_like_company_row(row: dict[str, Any]) -> bool:
    return bool(clean_text(row.get("ID")) and clean_text(row.get("Company Name")))


def looks_like_employee_row(row: dict[str, Any]) -> bool:
    fields = ["Person Name", "Person Role", "Person Email", "Person Linkedin"]
    return any(clean_text(row.get(field)) for field in fields)


def group_source_rows(rows: list[dict[str, Any]], source_tab: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        if looks_like_company_row(row):
            current = {
                "id": clean_text(row.get("ID")),
                "company": {
                    "name": clean_text(row.get("Company Name")),
                    "website": normalize_url(row.get("Company Website")),
                    "linkedin": normalize_url(row.get("Company Linkedin")),
                    "employee_count": clean_text(row.get("Company Employee Count")),
                    "class": clean_text(row.get("Class")),
                },
                "source_tab": source_tab,
                "source_rows": {
                    "company_row": row.get("_row_number"),
                    "employee_rows": [],
                },
                "employees_from_sheet": [],
                "status": "pending",
                "errors": [],
            }
            groups.append(current)
            if looks_like_employee_row(row):
                add_employee(current, row)
            continue
        if current and looks_like_employee_row(row):
            add_employee(current, row)
    return groups


def add_employee(group: dict[str, Any], row: dict[str, Any]) -> None:
    name = clean_text(row.get("Person Name"))
    employee = {
        "name": name,
        "role": clean_role_for_person(name, row.get("Person Role")),
        "email": clean_text(row.get("Person Email")),
        "linkedin": normalize_url(row.get("Person Linkedin")),
        "source_row": row.get("_row_number"),
    }
    group["employees_from_sheet"].append(employee)
    group["source_rows"]["employee_rows"].append(row.get("_row_number"))


def chunks(items: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def assign_primary_lanes(leads: list[dict[str, Any]]) -> dict[str, int]:
    """Assign balanced lanes, grouped for a Design-first review pass."""
    automation_count = (len(leads) + 1) // 2
    design_count = len(leads) - automation_count
    for lead in leads[:design_count]:
        lead["primary_lane"] = "Design"
    for lead in leads[design_count:]:
        lead["primary_lane"] = "Automation"
    return {"Automation": automation_count, "Design": design_count}
