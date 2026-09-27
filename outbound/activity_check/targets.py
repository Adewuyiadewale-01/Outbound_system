"""Target extraction for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S7). Pure move.
"""

from typing import Any

from outbound.activity_check.sheets import person_from_row
from outbound.activity_check.text import (
    canonical_linkedin_profile_url,
    clean_text,
    normalize_activity_value,
    target_key,
)


def extract_targets(
    rows: list[dict[str, Any]],
    recorded_activity: dict[str, Any] | None = None,
    recorded_issues: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    recorded_activity = recorded_activity or {}
    recorded_issues = recorded_issues or {}
    targets: list[dict[str, Any]] = []
    for row in rows:
        if not clean_text(row.get("ID")) and not clean_text(row.get("Company")):
            continue
        for prefix in ("P1", "P2"):
            person = person_from_row(row, prefix)
            original_profile_url = clean_text(person.get("linkedin"))
            profile_url = canonical_linkedin_profile_url(original_profile_url)
            if not profile_url:
                continue
            # Once a durable queue decision exists, this profile is no longer
            # eligible for another Activity Check preparation.
            if normalize_activity_value(
                (recorded_activity.get(target_key(row, prefix)) or {}).get("activity_value", "")
            ):
                continue
            issue = recorded_issues.get(target_key(row, prefix)) or {}
            if (
                issue.get("terminal")
                and canonical_linkedin_profile_url(issue.get("profile_url")) == profile_url
            ):
                continue
            targets.append(
                {
                    "key": target_key(row, prefix),
                    "lead_id": clean_text(row.get("ID")),
                    "row_number": row.get("_row_number"),
                    "company": clean_text(row.get("Company")),
                    "prefix": prefix,
                    "profile_url": profile_url,
                    "original_profile_url": original_profile_url,
                    "profile_url_normalized": profile_url != original_profile_url.rstrip("/"),
                }
            )
    return targets
