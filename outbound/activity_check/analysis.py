"""Activity classification and Final ranking (the scoring brain).

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S8). Pure move.
"""

from collections.abc import Sequence
from typing import Any

from outbound.activity_check.config import ACTIVITY_SCORE, BASE_COLUMNS, CATEGORY_PRIORITY
from outbound.activity_check.sheets import person_from_row, set_person
from outbound.activity_check.text import (
    clean_text,
    is_linkedin_profile_url,
    normalize_activity_value,
)
from outbound.shared.activity.readers import relative_days_from_time_text


def is_aggregate_activity_container(activity: dict[str, Any]) -> bool:
    sample = clean_text(activity.get("card_text_sample")).lower()
    return sample.startswith("all activity posts comments") and "loaded " in sample


def non_aggregate_entries(tab: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in tab.get("activities", []) or []
        if not is_aggregate_activity_container(item)
    ]


def count_entries_within(tab: dict[str, Any], days_limit: int) -> int:
    count = 0
    for activity in non_aggregate_entries(tab):
        days = relative_days_from_time_text(activity.get("time_text", ""))
        if days is not None and days <= days_limit:
            count += 1
    return count


def activity_detail_should_defer(activity_detail: dict[str, Any]) -> bool:
    return activity_detail_empty_success_reason(activity_detail) == "activity_feed_not_hydrated"


def detail_contains_text(value: Any, needles: Sequence[str]) -> bool:
    lowered_needles = [needle.lower() for needle in needles if needle]
    if not lowered_needles:
        return False
    if isinstance(value, str):
        text = value.lower()
        return any(needle in text for needle in lowered_needles)
    if isinstance(value, dict):
        return any(detail_contains_text(item, lowered_needles) for item in value.values())
    if isinstance(value, list):
        return any(detail_contains_text(item, lowered_needles) for item in value)
    return False


def activity_detail_empty_success_reason(activity_detail: dict[str, Any]) -> str:
    if not isinstance(activity_detail, dict) or activity_detail.get("error"):
        return ""
    tabs = activity_detail.get("tabs", {}) or {}
    checked_tabs = [
        tabs.get(name, {}) or {} for name in ("posts", "reactions", "comments") if name in tabs
    ]
    if detail_contains_text(activity_detail, ("invalid_profile_or_404", "/404/")):
        return "invalid_profile_or_404"
    if not checked_tabs:
        return "activity_feed_not_hydrated"
    if not activity_level(activity_detail) and any(
        tab.get("activity_classification_uncertain") for tab in checked_tabs
    ):
        return "activity_classification_uncertain"
    if any(
        int(tab.get("total_visible", 0) or 0) > 0 or non_aggregate_entries(tab)
        for tab in checked_tabs
    ):
        return ""
    if all(
        clean_text((tab.get("feed_state") or {}).get("reason")) == "explicit_empty_state"
        for tab in checked_tabs
    ):
        return ""
    return "activity_feed_not_hydrated"


def activity_level(activity_detail: dict[str, Any]) -> str:
    if not activity_detail or activity_detail.get("error"):
        return ""
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    posts = tabs.get("posts", {}) or {}
    comments = tabs.get("comments", {}) or {}
    reactions = tabs.get("reactions", {}) or {}
    if not any(non_aggregate_entries(tab) for tab in (posts, comments, reactions)):
        checked_tabs = [
            tabs.get(name, {}) or {} for name in ("posts", "reactions", "comments") if name in tabs
        ]
        all_explicitly_empty = len(checked_tabs) == 3 and all(
            clean_text((tab.get("feed_state") or {}).get("reason")) == "explicit_empty_state"
            for tab in checked_tabs
        )
        if all_explicitly_empty:
            return "Not active"
        return ""
    if (
        count_entries_within(posts, 7) >= 1
        or count_entries_within(comments, 7) >= 2
        or count_entries_within(reactions, 7) >= 2
    ):
        return "Very active"
    if (
        count_entries_within(posts, 14) >= 1
        or count_entries_within(comments, 30) + count_entries_within(reactions, 30) >= 5
    ):
        return "Active"
    if any(tab.get("activity_classification_uncertain") for tab in (posts, comments, reactions)):
        return ""
    return "Not active"


