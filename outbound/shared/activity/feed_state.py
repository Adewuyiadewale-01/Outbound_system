"""Activity feed-state waiters and destination matching.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S12). Pure move.
"""

import json
import time
from typing import Any

from outbound.shared.activity.url_utils import (
    _activity_destination_matches,
    _page_still_loading,
)
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers, detect_page


def _wait_for_activity_destination(
    cdp: CDPConnection,
    profile_base: str,
    tab_key: str,
    timeout: float = 16.0,
    poll_interval: float = 0.35,
) -> dict[str, Any]:
    """Wait until async navigation reaches the requested activity tab or a hard invalid page.

    LinkedIn's SPA router passes through a transitional /preload/ URL while
    fetching the activity feed. That phase is navigation-in-progress, not a
    failed arrival — it neither matches the destination nor counts against
    the timeout budget. Only non-transit time consumes the deadline.
    """
    started = time.time()
    last_url = ""
    transit_seconds = 0.0

    def effective_elapsed() -> float:
        return time.time() - started - transit_seconds

    while True:
        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {
                "arrived": False,
                "invalid": invalid,
                "url": invalid.get("url", ""),
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        try:
            last_url = str(cdp.evaluate("window.location.href", timeout=5) or "")
        except Exception as exc:
            return {
                "arrived": False,
                "reason": "url_probe_failed",
                "error": str(exc),
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        if _activity_destination_matches(last_url, profile_base, tab_key):
            return {
                "arrived": True,
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        if "/preload/" in last_url:
            # Navigation in progress: the click is being honored, the SPA
            # router just hasn't swapped the URL yet. Wait without burning
            # the arrival budget.
            time.sleep(poll_interval)
            transit_seconds += poll_interval
            continue
        if _page_still_loading(cdp):
            # Circumstantial grace: the page reports active loading or pending
            # network fetches. A fixed budget is unfair to slow networks —
            # wait without counting, same as transit.
            time.sleep(poll_interval)
            transit_seconds += poll_interval
            continue
        if effective_elapsed() >= max(0.5, timeout):
            return {
                "arrived": False,
                "reason": "activity_destination_not_reached",
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        remaining = max(0.05, timeout - effective_elapsed())
        time.sleep(min(max(0.05, poll_interval), remaining))


def _activity_invalid_result(cdp: CDPConnection) -> dict[str, Any] | None:
    """Detect profile-level invalid pages before a blank can be misclassified."""
    page = detect_page(cdp)
    if page.get("danger_type") == "invalid_profile_or_404":
        danger = "invalid_profile_or_404"
    else:
        danger = check_circuit_breakers(cdp)
    if not danger:
        return None
    return {
        "error": True,
        "danger": danger,
        "reason": "invalid_profile_or_404" if danger == "invalid_profile_or_404" else danger,
        "page_type": page.get("page_type", "unknown"),
        "url": page.get("url", ""),
        "title": page.get("title", ""),
    }


def _activity_scroll_snapshot(cdp: CDPConnection) -> dict[str, Any]:
    raw = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            };
            const hasTime = (el) => /\\b\\d+\\s*(s|m|h|d|w|mo|yr|y)\\b|today|yesterday|ago/i.test(el.innerText || el.textContent || '');
            const strictCards = Array.from(document.querySelectorAll('.feed-shared-update-v2[data-urn*="activity"]'))
                .filter((el) => visible(el) && hasTime(el));
            const fallbackCards = strictCards.length ? [] : Array.from(document.querySelectorAll('.feed-shared-update-v2, [data-urn*="activity"]'))
                .filter((el) => visible(el) && hasTime(el));
            const cards = strictCards.length ? strictCards : fallbackCards;
            const loaders = Array.from(document.querySelectorAll(
                '.artdeco-loader, .artdeco-spinner, [aria-busy="true"], [class*="skeleton"], [class*="loading"]'
            )).filter(visible);
            const bodyText = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const emptyNodes = Array.from(document.querySelectorAll(
                '.scaffold-finite-scroll__empty, .artdeco-empty-state, [class*="empty-state"]'
            )).filter(visible);
            const emptySignals = emptyNodes
                .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 3);
            const explicitEmptyText = /nothing to see for now|no posts|no activity|hasn.t posted|nothing to show|no results/i;
            // "Couldn't load" is deliberately not an empty signal: it means the
            // page failed, not that the member has no activity.
            const emptyState = cards.length === 0 && (
                emptySignals.some((text) => explicitEmptyText.test(text)) ||
                explicitEmptyText.test(bodyText)
            );
            return JSON.stringify({
                scrollY: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
                scrollHeight: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
                viewportHeight: window.innerHeight || document.documentElement.clientHeight || 0,
                cardCount: cards.length,
                strictCardCount: strictCards.length,
                fallbackCardCount: fallbackCards.length,
                loading: loaders.length > 0,
                emptyState,
                emptySignals,
                url: window.location.href
            });
        })()
    """,
        timeout=8,
    )
    return (
        json.loads(raw)
        if raw
        else {
            "scrollY": 0,
            "scrollHeight": 0,
            "viewportHeight": 0,
            "cardCount": 0,
            "loading": False,
            "emptyState": False,
            "emptySignals": [],
            "url": "",
        }
    )


def _wait_for_activity_feed_state(
    cdp: CDPConnection,
    timeout: float = 12.0,
    poll_interval: float = 0.7,
) -> dict[str, Any]:
    """Wait for LinkedIn's client-rendered activity feed, not just the shell."""
    started = time.time()
    last: dict[str, Any] = {}
    while time.time() - started < max(0.5, timeout):
        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {
                "ready": False,
                "invalid": invalid,
                "elapsed_sec": round(time.time() - started, 2),
            }
        try:
            last = _activity_scroll_snapshot(cdp)
        except (TimeoutError, RuntimeError) as exc:
            last = {"error": str(exc)}
        if int(last.get("cardCount", 0) or 0) > 0:
            return {
                "ready": True,
                "reason": "activity_cards_visible",
                "snapshot": last,
                "elapsed_sec": round(time.time() - started, 2),
            }
        if last.get("emptyState") and not last.get("loading"):
            return {
                "ready": True,
                "reason": "explicit_empty_state",
                "snapshot": last,
                "elapsed_sec": round(time.time() - started, 2),
            }
        time.sleep(min(max(0.1, poll_interval), max(0.1, timeout - (time.time() - started))))
    return {
        "ready": False,
        "reason": "activity_feed_not_hydrated",
        "snapshot": last,
        "elapsed_sec": round(time.time() - started, 2),
    }
