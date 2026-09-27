"""Text/URL parsing helpers for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S2). Pure move.
"""

import re
from typing import Any

from outbound.activity_check.config import ACTIVITY_VALUES
from outbound.shared.activity.url_utils import canonicalize_linkedin_profile_url


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _parse_positive_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_url(value: Any) -> str:
    raw = clean_text(value).replace(" ", "")
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    return raw


def is_linkedin_profile_url(value: Any) -> bool:
    return bool(canonical_linkedin_profile_url(value))


def normalized_profile_key(value: Any) -> str:
    return canonical_linkedin_profile_url(value).lower()


def canonical_linkedin_profile_url(value: Any) -> str:
    """Use one neutral LinkedIn profile URL at prep and execution boundaries."""
    return canonicalize_linkedin_profile_url(value)


def normalize_activity_value(value: Any) -> str:
    text = clean_text(value)
    return text if text in ACTIVITY_VALUES else ""


def normalize_navigation_type(value: Any) -> str:
    text = clean_text(value).lower().replace("_", " ").replace("-", " ")
    if text in {"selector based", "selector", "click through", "clickthrough"}:
        return "Selector-based"
    if text in {"direct url", "direct", "url"}:
        return "Direct Url"
    return "Direct Url"


def navigation_type_key(value: Any) -> str:
    return (
        "selector_based" if normalize_navigation_type(value) == "Selector-based" else "direct_url"
    )


def target_key(row: dict[str, Any], prefix: str) -> str:
    lead_id = clean_text(row.get("ID")) or f"row:{row.get('_row_number')}"
    return f"{lead_id}:{prefix}"
