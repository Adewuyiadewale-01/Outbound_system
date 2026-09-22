"""Assessment engine for the engagement workflow."""

from __future__ import annotations

import re
from typing import Any

from outbound.engagement.parsing import parse_follower_count, parse_relative_age_hours
from outbound.shared.state import now


def validate_profile_gate(gate: dict[str, Any]) -> None:
    """Reject global LinkedIn UI labels before they can affect ranking."""
    name = str(gate.get("name") or "").strip()
    location = str(gate.get("location") or "").strip()
    followers = str(gate.get("follower_text") or "").strip()
    follower_source = str(gate.get("follower_source") or "").strip()
    error = str(gate.get("error") or "").strip()
    if error:
        raise RuntimeError(f"profile_gate_{error}")
    if not name or re.search(r"\bnotifications?\b", name, re.IGNORECASE):
        raise RuntimeError("profile_gate_invalid_name")
    if not location or location.casefold() == "contact info":
        raise RuntimeError("profile_gate_invalid_location")
    if not parse_follower_count(followers):
        raise RuntimeError("profile_gate_missing_followers")
    if follower_source not in {"header", "activity"}:
        raise RuntimeError("profile_gate_unbounded_follower_source")


def activity_counts(detail: dict[str, Any], max_hours: float) -> dict[str, int]:
    tabs = detail.get("tabs", {}) if isinstance(detail, dict) else {}
    counts = {}
    for key in ("reactions", "comments", "posts"):
        activities = (
            tabs.get(key, {}).get("activities", []) if isinstance(tabs.get(key), dict) else []
        )
        counts[key] = sum(
            1
            for item in activities
            if (age := parse_relative_age_hours(item.get("time_text", ""))) is not None
            and age <= max_hours
        )
    return counts


def qualifies(counts: dict[str, int], config: dict[str, Any]) -> bool:
    return (
        counts.get("reactions", 0) >= int(config["reaction_threshold"])
        or counts.get("comments", 0) >= int(config["comment_threshold"])
        or counts.get("posts", 0) >= int(config["post_threshold"])
    )


def recommendation_for(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    counts = candidate.get("activity_counts", {})
    if not qualifies(counts, config):
        action = "ineligible"
        reason = "activity_below_threshold"
    elif int(candidate.get("follower_count") or 0) > int(config["follower_connection_limit"]):
        action = "follow"
        reason = "active_profile_above_follower_connection_limit"
    else:
        action = "connect"
        reason = "active_profile_within_follower_connection_limit"
    return {
        "action": action,
        "reason": reason,
        "assessed_at": now().isoformat(),
        "assessment_version": ACTIVITY_ASSESSMENT_VERSION,
    }


ACTIVITY_ASSESSMENT_VERSION = 1
