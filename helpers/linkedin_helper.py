#!/usr/bin/env python3
"""
LinkedIn automation helper — CDP-based browser control with human simulation.

Operates through a real Chrome profile via Chrome DevTools Protocol (port 18800).
Every action is designed to be indistinguishable from a human using LinkedIn.

Usage as library:
    from linkedin_helper import LinkedInSession
    session = LinkedInSession()
    session.connect()
    session.warm_up()
    ...
    session.cool_down()
    session.disconnect()

Usage as CLI:
    python3 linkedin_helper.py preflight
    python3 linkedin_helper.py warm-up
    python3 linkedin_helper.py view-profile --url <linkedin_url>
    python3 linkedin_helper.py read-feed --scrolls 5
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from outreach_helper import (
        count_outreach_log_connection_requests,
        count_pipeline_connected_leads,
    )
except Exception:  # pragma: no cover - keep LinkedIn-only helpers usable
    count_outreach_log_connection_requests = None
    count_pipeline_connected_leads = None

# --- linkedin_helper carve: package bootstrap (docs/CARVE-LINKEDIN-HELPER-PLAN.md) ---
_HELPERS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _HELPERS_DIR.parent
for _p in (str(_ROOT_DIR), str(_HELPERS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- linkedin_helper carve: shim re-exports (appended per slice) ---
from outbound.shared.acceptance.connections import _extract_connections_dom  # noqa: F401
from outbound.shared.acceptance.normalization import (  # noqa: F401
    _normalize_linkedin_profile_url,
    _normalize_sent_invitation_name,
)
from outbound.shared.acceptance.notifications import (  # noqa: F401
    _extract_acceptance_notifs_dom,
    check_acceptance_notifications,
)
from outbound.shared.acceptance.sent_scraper import (  # noqa: F401
    _extract_sent_invitations_dom,
    scrape_sent_invitations,
)
from outbound.shared.acceptance.subtractive import (  # noqa: F401
    check_acceptances,
    check_acceptances_subtractive,
    verify_acceptance_via_profile,
)
from outbound.shared.actions.engagement import (  # noqa: F401
    follow_engagement_trail,
    like_post,
    scan_reaction_list,
)
from outbound.shared.actions.prospecting import scan_prospect_box  # noqa: F401
from outbound.shared.actions.withdrawal import withdraw_connection  # noqa: F401
from outbound.shared.activity.feed_state import (  # noqa: F401
    _activity_invalid_result,
    _activity_scroll_snapshot,
    _wait_for_activity_destination,
    _wait_for_activity_feed_state,
)
from outbound.shared.activity.navigation import (  # noqa: F401
    _open_activity_tab,
    _open_profile_activity_from_profile,
    _try_open_profile_activity_from_profile,
)
from outbound.shared.activity.readers import (  # noqa: F401
    _extract_visible_activity_entries,
    _scroll_activity_with_lazy_patience,
    classify_activity_windows,
    read_activity_tab,
    read_activity_tabs_detail,
    relative_days_from_time_text,
)
from outbound.shared.activity.tab_policy import (  # noqa: F401
    ACTIVITY_ALLOWED_TAB_KEYS,
    ACTIVITY_DISALLOWED_PATH_RE,
    ACTIVITY_RANKING_TAB_ORDER,
    ACTIVITY_SELECTOR_RANKING_TAB_ORDER,
    ACTIVITY_TAB_ORDER,
)
from outbound.shared.activity.url_utils import (  # noqa: F401
    _activity_destination_matches,
    _activity_profile_slug,
    _activity_url_for_tab,
    _current_activity_url_is_disallowed,
    _page_still_loading,
    canonicalize_linkedin_profile_url,
)
from outbound.shared.browser.connection import CDP_HOST, CDP_PORT, CDPConnection  # noqa: F401
from outbound.shared.browser.readiness import (  # noqa: F401
    _navigate_with_readiness,
    _safe_scroll_or_js,
    _wait_for_linkedin_ready,
    _wait_for_page_ready,
)
from outbound.shared.browser.stealth import STEALTH_SCRIPTS, inject_stealth  # noqa: F401
from outbound.shared.browser.visibility import (  # noqa: F401
    get_visible_elements,
    is_element_visible,
)
from outbound.shared.danger.detection import (  # noqa: F401
    PageType,
    check_circuit_breakers,
    detect_page,
)
from outbound.shared.feed.post_types import (  # noqa: F401
    detect_post_type,
    execute_feed_scroll,
    generate_scroll_stop_sequence,
)
from outbound.shared.human.delays import human_delay, typing_delay  # noqa: F401
from outbound.shared.human.simulator import HumanSimulator  # noqa: F401
from outbound.shared.profile.diagnostics import (  # noqa: F401
    _safe_debug_slug,
    _write_profile_mapper_dump,
)
from outbound.shared.profile.mapper import (  # noqa: F401
    PROFILE_ACTION_READY_SELECTOR,
    PROFILE_MORE_CONNECT_SELECTOR,
    PROFILE_READY_SELECTOR,
    PROFILE_TOPCARD_SELECTOR,
    _open_more_and_click_connect,
    _open_more_and_find_connect,
    browse_profile_briefly,
    inspect_profile_action_state,
    view_profile,
)
from outbound.shared.profile.notif_toggle import (  # noqa: F401
    check_notifications,
    toggle_profile_notifications,
)
from outbound.shared.quota import (  # noqa: F401
    ACCEPTANCE_RATE_CRITICAL,
    ACCEPTANCE_RATE_WARN,
    DIAGNOSTIC_DIR,
    MAX_ACTIONS_PER_MINUTE,
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    MAX_MESSAGES_PER_DAY,
    MAX_PROFILE_VIEWS_PER_DAY,
    MAX_WITHDRAWALS_PER_DAY,
    STATE_DIR,
    STATE_FILE,
    WARMUP_SCHEDULE,
    _ensure_diagnostic_dir,
    _ensure_state_dir,
    _safe_slug,
    get_counter,
    get_today_key,
    get_week_key,
    get_weekly_counter,
    increment_counter,
    load_state,
    save_state,
)
from outbound.shared.send.diagnostics import _write_send_diagnostics  # noqa: F401
from outbound.shared.send.engine import (  # noqa: F401
    send_connection_only,
    send_connection_request,
    verify_no_note_send_ui,
)
from outbound.shared.send.modal import (  # noqa: F401
    _click_add_note_button,
    _click_connect_button,
    _click_send_button,
    _dismiss_connect_modal,
    _inspect_connect_modal,
    _type_connection_note,
    _wait_for_connect_modal,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# State file for daily quotas, session history, last scroll sequence, etc.

# Hard limits (non-overridable)

# Warmup schedule: week_number -> max daily conn_req

# Acceptance rate thresholds


# ---------------------------------------------------------------------------
# Delay utilities — all timing is randomized, never repeating patterns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# State persistence — tracks quotas, session history, scroll sequences
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CDP Connection Manager
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stealth patches — mask automation indicators
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Human simulation primitives
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Element visibility checker (honeypot guard)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Page detection — identify what LinkedIn page we're on
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Page-readiness gate — used by acceptance-check path for resilient navigation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Feed reader — content-aware scrolling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Profile viewer
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Activity tab reader
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Notification toggle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Notification checker
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session envelope — warm-up and cool-down
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Safety rails — pre-flight checks, quota enforcement, circuit breakers
# ---------------------------------------------------------------------------


def preflight_check(state: dict | None = None) -> dict[str, Any]:
    """Run pre-flight checks before any automation session.

    Returns dict with 'ok' boolean and details.
    """
    if state is None:
        state = load_state()

    results = {
        "ok": True,
        "checks": {},
    }

    # 1. Check Chrome CDP is responsive
    cdp = CDPConnection()
    health = cdp.health_check()
    results["checks"]["chrome_cdp"] = health
    if health["status"] != "ok":
        results["ok"] = False
        results["block_reason"] = f"Chrome CDP not responding on port {cdp.port}"
        return results

    # 2. Connect and check LinkedIn session
    try:
        cdp.connect()
        # Preflight must be quick and bounded.  A Chrome page-load event is
        # not reliable enough to gate the entire activity workflow: when it
        # never arrives, the old watcher could sit here for hours before it
        # ever opened a target profile.
        inject_stealth(cdp)
        page = detect_page(cdp, timeout=10)

        # CDP being reachable is not proof that LinkedIn is available. A newly
        # launched automation profile opens on Chrome's New Tab page, which
        # used to pass preflight and leave the first real workflow navigation
        # as the point of failure. Open LinkedIn deliberately and verify the
        # resulting page before allowing any workflow to proceed.
        # A profile reader can legitimately leave the active tab on LinkedIn's
        # 404 page after quarantining an invalid profile.  That is target-level
        # state, not evidence that the signed-in account or CDP lane is unsafe.
        # Re-establish a neutral LinkedIn page before applying the account-level
        # danger checks.  Login, checkpoint, captcha and restriction pages must
        # still block immediately and are intentionally not navigated away from.
        if page.get("page_type") in {"unknown", "not_found"}:
            cdp.navigate("https://www.linkedin.com/feed/", wait_load=False, timeout=8)
            time.sleep(2)
            page = detect_page(cdp, timeout=10)
        results["checks"]["linkedin_session"] = page

        if page.get("is_danger"):
            results["ok"] = False
            results["block_reason"] = f"Danger detected: {page.get('danger_type')}"
            cdp.disconnect()
            return results

        if page["page_type"] == "login":
            results["ok"] = False
            results["block_reason"] = "LinkedIn is logged out — manual login required"
            cdp.disconnect()
            return results

        if page.get("page_type") == "unknown":
            results["ok"] = False
            results["block_reason"] = (
                "LinkedIn session could not be verified after opening LinkedIn"
            )
            cdp.disconnect()
            return results

        cdp.disconnect()
    except Exception as e:
        results["ok"] = False
        results["block_reason"] = f"CDP connection failed: {e}"
        return results

    # 3. Check daily quotas
    conn_req_today = get_counter(state, "conn_req_sent")
    conn_req_week = get_weekly_counter(state, "conn_req_sent")
    profile_views_today = get_counter(state, "profile_views")

    results["checks"]["quotas"] = {
        "conn_req_today": conn_req_today,
        "conn_req_limit": MAX_CONN_REQ_PER_DAY,
        "conn_req_week": conn_req_week,
        "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
        "profile_views_today": profile_views_today,
        "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
    }

    if conn_req_today >= MAX_CONN_REQ_PER_DAY:
        results["checks"]["quotas"]["conn_req_exhausted"] = True
    if conn_req_week >= MAX_CONN_REQ_PER_WEEK:
        results["checks"]["quotas"]["conn_req_week_exhausted"] = True
        results["ok"] = False
        results["block_reason"] = "Weekly connection request limit reached"
    if profile_views_today >= MAX_PROFILE_VIEWS_PER_DAY:
        results["checks"]["quotas"]["profile_views_exhausted"] = True

    # 4. Check acceptance rate from sheet sources of truth.
    sheet_sent = {"ok": False, "source": "op_bruteforce_outreach_log"}
    if count_outreach_log_connection_requests is None:
        sheet_sent["error"] = "Outreach Log sent helper unavailable"
    else:
        try:
            sheet_sent = count_outreach_log_connection_requests()
        except Exception as e:
            sheet_sent["error"] = str(e)

    if not sheet_sent.get("ok"):
        results["ok"] = False
        results["block_reason"] = (
            "Unable to verify sent connection requests from Op Bruteforce Outreach Log: "
            f"{sheet_sent.get('error', 'unknown error')}"
        )
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": "op_bruteforce_pipeline",
            "sent": None,
            "accepted": None,
            "error": sheet_sent.get("error", "unknown error"),
        }
        return results

    sent_count = int(sheet_sent.get("sent", 0) or 0)
    if sent_count >= 10:  # Only check with sufficient sample
        pipeline_acceptance = {"ok": False, "source": "op_bruteforce_pipeline"}
        if count_pipeline_connected_leads is None:
            pipeline_acceptance["error"] = "Pipeline acceptance helper unavailable"
        else:
            try:
                pipeline_acceptance = count_pipeline_connected_leads()
            except Exception as e:
                pipeline_acceptance["error"] = str(e)

        if not pipeline_acceptance.get("ok"):
            results["ok"] = False
            results["block_reason"] = (
                "Unable to verify acceptance rate from Op Bruteforce Pipeline: "
                f"{pipeline_acceptance.get('error', 'unknown error')}"
            )
            results["checks"]["acceptance_rate"] = {
                "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
                "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
                "sent": sent_count,
                "accepted": None,
                "error": pipeline_acceptance.get("error", "unknown error"),
            }
            return results

        pipeline_connected = int(pipeline_acceptance.get("connected", 0) or 0)
        accepted_count = min(pipeline_connected, sent_count)
        acceptance_rate = accepted_count / sent_count
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
            "rate": round(acceptance_rate, 3),
            "sent": sent_count,
            "accepted": accepted_count,
            "outreach_log_sent": sent_count,
            "outreach_log_sent_rows": sheet_sent.get("sent_rows"),
            "pipeline_connected": pipeline_connected,
            "pipeline_connected_rows": pipeline_acceptance.get("connected_rows"),
        }
        if acceptance_rate < ACCEPTANCE_RATE_CRITICAL:
            results["ok"] = False
            results["block_reason"] = (
                f"Acceptance rate critically low ({acceptance_rate:.1%}). "
                "Connection requests paused until manual override."
            )
        elif acceptance_rate < ACCEPTANCE_RATE_WARN:
            results["checks"]["acceptance_rate"]["warning"] = True

    return results


# ---------------------------------------------------------------------------
# Phase 3: Connection request engine + engagement scanner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Engagement actions (Approaches A-E)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sent-invitations scraper & subtractive acceptance detection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Withdrawal manager — withdraw stale pending connection requests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session orchestrator — batch connection requests with interleaved engagement
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# LinkedInSession — high-level session manager
# ---------------------------------------------------------------------------


class LinkedInSession:
    """High-level LinkedIn automation session manager.

    Usage:
        session = LinkedInSession()
        session.connect()
        session.warm_up()
        # ... do work ...
        session.cool_down()
        session.disconnect()
    """

    def __init__(self):
        self.cdp = CDPConnection()
        self.sim: HumanSimulator | None = None
        self.state = load_state()
        self.connected = False
        self._action_times: list[float] = []  # timestamps of recent actions

    def connect(self, skip_rate_check: bool = False) -> dict[str, Any]:
        """Connect to Chrome CDP, inject stealth, run preflight.

        Args:
            skip_rate_check: If True, skip the acceptance rate gate in preflight.
                Use for read-only operations like acceptance checks that should
                run even when the acceptance rate is critically low.
        """
        # Preflight
        preflight = preflight_check(self.state)
        if not preflight["ok"]:
            # Read-only operations never send a connection request, so a rate
            # gate (including a temporary failure while reading its Sheets
            # sources) must not make the browser lane look unhealthy.  Keep
            # every other preflight blocker -- CDP, login, danger and quota --
            # fully enforced.
            rate_gate_failed = (
                "acceptance" in str(preflight.get("block_reason", "")).lower()
                or "outreach log" in str(preflight.get("block_reason", "")).lower()
            )
            if skip_rate_check and rate_gate_failed:
                preflight["ok"] = True
                preflight["acceptance_rate_skipped"] = True
            else:
                return preflight

        # Connect
        self.cdp.connect()
        # preflight_check already attached the stealth scripts to this same
        # page and to every future document. Re-applying them here creates a
        # second CDP evaluation phase before the first profile can open.
        self.sim = HumanSimulator(self.cdp)
        self.connected = True
        return {
            "ok": True,
            "status": "connected",
            **{k: v for k, v in preflight.items() if k.startswith("acceptance_rate")},
        }

    def disconnect(self):
        """Disconnect from Chrome CDP."""
        self.cdp.disconnect()
        self.connected = False
        self.sim = None

    def _enforce_rate_limit(self):
        """Ensure we don't exceed MAX_ACTIONS_PER_MINUTE."""
        now = time.time()
        # Clean old entries
        self._action_times = [t for t in self._action_times if now - t < 60]
        if len(self._action_times) >= MAX_ACTIONS_PER_MINUTE:
            wait_until = self._action_times[0] + 60
            if wait_until > now:
                time.sleep(wait_until - now + random.uniform(0.5, 2.0))
        self._action_times.append(time.time())

    def _check_danger(self) -> str | None:
        """Check for circuit breaker conditions."""
        return check_circuit_breakers(self.cdp)

    def warm_up(self) -> dict[str, Any]:
        """Execute session warm-up."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return session_warm_up(self.cdp, self.sim, self.state)

    def cool_down(self) -> dict[str, Any]:
        """Execute session cool-down."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return session_cool_down(self.cdp, self.sim)

    def view_profile(self, profile_url: str) -> dict[str, Any]:
        """View a profile with human-like behavior."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"error": True, "danger": danger}

        result = view_profile(self.cdp, self.sim, profile_url)
        increment_counter(self.state, "profile_views")
        return result

    def inspect_profile_action_state(self, profile_url: str) -> dict[str, Any]:
        """Inspect profile action state without heavy browsing."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        # A 404 belongs to the profile that was previously open.  It is not a
        # browser-wide condition and must not prevent us navigating to the
        # next profile in the frozen queue.
        if danger and danger != "invalid_profile_or_404":
            return {"state": "profile_unavailable", "error": danger, "profile_url": profile_url}
        result = inspect_profile_action_state(self.cdp, profile_url)
        increment_counter(self.state, "profile_views")
        return result

    def verify_no_note_send_ui(
        self, profile_url: str, profile_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify the no-note invite modal controls without sending."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"ok": False, "error": danger, "profile_url": profile_url}
        return verify_no_note_send_ui(self.cdp, self.sim, self.state, profile_url, profile_state)

    def read_activity(self, profile_url: str, max_seconds: float = 30.0) -> dict[str, Any]:
        """Read a profile's activity tab."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"error": True, "danger": danger}

        return read_activity_tab(self.cdp, self.sim, profile_url, max_seconds=max_seconds)

    def read_activity_detail(
        self,
        profile_url: str,
        max_seconds: float = 45.0,
        navigation_type: str = "direct_url",
        tab_order: list[tuple[str, str]] | None = None,
        minimum_direct_comments: int = 2,
        disable_early_stop: bool = False,
    ) -> dict[str, Any]:
        """Read profile activity tabs separately for ranking."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger and danger != "invalid_profile_or_404":
            return {"error": True, "danger": danger}

        return read_activity_tabs_detail(
            self.cdp,
            self.sim,
            profile_url,
            max_seconds=max_seconds,
            navigation_type=navigation_type,
            tab_order=tab_order,
            minimum_direct_comments=minimum_direct_comments,
            disable_early_stop=disable_early_stop,
        )

    def set_notifications(self, enable: bool = True) -> bool:
        """Toggle notifications for the current profile."""
        return toggle_profile_notifications(self.cdp, self.sim, enable)

    def read_feed(self, num_stops: int = 5) -> list[dict]:
        """Scroll the feed with content-aware behavior."""
        self._enforce_rate_limit()
        sequence = generate_scroll_stop_sequence(num_stops)
        return execute_feed_scroll(self.sim, sequence)

    def get_quotas(self) -> dict[str, Any]:
        """Get current quota status."""
        return {
            "conn_req_today": get_counter(self.state, "conn_req_sent"),
            "conn_req_limit": MAX_CONN_REQ_PER_DAY,
            "conn_req_week": get_weekly_counter(self.state, "conn_req_sent"),
            "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
            "profile_views_today": get_counter(self.state, "profile_views"),
            "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
        }

    # --- Phase 3 methods ---

    def send_connection(
        self,
        profile_url: str,
        note: str | None = None,
        enable_notifications: bool = False,
        profile_data: dict[str, Any] | None = None,
        activity_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a connection request to a profile."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return send_connection_request(
            self.cdp,
            self.sim,
            self.state,
            profile_url,
            note,
            enable_notifications,
            profile_data,
            activity_data,
        )

    def send_connection_only(
        self,
        profile_url: str,
        profile_state: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a connection request without reading activity internally."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return send_connection_only(
            self.cdp, self.sim, self.state, profile_url, profile_state, note
        )

    def like(self, post_url: str | None = None) -> dict[str, Any]:
        """Like a post (Approach E)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return like_post(self.cdp, self.sim, self.state, post_url)

    def engagement_trail(
        self,
        post_url: str,
        max_profiles: int = 3,
    ) -> dict[str, Any]:
        """Follow engagement trail on a post (Approach B)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return follow_engagement_trail(
            self.cdp,
            self.sim,
            self.state,
            post_url,
            max_profiles,
        )

    def reaction_scan(self, post_url: str) -> dict[str, Any]:
        """Scan reaction list on a post (Approach C)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return scan_reaction_list(self.cdp, self.sim, post_url)

    def check_accepts(self) -> dict[str, Any]:
        """Check for new connection acceptances (legacy: connections page scan)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptances(self.cdp, self.sim, self.state)

    def check_accepts_subtractive(self, pending_prospects: list[dict[str, Any]]) -> dict[str, Any]:
        """Check for acceptances by comparing pending prospects against sent invitations.

        Subtractive strategy: anyone pending in sheet but missing from sent page
        has had a status change. Verifies via profile visit.
        """
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptances_subtractive(
            self.cdp,
            self.sim,
            self.state,
            pending_prospects,
        )

    def prospect_box(self) -> dict[str, Any]:
        """Scan 'People you may know' suggestions (Approach D)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return scan_prospect_box(self.cdp, self.sim)

    def batch_session(
        self,
        prospects: list[dict[str, Any]],
        burst_size: tuple[int, int] = (3, 6),
        engagement_approaches: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a full batch session with interleaved engagement."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return run_session(
            self.cdp,
            self.sim,
            self.state,
            prospects,
            burst_size,
            engagement_approaches,
        )

    def withdraw(self, profile_url: str) -> dict[str, Any]:
        """Withdraw a pending connection request."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return withdraw_connection(self.cdp, self.sim, self.state, profile_url)

    def check_acceptance_notifs(self) -> dict[str, Any]:
        """Check notifications page for connection acceptances."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptance_notifications(self.cdp, self.sim, self.state)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LinkedIn automation helper")
    sub = parser.add_subparsers(dest="command")

    # preflight
    sub.add_parser("preflight", help="Run pre-flight checks")

    # health
    sub.add_parser("health", help="Check Chrome CDP health")

    # warm-up
    sub.add_parser("warm-up", help="Execute session warm-up")

    # cool-down
    sub.add_parser("cool-down", help="Execute session cool-down")

    # view-profile
    vp = sub.add_parser("view-profile", help="View a LinkedIn profile")
    vp.add_argument("--url", required=True, help="LinkedIn profile URL")

    # read-activity
    ra = sub.add_parser("read-activity", help="Read profile activity tab")
    ra.add_argument("--url", required=True, help="LinkedIn profile URL")

    # read-feed
    rf = sub.add_parser("read-feed", help="Scroll and read the feed")
    rf.add_argument("--stops", type=int, default=5, help="Number of scroll stops")

    # detect-page
    sub.add_parser("detect-page", help="Detect current LinkedIn page type")

    # quotas
    sub.add_parser("quotas", help="Show current quota status")

    # --- Phase 3 CLI commands ---

    # send-connection
    sc = sub.add_parser("send-connection", help="Send a connection request")
    sc.add_argument("--url", required=True, help="LinkedIn profile URL")
    sc.add_argument("--note", default=None, help="Personalized note (max 300 chars)")
    sc.add_argument("--notify", action="store_true", help="Enable notifications if active")

    vm = sub.add_parser(
        "verify-connection-modal", help="Open and dismiss the connection modal without sending"
    )
    vm.add_argument("--url", required=True, help="LinkedIn profile URL")

    # like-post
    lp = sub.add_parser("like-post", help="Like a post")
    lp.add_argument("--url", default=None, help="Post URL (or like from current viewport)")

    # engagement-trail
    et = sub.add_parser("engagement-trail", help="Follow engagement trail on a post")
    et.add_argument("--url", required=True, help="Post URL")
    et.add_argument("--max-profiles", type=int, default=3, help="Max profiles to visit")

    # reaction-scan
    rs = sub.add_parser("reaction-scan", help="Scan reaction list on a post")
    rs.add_argument("--url", required=True, help="Post URL")

    # check-acceptances
    sub.add_parser("check-acceptances", help="Check for new connection acceptances")

    # prospect-box
    sub.add_parser("prospect-box", help="Scan People You May Know suggestions")

    # withdraw
    wd = sub.add_parser("withdraw", help="Withdraw a pending connection request")
    wd.add_argument("--url", required=True, help="LinkedIn profile URL")

    # check-acceptance-notifs
    sub.add_parser("check-acceptance-notifs", help="Check notifications for connection acceptances")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "health":
        cdp = CDPConnection()
        result = cdp.health_check()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "ok" else 1)

    if args.command == "preflight":
        result = preflight_check()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 1)

    if args.command == "quotas":
        state = load_state()
        quotas = {
            "conn_req_today": get_counter(state, "conn_req_sent"),
            "conn_req_limit": MAX_CONN_REQ_PER_DAY,
            "conn_req_week": get_weekly_counter(state, "conn_req_sent"),
            "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
            "profile_views_today": get_counter(state, "profile_views"),
            "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
        }
        print(json.dumps(quotas, indent=2))
        sys.exit(0)

    # Commands that need a full session
    session = LinkedInSession()
    connect_result = session.connect()
    if not connect_result.get("ok"):
        print(json.dumps(connect_result, indent=2))
        sys.exit(1)

    try:
        if args.command == "warm-up":
            result = session.warm_up()
        elif args.command == "cool-down":
            result = session.cool_down()
        elif args.command == "view-profile":
            result = session.view_profile(args.url)
        elif args.command == "read-activity":
            result = session.read_activity(args.url)
        elif args.command == "read-feed":
            sequence = generate_scroll_stop_sequence(args.stops)
            result = execute_feed_scroll(session.sim, sequence)
        elif args.command == "detect-page":
            result = detect_page(session.cdp)
        elif args.command == "send-connection":
            result = session.send_connection(
                args.url,
                note=args.note,
                enable_notifications=args.notify,
            )
        elif args.command == "verify-connection-modal":
            profile_state = session.inspect_profile_action_state(args.url)
            result = session.verify_no_note_send_ui(args.url, profile_state)
            result["profile_state"] = profile_state
        elif args.command == "like-post":
            result = session.like(args.url)
        elif args.command == "engagement-trail":
            result = session.engagement_trail(args.url, args.max_profiles)
        elif args.command == "reaction-scan":
            result = session.reaction_scan(args.url)
        elif args.command == "check-acceptances":
            result = session.check_accepts()
        elif args.command == "prospect-box":
            result = session.prospect_box()
        elif args.command == "withdraw":
            result = session.withdraw(args.url)
        elif args.command == "check-acceptance-notifs":
            result = session.check_acceptance_notifs()
        else:
            result = {"error": f"Unknown command: {args.command}"}

        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        session.disconnect()


if __name__ == "__main__":
    main()