def activity_evidence(profile_url: str, activity_detail: dict[str, Any]) -> dict[str, Any]:
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    evidence: dict[str, Any] = {
        "profile_url": profile_url,
        "original_profile_url": activity_detail.get("original_profile_url", "")
        if isinstance(activity_detail, dict)
        else "",
        "canonical_profile_url": activity_detail.get("canonical_profile_url", profile_url)
        if isinstance(activity_detail, dict)
        else profile_url,
        "profile_url_normalized": bool(activity_detail.get("profile_url_normalized"))
        if isinstance(activity_detail, dict)
        else False,
        "navigation_type_used": activity_detail.get("navigation_type_used", "")
        if isinstance(activity_detail, dict)
        else "",
        "fresh_tab_recovery": bool(activity_detail.get("fresh_tab_recovery"))
        if isinstance(activity_detail, dict)
        else False,
        "page_url": activity_detail.get("page_url", "")
        if isinstance(activity_detail, dict)
        else "",
        "reason": activity_detail.get("reason", "") if isinstance(activity_detail, dict) else "",
        "level": activity_level(activity_detail),
        "error": bool(activity_detail.get("error")) if isinstance(activity_detail, dict) else True,
        "danger": activity_detail.get("danger", "") if isinstance(activity_detail, dict) else "",
        "tabs": {},
    }
    for tab_name in ("posts", "comments", "reactions"):
        tab = tabs.get(tab_name, {}) or {}
        entries = []
        for item in non_aggregate_entries(tab)[:8]:
            time_text = clean_text(item.get("time_text"))
            entries.append(
                {
                    "time_text": time_text,
                    "days": relative_days_from_time_text(time_text),
                    "sample": clean_text(item.get("card_text_sample"))[:160],
                }
            )
        evidence["tabs"][tab_name] = {
            "total_visible": int(tab.get("total_visible", 0) or 0),
            "real_entries": len(non_aggregate_entries(tab)),
            "uncertain": bool(tab.get("activity_classification_uncertain")),
            "entries": entries,
        }
    return evidence


def activity_score(activity: str) -> int:
    return ACTIVITY_SCORE.get(normalize_activity_value(activity), 0)


def should_swap(p1_activity: str, p2_activity: str) -> bool:
    p2_score = activity_score(p2_activity)
    return p2_score > 0 and p2_score > activity_score(p1_activity)


def category_for_row(row: dict[str, Any]) -> str:
    activities = [
        normalize_activity_value(row.get("P1 Activity")),
        normalize_activity_value(row.get("P2 Activity")),
    ]
    if not any(activities):
        return ""
    linkedin_count = sum(
        1
        for url in (row.get("P1 LinkedIn"), row.get("P2 LinkedIn"))
        if is_linkedin_profile_url(url)
    )
    if linkedin_count >= 2:
        if "Very active" in activities:
            return "Hyper"
        if "Active" in activities:
            return "High"
        return "Low"
    if linkedin_count == 1:
        if "Very active" in activities:
            return "Alpha-medium"
        if "Active" in activities:
            return "Medium"
        return "Low"
    return ""


def rank_row_for_final(
    source_row: dict[str, Any], activity_by_prefix: dict[str, str]
) -> dict[str, Any]:
    ranked = {column: clean_text(source_row.get(column)) for column in BASE_COLUMNS}
    p1 = person_from_row(source_row, "P1")
    p2 = person_from_row(source_row, "P2")
    p1["activity"] = normalize_activity_value(activity_by_prefix.get("P1"))
    p2["activity"] = normalize_activity_value(activity_by_prefix.get("P2"))
    if should_swap(p1.get("activity", ""), p2.get("activity", "")):
        p1, p2 = p2, p1
    set_person(ranked, "P1", p1)
    set_person(ranked, "P2", p2)
    ranked["Category"] = category_for_row(ranked)
    ranked["Engaged Person"] = clean_text(source_row.get("Engaged Person")) or (
        "Person 1" if is_linkedin_profile_url(ranked.get("P1 LinkedIn")) else "Person 2"
    )
    return ranked


def final_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        CATEGORY_PRIORITY.get(clean_text(row.get("Category")), 99),
        0 if is_linkedin_profile_url(row.get("P2 LinkedIn")) else 1,
        clean_text(row.get("Company")).lower(),
    )
