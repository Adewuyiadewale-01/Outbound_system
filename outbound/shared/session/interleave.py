"""Legacy batch orchestrator (run_session + interleave).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S26). Pure move.
"""

import random
from datetime import datetime
from typing import Any

from outbound.shared.actions.engagement import like_post
from outbound.shared.actions.prospecting import scan_prospect_box
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.feed.post_types import execute_feed_scroll, generate_scroll_stop_sequence
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.quota import (
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    get_counter,
    get_weekly_counter,
)
from outbound.shared.send.engine import send_connection_request
from outbound.shared.session.envelope import session_cool_down, session_warm_up


def run_session(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    prospects: list[dict[str, Any]],
    burst_size: tuple[int, int] = (3, 6),
    engagement_approaches: list[str] | None = None,
) -> dict[str, Any]:
    """Run a full connection request session with interleaved engagement.

    This is the main orchestrator that combines:
    1. Warm-up (feed scroll)
    2. Bursts of connection requests (burst_size range per burst)
    3. Between-burst interleave activities (engagement approaches)
    4. Cool-down

    The agent provides:
    - prospects: list of prospect dicts from outreach_helper.load_prospect_queue()
    - engagement_approaches: list of approach letters to use (e.g., ["A", "E"])
      A = built-in (Activity tab on every profile, already mandatory)
      B = follow_engagement_trail (visit likers/commenters)
      C = scan_reaction_list (mine reactions)
      D = scan_prospect_box (PYMK suggestions)
      E = like_post (feed engagement filler)

    Returns detailed session report.
    """
    if engagement_approaches is None:
        engagement_approaches = ["E"]  # Default to feed likes as filler

    session_report = {
        "action": "run_session",
        "started_at": datetime.now().isoformat(),
        "conn_requests": [],
        "engagement_actions": [],
        "errors": [],
        "warm_up": None,
        "cool_down": None,
    }

    # --- Warm-up ---
    warm_up_result = session_warm_up(cdp, sim, state)
    session_report["warm_up"] = warm_up_result
    if warm_up_result.get("error"):
        session_report["aborted"] = True
        session_report["abort_reason"] = warm_up_result.get("danger", "warm_up_failed")
        return session_report

    # --- Process prospects in bursts ---
    prospect_idx = 0
    burst_count = 0

    while prospect_idx < len(prospects):
        # Determine burst size (randomized within range)
        this_burst = random.randint(burst_size[0], burst_size[1])
        burst_prospects = prospects[prospect_idx : prospect_idx + this_burst]
        prospect_idx += this_burst
        burst_count += 1

        # Execute burst
        for prospect in burst_prospects:
            # Check quotas before each request
            if get_counter(state, "conn_req_sent") >= MAX_CONN_REQ_PER_DAY:
                session_report["stopped_reason"] = "daily_limit_reached"
                break
            if get_weekly_counter(state, "conn_req_sent") >= MAX_CONN_REQ_PER_WEEK:
                session_report["stopped_reason"] = "weekly_limit_reached"
                break

            # Inter-request delay (randomized, never identical)
            if session_report["conn_requests"]:
                human_delay(30, 90, distribution="log_normal")

            # Send the connection request
            cr_result = send_connection_request(
                cdp,
                sim,
                state,
                profile_url=prospect.get("contact_linkedin", ""),
                note=prospect.get("_note"),
                enable_notifications=prospect.get("_enable_notifications", False),
            )
            cr_result["prospect_id"] = prospect.get("id", "")
            cr_result["company"] = prospect.get("company", "")
            cr_result["contact_name"] = prospect.get("contact_name", "")
            session_report["conn_requests"].append(cr_result)

            if cr_result.get("error") in ("daily_conn_req_limit", "weekly_conn_req_limit"):
                session_report["stopped_reason"] = cr_result["error"]
                break

            # Circuit breaker from connection request
            if cr_result.get("error") in (
                "captcha",
                "restriction",
                "email_verify",
                "robot_check",
                "login",
            ):
                session_report["aborted"] = True
                session_report["abort_reason"] = cr_result["error"]
                return session_report

        # Check if we should stop
        if session_report.get("stopped_reason") or session_report.get("aborted"):
            break

        # --- Between-burst interleave ---
        if prospect_idx < len(prospects):
            interleave_result = _execute_interleave(cdp, sim, state, engagement_approaches)
            session_report["engagement_actions"].append(interleave_result)

    # --- Cool-down ---
    human_delay(5, 15)
    cool_down_result = session_cool_down(cdp, sim)
    session_report["cool_down"] = cool_down_result

    # --- Summary ---
    successful = sum(1 for cr in session_report["conn_requests"] if cr.get("success"))
    session_report["summary"] = {
        "total_attempted": len(session_report["conn_requests"]),
        "successful": successful,
        "failed": len(session_report["conn_requests"]) - successful,
        "bursts": burst_count,
        "engagement_actions": len(session_report["engagement_actions"]),
        "ended_at": datetime.now().isoformat(),
    }

    return session_report


def _execute_interleave(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    approaches: list[str],
) -> dict[str, Any]:
    """Execute a random interleave activity between connection request bursts.

    Picks a random approach from the provided list and executes it.
    """
    approach = random.choice(approaches)
    result = {"approach": approach}

    human_delay(5, 20, distribution="gaussian")

    if approach == "E":
        # Feed engagement — scroll feed and like a post
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)

        # Scroll a few times
        for _ in range(random.randint(2, 5)):
            sim.scroll(random.randint(300, 600))
            human_delay(1, 4)

        # Like a visible post
        like_result = like_post(cdp, sim, state)
        result["like"] = like_result

    elif approach == "B":
        # Engagement trail — needs a post URL (agent provides via prospect activity)
        # Since we don't have a URL here, do a feed scroll instead as fallback
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)
        scroll_seq = generate_scroll_stop_sequence(random.randint(2, 4))
        observed = execute_feed_scroll(sim, scroll_seq)
        result["feed_scroll"] = {"posts_observed": len(observed)}

    elif approach == "D":
        # Prospect box scan
        scan_result = scan_prospect_box(cdp, sim)
        result["suggestions"] = scan_result

    else:
        # Default: brief feed scroll
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)
        sim.scroll(random.randint(300, 800))
        human_delay(3, 8)

    return result
