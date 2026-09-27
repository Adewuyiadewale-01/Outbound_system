"""Subtractive acceptance detection (sent page vs profile verification).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S21). Pure move.
"""

import json
import random
from datetime import date, datetime
from typing import Any

from outbound.shared.acceptance.connections import _extract_connections_dom
from outbound.shared.acceptance.normalization import (
    _normalize_linkedin_profile_url,
    _normalize_sent_invitation_name,
)
from outbound.shared.acceptance.sent_scraper import scrape_sent_invitations
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _navigate_with_readiness, _safe_scroll_or_js
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.mapper import inspect_profile_action_state


def verify_acceptance_via_profile(
    cdp: CDPConnection,
    sim: HumanSimulator,
    profile_url: str,
) -> dict[str, Any]:
    """Visit a profile to verify whether a connection was accepted, declined, or expired.

    Uses inspect_profile_action_state which is already part of view_profile.
    Returns:
    - status: 'accepted', 'declined_or_expired', 'still_pending', 'unknown'
    - action_state: raw state from inspect_profile_action_state
    """
    result: dict[str, Any] = {
        "action": "verify_acceptance",
        "profile_url": profile_url,
        "action_state": "",
        "profile_name": "",
    }

    nav = _navigate_with_readiness(
        cdp,
        profile_url,
        nav_timeout=30,
        ready_timeout=15,
    )

    if not nav.get("ready"):
        result["status"] = "unknown"
        result["error"] = (
            f"page_not_ready|readyState={nav.get('readyState', '?')}"
            f"|overlay={nav.get('overlay_detected', '?')}"
            f"|loading={nav.get('loading_detected', '?')}"
            f"|timeout={nav.get('timeout', '?')}"
            f"|url={nav.get('url', '?')}"
        )
        return result

    human_delay(2, 3)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["status"] = "unknown"
        result["error"] = f"circuit_breaker:{danger}"
        return result

    inspected = inspect_profile_action_state(cdp, profile_url)
    action_state = inspected.get("state", "unknown")
    result["action_state"] = action_state
    result["profile_name"] = inspected.get("profileName", "")

    if action_state == "already_connected":
        result["status"] = "accepted"
    elif action_state == "already_pending":
        result["status"] = "still_pending"
    elif action_state in ("connect_direct", "connect_in_more"):
        result["status"] = "declined_or_expired"
    elif action_state == "no_connect_button":
        # Profile visible but no connect/pending button — likely accepted (1st degree)
        # or profile has restricted connection options
        result["status"] = "accepted"
        result["note"] = "inferred_from_no_connect_button"
    elif action_state == "profile_unavailable":
        result["status"] = "declined_or_expired"
        result["note"] = "profile_unavailable"
    else:
        result["status"] = "unknown"

    # Brief natural scroll to look human
    _safe_scroll_or_js(cdp, sim, random.randint(100, 250))
    human_delay(1, 2)

    return result


