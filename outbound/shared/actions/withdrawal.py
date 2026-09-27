"""Withdraw a pending connection request from a prospect's profile.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S23). Pure move.
"""

import json
import random
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.quota import MAX_WITHDRAWALS_PER_DAY, get_counter, increment_counter


def withdraw_connection(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
) -> dict[str, Any]:
    """Withdraw a pending connection request from a prospect's profile.

    Sequence:
    1. Navigate to profile
    2. Verify the request is still pending
    3. Click "Pending" → "Withdraw"
    4. Confirm withdrawal

    Returns result dict with success status.
    """
    result = {
        "action": "withdraw_connection",
        "profile_url": profile_url,
        "success": False,
    }

    # Check daily withdrawal quota
    withdrawals_today = get_counter(state, "withdrawals")
    if withdrawals_today >= MAX_WITHDRAWALS_PER_DAY:
        result["error"] = "daily_withdrawal_limit"
        return result

    # Navigate to profile
    cdp.navigate(profile_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Brief natural scroll (don't go deep — just enough to look human)
    sim.scroll(random.randint(100, 300))
    human_delay(2, 5, distribution="gaussian")

    # Check if Pending button exists
    pending_info = cdp.evaluate("""
        (() => {
            const pendingBtn = document.querySelector(
                'button[aria-label*="Pending"], ' +
                'button[class*="pending"]'
            );
            if (pendingBtn) {
                return JSON.stringify({
                    found: true,
                    label: pendingBtn.getAttribute('aria-label') || '',
                });
            }

            // Check if already connected (no withdrawal needed)
            const msgBtn = document.querySelector('button[aria-label*="Message"]');
            const connectBtn = document.querySelector('button[aria-label*="Connect"]');
            if (msgBtn && !connectBtn) {
                return JSON.stringify({found: false, reason: 'already_connected'});
            }
            if (connectBtn) {
                return JSON.stringify({found: false, reason: 'not_pending'});
            }

            return JSON.stringify({found: false, reason: 'button_not_found'});
        })()
    """)

    info = json.loads(pending_info) if pending_info else {}

    if not info.get("found"):
        result["error"] = info.get("reason", "pending_button_not_found")
        return result

    # Click Pending button
    human_delay(1, 2, distribution="gaussian")
    clicked = sim.click_element('button[aria-label*="Pending"], button[class*="pending"]')

    if not clicked:
        result["error"] = "pending_click_failed"
        return result

    human_delay(0.5, 1.5)

    # Click "Withdraw" in the dropdown/modal
    withdraw_clicked = sim.click_element(
        'button[aria-label*="Withdraw"], '
        'button:has(span:contains("Withdraw")), '
        '[class*="dropdown"] li button, '
        'div[class*="artdeco-dropdown"] button'
    )

    if not withdraw_clicked:
        # Try alternative — sometimes it's a confirmation dialog
        human_delay(0.5, 1)
        withdraw_clicked = cdp.evaluate("""
            (() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText.trim().toLowerCase().includes('withdraw')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)

    if not withdraw_clicked:
        # Dismiss any open dropdown
        sim.click_element('button[aria-label="Dismiss"], button[aria-label="Close"]')
        result["error"] = "withdraw_button_not_found"
        return result

    human_delay(1, 2)

    # Verify withdrawal — button should no longer say "Pending"
    verify = cdp.evaluate("""
        (() => {
            const pending = document.querySelector(
                'button[aria-label*="Pending"]'
            );
            const connect = document.querySelector(
                'button[aria-label*="Connect"]'
            );
            return JSON.stringify({
                still_pending: !!pending,
                connect_available: !!connect,
            });
        })()
    """)

    verification = json.loads(verify) if verify else {}

    if verification.get("still_pending"):
        result["error"] = "withdrawal_may_have_failed"
        return result

    result["success"] = True
    result["connect_restored"] = verification.get("connect_available", False)
    increment_counter(state, "withdrawals")

    return result
