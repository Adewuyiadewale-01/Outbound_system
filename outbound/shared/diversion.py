"""Deterministic human-behavior diversions shared across workflows.

Seeded planners generate stable per-day diversion plans; the executor drives a
browser session through the chosen behavior. Consumers: outreach (lead loop),
engagement (obf-style diversions), activity check (prefinal bridge).
"""

from __future__ import annotations

import random
import time
from typing import Any

from outbound.shared.dates import sequence_date_key, sheet_date


def generate_lead_diversion_plan(date_value: str, slot_ids: list[int]) -> dict[str, Any]:
    ordered_slots = sorted(int(x) for x in slot_ids)
    options = [
        ("none", 45),
        ("feed_scroll", 20),
        ("engagement_trail", 14),
        ("profile_drill", 9),
        ("company_page_browse", 7),
        ("recent_post_read", 5),
    ]
    values = [item[0] for item in options]
    weights = [item[1] for item in options]
    rng = random.Random(
        f"linkedin-outreach-lead-diversion:{sequence_date_key(date_value)}:{','.join(map(str, ordered_slots))}"
    )

    entries = []
    for slot_id in ordered_slots:
        diversion = rng.choices(values, weights=weights, k=1)[0]
        entries.append({"slot_id": slot_id, "lead_diversion": diversion})

    return {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "slot_count": len(ordered_slots),
        "entries": entries,
    }


def _diversion_range(diversion: str) -> tuple[int, int] | None:
    kind = str(diversion).strip().lower()
    if kind in {"", "none"}:
        return None
    if kind == "feed_scroll":
        return (12, 45)
    if kind == "engagement_trail":
        return (25, 90)
    if kind == "profile_drill":
        return (35, 120)
    if kind == "company_page_browse":
        return (20, 80)
    if kind == "recent_post_read":
        return (15, 65)
    # Fallback for future diversion values.
    return (20, 75)


def generate_lead_diversion_seconds_plan(
    date_value: str,
    slot_diversions: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized = sorted(
        [
            {
                "slot_id": int(item["slot_id"]),
                "lead_diversion": str(item["lead_diversion"]).strip().lower(),
            }
            for item in slot_diversions
        ],
        key=lambda x: x["slot_id"],
    )
    seed_key = ",".join(f"{item['slot_id']}:{item['lead_diversion']}" for item in normalized)
    rng = random.Random(
        f"linkedin-outreach-lead-diversion-sec:{sequence_date_key(date_value)}:{seed_key}"
    )

    entries = []
    for item in normalized:
        bounds = _diversion_range(item["lead_diversion"])
        seconds = None if bounds is None else rng.randint(bounds[0], bounds[1])
        entries.append(
            {
                "slot_id": item["slot_id"],
                "lead_diversion": item["lead_diversion"],
                "lead_diversion_sec": seconds,
            }
        )

    return {
        "date": sheet_date(date_value),
        "date_key": sequence_date_key(date_value),
        "slot_count": len(normalized),
        "entries": entries,
    }


def _run_diversion(
    session: Any,
    diversion: str,
    diversion_sec: int | None,
    activity: dict[str, Any] | None = None,
    company_linkedin: str = "",
) -> dict[str, Any]:
    kind = str(diversion).strip().lower()
    if kind in {"", "none"}:
        return {"ok": True, "executed": False, "reason": "none"}

    seconds = diversion_sec
    if seconds is None:
        bounds = _diversion_range(kind)
        if bounds:
            seconds = bounds[0]
    seconds = max(0, int(seconds or 0))

    payload: dict[str, Any] = {"ok": True, "executed": True, "type": kind, "seconds": seconds}
    post_url = _first_post_url(activity or {})
    try:
        if kind == "feed_scroll":
            stops = max(2, min(8, int(max(seconds, 20) / 12)))
            payload["result"] = session.read_feed(num_stops=stops)
        elif kind == "engagement_trail":
            if post_url:
                payload["result"] = session.engagement_trail(post_url, max_profiles=3)
            else:
                payload["result"] = session.read_feed(num_stops=3)
                payload["fallback"] = "feed_scroll_no_post_url"
        elif kind == "profile_drill":
            payload["result"] = session.prospect_box()
        elif kind == "company_page_browse":
            company_url = str(company_linkedin or "").strip()
            if not company_url:
                # Company URLs are optional enrichment, so this one diversion
                # must not turn an otherwise valid lead into a failed run.
                return {
                    "ok": True,
                    "executed": False,
                    "type": kind,
                    "reason": "missing_company_linkedin",
                }
            payload["result"] = session.cdp.navigate(company_url)
        elif kind == "recent_post_read":
            if post_url:
                payload["result"] = session.reaction_scan(post_url)
            else:
                payload["result"] = session.read_feed(num_stops=2)
                payload["fallback"] = "feed_scroll_no_post_url"
        else:
            payload["executed"] = False
            payload["reason"] = f"unsupported_diversion:{kind}"

        if seconds > 0:
            time.sleep(seconds)
    except Exception as exc:
        return {"ok": False, "executed": False, "type": kind, "error": str(exc), "seconds": seconds}
    return payload


def _first_post_url(activity: dict[str, Any]) -> str | None:
    for item in activity.get("recent_items", []):
        post_url = item.get("post_url")
        if post_url:
            return post_url
    return None