def check_acceptances_subtractive(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    pending_prospects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect new acceptances by comparing pending prospects against sent invitations.

    Subtractive strategy:
    1. Scrape sent invitations page for all currently pending invites
    2. For each prospect in pending_prospects, check if they're still on the sent page
    3. If missing from sent page, visit their profile to verify acceptance vs decline
    4. Return categorized results

    Args:
        pending_prospects: list of prospect dicts from the sheet, each must have
            'contact_linkedin' (URL) and 'contact_name' fields.

    Returns dict with acceptances, declines, still_pending, and verification details.
    """
    result: dict[str, Any] = {
        "action": "check_acceptances_subtractive",
        "acceptances": [],
        "declines": [],
        "still_pending": [],
        "errors": [],
        "sent_invitations_count": 0,
    }

    if not pending_prospects:
        result["status"] = "no_pending_prospects"
        return result

    # Step 1: Scrape sent invitations page
    sent = scrape_sent_invitations(cdp, sim)
    if sent.get("error"):
        result["error"] = sent["error"]
        return result

    sent_urls = set()
    sent_names: dict[str, list[dict[str, Any]]] = {}
    for inv in sent.get("invitations", []):
        url = _normalize_linkedin_profile_url(inv.get("url", ""))
        if url:
            sent_urls.add(url)
        name_key = _normalize_sent_invitation_name(inv.get("name", ""))
        if name_key:
            sent_names.setdefault(name_key, []).append(inv)

    result["sent_invitations_count"] = sent.get("count", len(sent.get("invitations", [])))
    result["sent_invitations_url_count"] = len(sent_urls)
    result["sent_invitations"] = sent.get("invitations", [])
    result["sent_invitations_scroll_snapshots"] = sent.get("scroll_snapshots", [])

    # Step 2: Compare each pending prospect against sent page
    missing_from_sent: list[dict[str, Any]] = []
    for prospect in pending_prospects:
        prospect_url = _normalize_linkedin_profile_url(prospect.get("contact_linkedin") or "")
        prospect_name_key = _normalize_sent_invitation_name(prospect.get("contact_name", ""))
        if not prospect_url:
            result["errors"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "error": "no_linkedin_url_in_sheet",
                }
            )
            continue

        if prospect_url in sent_urls:
            result["still_pending"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "url": prospect_url,
                    "status": "still_on_sent_page",
                }
            )
        elif prospect_name_key and len(sent_names.get(prospect_name_key, [])) == 1:
            result["still_pending"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "url": prospect_url,
                    "status": "still_on_sent_page_name_match",
                    "sent_page_name": sent_names[prospect_name_key][0].get("name", ""),
                }
            )
        else:
            missing_from_sent.append(prospect)

    # Step 3: Verify missing prospects via profile visit
    result["missing_from_sent_count"] = len(missing_from_sent)
    for prospect in missing_from_sent:
        prospect_url = (prospect.get("contact_linkedin") or "").strip()
        contact_name = prospect.get("contact_name", "")
        human_delay(2, 4)  # Pace between profile visits

        verification = None
        last_error = None
        for attempt in range(2):  # Retry once on unknown/error
            try:
                verification = verify_acceptance_via_profile(cdp, sim, prospect_url)
            except Exception as exc:
                last_error = str(exc)
                verification = None
                if attempt == 0:
                    human_delay(3, 5)
                continue

            status = verification.get("status", "unknown")
            if status != "unknown":
                break  # Got a definitive answer
            # First attempt returned unknown — retry after a pause
            if attempt == 0:
                human_delay(3, 5)

        if verification is None:
            result["errors"].append(
                {
                    "contact_name": contact_name,
                    "url": prospect_url,
                    "error": last_error or "verification_failed_after_retries",
                }
            )
            continue

        status = verification.get("status", "unknown")
        entry = {
            "name": verification.get("profile_name") or contact_name,
            "url": prospect_url.rstrip("/").lower().split("?")[0],
            "source": "sent_page_subtractive",
            "verification": status,
            "action_state": verification.get("action_state", ""),
        }

        if status == "accepted":
            result["acceptances"].append(entry)
        elif status == "declined_or_expired":
            result["declines"].append(entry)
        elif status == "still_pending":
            # Was missing from sent page but profile says pending — possible pagination miss
            result["still_pending"].append(
                {
                    **entry,
                    "note": "missing_from_sent_but_profile_shows_pending",
                }
            )
        else:
            verify_error = verification.get("error", "")
            result["errors"].append(
                {
                    **entry,
                    "error": f"unknown_status_after_retry:{status}|detail={verify_error}",
                }
            )

    return result


def check_acceptances(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
) -> dict[str, Any]:
    """Scan My Network for new connection acceptances.

    Used in the 10:00 AM acceptance check session.
    Returns list of newly accepted connections.

    Hardened path: uses JS navigation with readiness gates and automatic
    retry/fallback so this works reliably in unattended/background-window
    scenarios. Human-sim mouse events are attempted but not required.
    """
    result: dict[str, Any] = {"action": "check_acceptances", "acceptances": []}

    # --- Navigate with readiness gate ---
    CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
    # Selector covers current (LazyColumn) and legacy LinkedIn connection layouts
    EXPECTED_SELECTOR = (
        '[data-component-type="LazyColumn"], .mn-connection-card, [class*="connection-card"]'
    )

    nav = _navigate_with_readiness(
        cdp,
        CONNECTIONS_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        # One retry: sometimes LinkedIn redirects through an interstitial
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            CONNECTIONS_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Page not ready after navigation: "
            f"readyState={nav.get('readyState')}, "
            f"overlay={nav.get('overlay_detected')}, "
            f"selector_found={nav.get('selector_found')}"
        )
        return result

    human_delay(2, 4)

    # --- Circuit breaker check ---
    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # --- Scroll to trigger lazy-loading (reliability > human-likeness) ---
    scroll_info = _safe_scroll_or_js(cdp, sim, random.randint(200, 500))
    result["scroll"] = scroll_info
    human_delay(2, 4)

    # --- Extract recent connections (with retry) ---
    conn_data = _extract_connections_dom(cdp)
    if not conn_data:
        # Retry once after a short wait — page may still be hydrating
        human_delay(2, 3)
        conn_data = _extract_connections_dom(cdp)

    connections = json.loads(conn_data) if conn_data else []

    # Filter for recent connections (today / yesterday)
    today = date.today()
    recent = []
    for conn in connections:
        time_text = conn.get("time_text", "")
        is_recent = any(
            t in time_text
            for t in [
                "just now",
                "today",
                "hour",
                "minute",
                "1 day",
                "yesterday",
                "1d",
                "2d",
                "1h",
                "2h",
                "3h",
            ]
        )

        # Also match "connected on <date>" format (e.g. "connected on april 27, 2026")
        if not is_recent and "connected on" in time_text:
            # Try to parse the date from the text
            try:
                date_part = time_text.split("connected on")[-1].strip().rstrip(".")
                # Handle formats like "april 27, 2026"
                conn_date = datetime.strptime(date_part, "%B %d, %Y").date()
                days_ago = (today - conn_date).days
                if days_ago <= 2:  # Today or yesterday (with buffer)
                    is_recent = True
                    conn["_parsed_days_ago"] = days_ago
            except (ValueError, TypeError):
                pass  # Unparseable date — fall through to index check

        if is_recent or conn["index"] < 5:  # Top 5 are newest
            recent.append(conn)

    result["acceptances"] = recent
    result["total_connections_visible"] = len(connections)

    return result
