"""Acceptance-notifications reader.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S21). Pure move.
"""

import json
import random
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _navigate_with_readiness, _safe_scroll_or_js
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def check_acceptance_notifications(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
) -> dict[str, Any]:
    """Check the LinkedIn notifications page for connection acceptance events.

    More natural than checking My Network directly — humans check notifications.
    Looks for "accepted your invitation" / "accepted your connection request" entries.

    Returns list of accepted connections with profile URLs and names.

    Hardened path: uses readiness-gated navigation and safe scroll fallbacks
    for reliable unattended operation.
    """
    result: dict[str, Any] = {"action": "check_acceptance_notifications", "acceptances": []}

    # --- Navigate with readiness gate ---
    NOTIF_URL = "https://www.linkedin.com/notifications/"
    EXPECTED_SELECTOR = (
        '.nt-card, [class*="notification-card"], .notification-list-item, '
        '[class*="ntfctn"], li[class*="notification"]'
    )

    nav = _navigate_with_readiness(
        cdp,
        NOTIF_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            NOTIF_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Notifications page not ready: "
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

    # --- Scroll to load notifications (safe fallback) ---
    _safe_scroll_or_js(cdp, sim, random.randint(200, 500))
    human_delay(2, 4)

    # Sometimes scroll more (humans browse notifications)
    if random.random() < 0.4:
        _safe_scroll_or_js(cdp, sim, random.randint(200, 400))
        human_delay(1, 3)

    # --- Extract acceptance notifications (with retry) ---
    notif_data = _extract_acceptance_notifs_dom(cdp)
    if not notif_data:
        human_delay(2, 3)
        notif_data = _extract_acceptance_notifs_dom(cdp)

    acceptances = json.loads(notif_data) if notif_data else []
    result["acceptances"] = acceptances
    result["count"] = len(acceptances)

    # Surface the freshest notification matches separately for the runner,
    # but leave counting to the higher-level acceptance workflow.
    if acceptances:
        new_accepts = [
            a
            for a in acceptances
            if any(
                t in a.get("time_text", "")
                for t in [
                    "just now",
                    "today",
                    "hour",
                    "minute",
                    "1d",
                    "1 day",
                    "yesterday",
                    "2h",
                    "3h",
                    "4h",
                    "5h",
                ]
            )
            or a.get("index", 99) < 5
        ]
        if new_accepts:
            result["new_acceptances"] = new_accepts

    return result


def _extract_acceptance_notifs_dom(cdp: CDPConnection) -> str | None:
    """Extract acceptance notification data from the DOM via Runtime.evaluate.

    Factored out of check_acceptance_notifications to enable retry logic.
    """
    return cdp.evaluate("""
        (() => {
            const acceptances = [];
            const items = document.querySelectorAll(
                '.nt-card, [class*="notification-card"], .notification-list-item, ' +
                '[class*="ntfctn"], li[class*="notification"]'
            );
            items.forEach((item, i) => {
                if (i >= 30) return;
                const text = item.innerText.toLowerCase();

                // Check for acceptance language
                const isAcceptance = (
                    text.includes('accepted your invitation') ||
                    text.includes('accepted your connection') ||
                    text.includes('accepted your request') ||
                    text.includes('is now a connection') ||
                    text.includes('you are now connected')
                );

                if (isAcceptance) {
                    const linkEl = item.querySelector('a[href*="/in/"]');
                    const url = linkEl ? linkEl.href.split('?')[0] : '';

                    const nameEl = item.querySelector(
                        'strong, [class*="actor-name"], a[href*="/in/"]'
                    );
                    const name = nameEl ? nameEl.innerText.trim().split('\\n')[0] : '';

                    const timeEl = item.querySelector(
                        'time, [class*="time"], [class*="timestamp"]'
                    );
                    const timeText = timeEl ? timeEl.innerText.trim() : '';

                    if (url || name) {
                        acceptances.push({
                            url: url,
                            name: name,
                            time_text: timeText.toLowerCase(),
                            index: i,
                            notification_text: item.innerText.substring(0, 150).trim(),
                        });
                    }
                }
            });
            return JSON.stringify(acceptances);
        })()
    """)
