"""Session envelope — warm-up and cool-down routines.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S24). Pure move.
"""

import random
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.feed.post_types import execute_feed_scroll, generate_scroll_stop_sequence
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.notif_toggle import check_notifications
from outbound.shared.quota import save_state


def session_warm_up(cdp: CDPConnection, sim: HumanSimulator, state: dict) -> dict[str, Any]:
    """Execute the session warm-up routine.

    1. Navigate to feed
    2. Content-aware scroll with randomized stop sequence
    3. Maybe check notifications
    4. Return summary of warm-up
    """
    # Navigate to feed
    cdp.navigate("https://www.linkedin.com/feed/")
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        return {"error": True, "danger": danger, "phase": "warm_up"}

    # Generate scroll-stop sequence (unique per session)
    num_stops = random.randint(3, 6)
    sequence = generate_scroll_stop_sequence(num_stops)

    # Record the sequence to state to ensure we don't repeat it
    last_sequence = state.get("last_scroll_sequence", [])
    # Regenerate if somehow identical to yesterday's (unlikely but safe)
    attempts = 0
    while sequence == last_sequence and attempts < 5:
        sequence = generate_scroll_stop_sequence(num_stops)
        attempts += 1

    state["last_scroll_sequence"] = sequence
    save_state(state)

    # Execute feed scroll
    observed = execute_feed_scroll(sim, sequence)

    # Maybe check notifications (30-50% chance)
    checked_notifications = False
    if random.random() < random.uniform(0.3, 0.5):
        check_notifications(cdp, sim)
        checked_notifications = True
        human_delay(2, 5)
        # Navigate back to feed
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(1, 3)

    return {
        "phase": "warm_up",
        "scroll_stops": len(sequence),
        "posts_observed": len(observed),
        "post_types": [p["type"] for p in observed],
        "checked_notifications": checked_notifications,
    }


def session_cool_down(cdp: CDPConnection, sim: HumanSimulator) -> dict[str, Any]:
    """Execute the session cool-down routine."""
    # Return to feed or messaging (randomized)
    if random.random() < 0.7:
        cdp.navigate("https://www.linkedin.com/feed/")
    else:
        cdp.navigate("https://www.linkedin.com/messaging/")
    human_delay(1, 3)

    # Brief scroll or check notification
    if random.random() < 0.5:
        sim.scroll(random.randint(200, 500))
        human_delay(2, 5)
    else:
        # Quick glance at messaging or feed
        human_delay(3, 8)

    # Randomized idle period (never fixed)
    idle_time = human_delay(45, 180)

    return {
        "phase": "cool_down",
        "idle_seconds": round(idle_time, 1),
    }
