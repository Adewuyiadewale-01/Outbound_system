"""Pure parsing and classification helpers for the engagement workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from outbound.shared.state import read_json

ROOT = Path(__file__).resolve().parents[2]
GEO_PATH = ROOT / "config" / "post_engagement_geography.json"


def canonical_profile_url(value: str) -> str:
    match = re.search(r"(?:https?://(?:www\.)?linkedin\.com)?(/in/[^/?#]+)", str(value or ""), re.I)
    return f"https://www.linkedin.com{match.group(1).rstrip('/')}/" if match else ""


def parse_relative_age_hours(value: str) -> float | None:
    text = re.sub(r"[·•]", "", str(value or "").strip().lower())
    if text in {"now", "just now"}:
        return 0.0
    if text == "today":
        return 12.0
    if text == "yesterday":
        return 24.0
    match = re.search(r"(\d+)\s*(m|min|h|hr|d|day|w|week)", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return (
        amount / 60
        if unit in {"m", "min"}
        else amount
        if unit in {"h", "hr"}
        else amount * 24
        if unit in {"d", "day"}
        else amount * 168
    )


def parse_follower_count(value: str) -> int | None:
    text = str(value or "").lower().replace(",", "").strip()
    match = re.search(r"([\d.]+)\s*([km]?)\s+followers?", text)
    if not match:
        return None
    multiplier = 1000 if match.group(2) == "k" else 1_000_000 if match.group(2) == "m" else 1
    return round(float(match.group(1)) * multiplier)


def classify_location(raw_location: str) -> dict[str, Any]:
    data = read_json(GEO_PATH, {})
    normalized = re.sub(r"[^a-z0-9]+", " ", str(raw_location or "").lower()).strip()
    code = ""
    method = "unknown"
    for alias, candidate in data.get("subdivision_aliases", {}).items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            code, method = candidate, "subdivision_alias"
            break
    if not code:
        for candidate, details in data.get("countries", {}).items():
            if any(
                re.search(rf"\b{re.escape(name)}\b", normalized)
                for name in details.get("names", [])
            ):
                code, method = candidate, "country_name"
                break
    details = data.get("countries", {}).get(code, {})
    region = details.get("region", "Unknown")
    if code in data.get("last_resort_countries", []):
        tier = 3
    elif region in data.get("preferred_regions", []):
        tier = 1
    elif code:
        tier = 2
    else:
        tier = 4
    return {
        "raw_location": raw_location,
        "normalized_country": (details.get("names") or [""])[0].title(),
        "country_code": code,
        "region": region,
        "geography_tier": tier,
        "classification_method": method,
    }
