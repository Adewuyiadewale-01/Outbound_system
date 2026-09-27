"""Profile notification toggle and notification-feed reader.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S16). Pure move.
"""

import json
import random
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def toggle_profile_notifications(
    cdp: CDPConnection, sim: HumanSimulator, enable: bool = True
) -> bool:
    """Toggle notification bell on current profile page. Returns True if successful."""
    # The notification bell is usually accessible via the "More" menu or directly on the profile
    success = cdp.evaluate("""
        (() => {
            // Look for the bell/notification button on profile
            const bellBtn = document.querySelector(
                'button[aria-label*="notification"], ' +
                'button[aria-label*="Notify me"], ' +
                'button[class*="notification"]'
            );
            if (bellBtn) {
                return JSON.stringify({found: true, selector: 'bell'});
            }

            // Try the "More" dropdown first
            const moreBtn = document.querySelector(
                'button[aria-label="More actions"], ' +
                'button[aria-label*="More"]'
            );
            if (moreBtn) {
                return JSON.stringify({found: true, selector: 'more_menu'});
            }

            return JSON.stringify({found: false});
        })()
    """)

    if not success:
        return False

    info = json.loads(success)
    if not info.get("found"):
        return False

    if info["selector"] == "bell":
        return sim.click_element(
            'button[aria-label*="notification"], button[aria-label*="Notify me"]'
        )
    elif info["selector"] == "more_menu":
        # Click More, then find notification option
        sim.click_element('button[aria-label="More actions"], button[aria-label*="More"]')
        human_delay(0.5, 1.5)
        # Look for notification option in dropdown
        return sim.click_element(
            '[class*="dropdown"] button[aria-label*="notification"], '
            '[class*="dropdown"] [data-control-name*="notification"]'
        )

    return False


def check_notifications(cdp: CDPConnection, sim: HumanSimulator) -> dict[str, Any]:
    """Navigate to notifications and extract recent items."""
    cdp.navigate("https://www.linkedin.com/notifications/")
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        return {"error": True, "danger": danger}

    # Scroll a bit
    sim.scroll(random.randint(200, 500))
    human_delay(2, 5)

    notif_data = cdp.evaluate("""
        (() => {
            const items = document.querySelectorAll(
                '.nt-card, [class*="notification-card"], .notification-list-item'
            );
            const notifications = [];
            items.forEach((item, i) => {
                if (i >= 10) return;
                notifications.push({
                    text: item.innerText.substring(0, 200).trim(),
                    index: i,
                });
            });
            return JSON.stringify({
                count: items.length,
                notifications: notifications,
            });
        })()
    """)

    return json.loads(notif_data) if notif_data else {"count": 0, "notifications": []}
