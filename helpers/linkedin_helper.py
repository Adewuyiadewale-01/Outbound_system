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
import re
import sys
import time
from datetime import date, datetime
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


def detect_post_type(cdp: CDPConnection) -> str:
    """Detect the type of post currently visible in the viewport center."""
    result = cdp.evaluate("""
        (() => {
            const vh = window.innerHeight;
            const centerY = vh / 2;

            // Find the feed post element nearest to viewport center
            const posts = document.querySelectorAll(
                '.feed-shared-update-v2, .occludable-update, [data-urn*="activity"]'
            );

            let closest = null;
            let closestDist = Infinity;
            posts.forEach(post => {
                const rect = post.getBoundingClientRect();
                const postCenter = rect.top + rect.height / 2;
                const dist = Math.abs(postCenter - centerY);
                if (dist < closestDist && rect.top < vh && rect.bottom > 0) {
                    closest = post;
                    closestDist = dist;
                }
            });

            if (!closest) return 'none';

            // Detect post type
            const hasCarousel = !!(
                closest.querySelector('.feed-shared-carousel') ||
                closest.querySelector('[class*="carousel"]') ||
                closest.querySelector('.feed-shared-document')
            );
            const hasVideo = !!(
                closest.querySelector('video') ||
                closest.querySelector('.feed-shared-linkedin-video') ||
                closest.querySelector('[class*="video-player"]')
            );
            const hasImage = !!(
                closest.querySelector('.feed-shared-image') ||
                closest.querySelector('img.feed-shared-image__image')
            );

            // Check text length for long-form detection
            const textEl = closest.querySelector(
                '.feed-shared-text, .feed-shared-update-v2__description, .break-words'
            );
            const textLen = textEl ? textEl.innerText.length : 0;

            if (hasCarousel) return 'carousel';
            if (hasVideo) return 'video';
            if (textLen > 500) return 'long_form';
            if (hasImage) return 'image';
            if (textLen > 0) return 'short_text';
            return 'unknown';
        })()
    """)
    return result or "unknown"


def generate_scroll_stop_sequence(num_stops: int = 5) -> list[dict]:
    """Generate a unique scroll-stop sequence for this session.

    Returns a list of dicts like:
    [{"scrolls_before_pause": 3, "pause_type": "read"}, ...]
    """
    sequence = []
    for _ in range(num_stops):
        sequence.append(
            {
                "scrolls_before_pause": random.randint(1, 6),
                "pause_type": random.choice(["read", "skim", "linger"]),
            }
        )
    return sequence


def execute_feed_scroll(sim: HumanSimulator, scroll_stop_sequence: list[dict]) -> list[dict]:
    """Execute a feed scroll session following the generated sequence.

    Returns list of observed posts with their types.
    """
    observed_posts = []
    total_scrolls = 0

    for stop in scroll_stop_sequence:
        # Scroll the specified number of times before pausing
        for _ in range(stop["scrolls_before_pause"]):
            scroll_amount = random.randint(300, 600)
            sim.scroll(scroll_amount)
            human_delay(0.5, 1.5)
            total_scrolls += 1

        # Detect what post is in view and react appropriately
        post_type = detect_post_type(sim.cdp)
        observed_posts.append({"type": post_type, "scroll_position": total_scrolls})

        # Content-aware pause
        if post_type == "long_form":
            human_delay(8, 20)
            # Slow mid-read scrolls
            for _ in range(random.randint(1, 2)):
                sim.scroll(random.randint(100, 200))
                human_delay(2, 5)
        elif post_type == "carousel":
            # Scroll through slides
            for _ in range(random.randint(2, 3)):
                sim.scroll(random.randint(80, 150))
                human_delay(1.5, 4)
            # Sometimes skip the last slide
            if random.random() < 0.3:
                sim.scroll(random.randint(200, 400))
        elif post_type == "video":
            human_delay(3, 8)
            # Occasionally linger longer on video
            if random.random() < 0.2:
                human_delay(5, 15)
        elif post_type == "image":
            human_delay(3, 8)
        elif post_type == "short_text":
            human_delay(2, 5)
        else:
            human_delay(1, 3)

        # Between scroll groups
        human_delay(1, 4)

    return observed_posts


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


def send_connection_request(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
    note: str | None = None,
    enable_notifications: bool = False,
    profile_data: dict[str, Any] | None = None,
    activity_data: dict[str, Any] | None = None,
    verify_connection_modal_only: bool = False,
) -> dict[str, Any]:
    """Send a connection request only; activity timing is owned by the runner."""
    result = {
        "action": "send_connection_request",
        "profile_url": profile_url,
        "success": False,
    }
    stage = "start"

    try:
        # --- Quota gate ---
        conn_today = get_counter(state, "conn_req_sent")
        conn_week = get_weekly_counter(state, "conn_req_sent")
        views_today = get_counter(state, "profile_views")

        if conn_today >= MAX_CONN_REQ_PER_DAY:
            result["error"] = "daily_conn_req_limit"
            return result
        if conn_week >= MAX_CONN_REQ_PER_WEEK:
            result["error"] = "weekly_conn_req_limit"
            return result
        if views_today >= MAX_PROFILE_VIEWS_PER_DAY:
            result["error"] = "daily_profile_view_limit"
            return result

        # --- Step 1: Inspect profile action state ---
        stage = "inspect_profile"
        if profile_data is None:
            profile_data = view_profile(cdp, sim, profile_url)
            increment_counter(state, "profile_views")

        if profile_data.get("error"):
            result["error"] = profile_data.get("danger", "profile_view_failed")
            return result

        result["profile"] = profile_data
        action_state = str(profile_data.get("action_state") or "").strip()

        # Already connected or pending?
        if profile_data.get("is_connected") or action_state == "already_connected":
            result["error"] = "already_connected"
            return result
        if profile_data.get("is_pending") or action_state == "already_pending":
            result["error"] = "already_pending"
            return result
        if action_state in {"profile_unavailable", "unknown"}:
            result["error"] = action_state
            return result
        if not profile_data.get("has_connect_button") and action_state not in {
            "connect_direct",
            "connect_in_more",
        }:
            result["error"] = "no_connect_button"
            result["detail"] = "Profile may require InMail or Follow only"
            return result

        stage = "return_to_profile"
        cdp.navigate(profile_url)
        _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_ACTION_READY_SELECTOR,
            timeout=30,
            stable_for=0.8,
            ignored_overlays={".authentication-outlet"},
        )
        human_delay(1, 2)

        # --- Step 2: Circuit breaker re-check ---
        stage = "check_circuit_breakers"
        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            return result

        # --- Step 3: Click Connect ---
        stage = "click_connect"
        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_ACTION_READY_SELECTOR,
            timeout=45,
            stable_for=1.0,
            ignored_overlays={".authentication-outlet"},
        )
        result["connect_ready_state"] = ready_state
        if not ready_state.get("ready"):
            result["error"] = "connect_page_not_ready"
            return result
        human_delay(1, 3, distribution="gaussian")

        connect_clicked = _click_connect_button(cdp, sim, action_state=action_state)

        if not connect_clicked:
            result["error"] = "connect_button_not_clickable"
            return result

        human_delay(1, 2)

        # --- Step 4: Handle the connection modal ---
        stage = "handle_note_modal"
        if note:
            add_note_clicked = _click_add_note_button(cdp, sim)

            if add_note_clicked:
                human_delay(0.5, 1.5)
                _type_connection_note(cdp, sim, note[:300])
                human_delay(0.5, 1.5)
            else:
                result["note_skipped"] = True
                result["note_skip_reason"] = "add_note_button_not_found"

        # --- Step 5: Click Send ---
        stage = "click_send"
        modal_ready = _wait_for_connect_modal(cdp, timeout=30)
        modal_state = _inspect_connect_modal(cdp)
        result["send_modal_ready_state"] = {"ready": modal_ready, **modal_state}
        if not modal_ready:
            _dismiss_connect_modal(cdp, sim)
            result["error"] = "send_modal_not_ready"
            return result
        if modal_state.get("emailRequired"):
            _dismiss_connect_modal(cdp, sim)
            result["error"] = "email_required_to_connect"
            result["detail"] = (
                "LinkedIn requires the member's email before sending this invitation."
            )
            return result

        if verify_connection_modal_only:
            # Follow the production send route through its final checkpoint;
            # replace only the Send click with a confirmed dismissal.
            result["modal_opened"] = True
            result["send_without_note_found"] = isinstance(modal_state.get("sendWithoutNote"), dict)
            result["send_without_note_disabled"] = bool(
                isinstance(modal_state.get("sendWithoutNote"), dict)
                and modal_state["sendWithoutNote"].get("disabled")
            )
            result["email_required"] = False
            result["closed"] = _dismiss_connect_modal(cdp, sim)
            result["success"] = bool(result["closed"])
            if not result["success"]:
                result["error"] = "connection_modal_not_dismissed"
            return result

        human_delay(0.5, 1.5, distribution="gaussian")
        send_clicked = _click_send_button(cdp, sim)

        if not send_clicked:
            _dismiss_connect_modal(cdp, sim)
            send_without_note = modal_state.get("sendWithoutNote")
            if isinstance(send_without_note, dict) and send_without_note.get("disabled"):
                result["error"] = "send_without_note_disabled"
            else:
                result["error"] = "send_button_not_clickable"
            return result

        human_delay(1, 3)

        # --- Step 6: Verify success ---
        stage = "verify_success"
        _wait_for_linkedin_ready(
            cdp,
            expected_selector="body",
            timeout=20,
            stable_for=0.8,
            ignored_overlays={".authentication-outlet"},
        )
        verification: dict[str, Any] = {"pending": False, "state": None, "attempts": []}
        for _attempt in range(3):
            live_state = inspect_profile_action_state(cdp, profile_url)
            state_name = str(live_state.get("state") or "").strip()
            verification["attempts"].append(
                {
                    "state": state_name,
                    "profileName": live_state.get("profileName"),
                    "url": live_state.get("url"),
                }
            )
            if state_name == "already_pending":
                verification["pending"] = True
                verification["state"] = state_name
                break
            human_delay(2, 4)

        result["verification"] = verification
        result["success"] = bool(verification.get("pending"))
        result["verified_pending"] = bool(verification.get("pending"))
        result["note_sent"] = bool(note) and not result.get("note_skipped")
        if not result["success"]:
            result["error"] = "send_unverified"

        if result["success"]:
            increment_counter(state, "conn_req_sent")

        return result
    except Exception as exc:
        diagnostics_path = _write_send_diagnostics(cdp, profile_url, stage, exc, result)
        raise RuntimeError(f"{exc} | diagnostics={diagnostics_path}")


def send_connection_only(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
    profile_state: dict[str, Any] | None = None,
    note: str | None = None,
    verify_connection_modal_only: bool = False,
) -> dict[str, Any]:
    """Explicit send-only interface used by the outreach state machine."""
    profile_data = profile_state
    if profile_data and "action_state" not in profile_data:
        profile_data = {
            "name": str(profile_data.get("profileName", "")).strip(),
            "has_connect_button": profile_data.get("state")
            in {"connect_direct", "connect_in_more"},
            "is_pending": profile_data.get("state") == "already_pending",
            "is_connected": profile_data.get("state") == "already_connected",
            "action_state": profile_data.get("state"),
            "inspection": profile_data,
        }
    return send_connection_request(
        cdp,
        sim,
        state,
        profile_url,
        note=note,
        enable_notifications=False,
        profile_data=profile_data,
        activity_data={},
        verify_connection_modal_only=verify_connection_modal_only,
    )


def verify_no_note_send_ui(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict[str, Any],
    profile_url: str,
    profile_state: dict[str, Any],
) -> dict[str, Any]:
    """Use the production send path through the modal, then dismiss it safely."""
    action_state = str(profile_state.get("state") or "").strip()
    profile_data = {
        "name": str(profile_state.get("profileName", "")).strip(),
        "has_connect_button": action_state in {"connect_direct", "connect_in_more"},
        "is_pending": action_state == "already_pending",
        "is_connected": action_state == "already_connected",
        "action_state": action_state,
        "inspection": profile_state,
    }
    send_path = send_connection_request(
        cdp,
        sim,
        state,
        profile_url,
        note=None,
        enable_notifications=False,
        profile_data=profile_data,
        activity_data={},
        verify_connection_modal_only=True,
    )
    return {
        "ok": bool(send_path.get("success")),
        "profile_url": profile_url,
        "action_state": action_state,
        "modal_opened": bool(send_path.get("modal_opened")),
        "send_without_note_found": bool(send_path.get("send_without_note_found")),
        "send_without_note_disabled": send_path.get("send_without_note_disabled"),
        "email_required": bool(send_path.get("email_required")),
        "closed": bool(send_path.get("closed")),
        "modal_state": send_path.get("send_modal_ready_state"),
        **({"error": send_path["error"]} if send_path.get("error") else {}),
    }


# ---------------------------------------------------------------------------
# Engagement actions (Approaches A-E)
# ---------------------------------------------------------------------------


def like_post(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Like a post visible in the current viewport or navigate to a specific post URL.

    Approach E: Feed engagement / filler activity between connection requests.
    """
    result = {"action": "like_post", "success": False}

    if post_url:
        cdp.navigate(post_url)
        human_delay(2, 4)
        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            return result

    # Find and click the like button
    like_info = cdp.evaluate("""
        (() => {
            // Find the like button (not already liked)
            const likeBtns = document.querySelectorAll(
                'button[aria-label*="Like"], button[aria-label*="like"]'
            );
            for (const btn of likeBtns) {
                const label = btn.getAttribute('aria-label') || '';
                const pressed = btn.getAttribute('aria-pressed');
                // Skip if already liked
                if (pressed === 'true') continue;
                // Skip "Unlike" buttons
                if (label.toLowerCase().startsWith('unlike')) continue;
                return JSON.stringify({
                    found: true,
                    label: label.substring(0, 100),
                    already_liked: false,
                });
            }
            // Check if already liked
            const alreadyLiked = document.querySelector(
                'button[aria-pressed="true"][aria-label*="like"]'
            );
            if (alreadyLiked) {
                return JSON.stringify({found: true, already_liked: true});
            }
            return JSON.stringify({found: false});
        })()
    """)

    info = json.loads(like_info) if like_info else {}

    if not info.get("found"):
        result["error"] = "like_button_not_found"
        return result

    if info.get("already_liked"):
        result["error"] = "already_liked"
        return result

    # Human-like reading pause before liking
    human_delay(2, 6, distribution="gaussian")

    clicked = sim.click_element(
        'button[aria-label*="Like"]:not([aria-pressed="true"]), '
        'button[aria-label*="like"]:not([aria-pressed="true"])'
    )

    if clicked:
        result["success"] = True
        increment_counter(state, "likes")
        human_delay(0.5, 2)
    else:
        result["error"] = "like_click_failed"

    return result


def follow_engagement_trail(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    post_url: str,
    max_profiles: int = 3,
) -> dict[str, Any]:
    """Visit profiles of people who engaged with a prospect's post.

    Approach B: Follow the engagement trail — visit likers/commenters
    to build a natural browsing pattern before connecting with the prospect.

    Returns list of profiles visited (for the agent to potentially use).
    """
    result = {
        "action": "follow_engagement_trail",
        "post_url": post_url,
        "profiles_visited": [],
    }

    cdp.navigate(post_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Read the post content first (natural behavior)
    human_delay(3, 8, distribution="gaussian")
    sim.scroll(random.randint(100, 300))
    human_delay(1, 3)

    # Extract commenter/reactor profile links
    trail_data = cdp.evaluate(f"""
        (() => {{
            const profiles = [];
            const seen = new Set();

            // Get commenters
            const commentAuthors = document.querySelectorAll(
                '.comments-comment-item__post-meta a[href*="/in/"], ' +
                '.comments-post-meta__profile-info-wrapper a[href*="/in/"]'
            );
            commentAuthors.forEach(a => {{
                const href = a.href.split('?')[0];
                if (!seen.has(href)) {{
                    seen.add(href);
                    const name = a.innerText.trim().split('\\n')[0];
                    profiles.push({{url: href, name: name, source: 'commenter'}});
                }}
            }});

            // Get reactor profile links (from reaction overlay if visible)
            const reactorLinks = document.querySelectorAll(
                '.social-details-reactors-tab-body a[href*="/in/"], ' +
                'a.social-details-social-counts__count-value'
            );
            reactorLinks.forEach(a => {{
                const href = a.href.split('?')[0];
                if (href.includes('/in/') && !seen.has(href)) {{
                    seen.add(href);
                    const name = a.innerText.trim().split('\\n')[0];
                    profiles.push({{url: href, name: name, source: 'reactor'}});
                }}
            }});

            return JSON.stringify(profiles.slice(0, {max_profiles + 2}));
        }})()
    """)

    profiles = json.loads(trail_data) if trail_data else []

    if not profiles:
        result["profiles_found"] = 0
        return result

    # Shuffle and visit a subset
    random.shuffle(profiles)
    to_visit = profiles[:max_profiles]

    for profile in to_visit:
        # Check view quota
        if get_counter(state, "profile_views") >= MAX_PROFILE_VIEWS_PER_DAY:
            break

        human_delay(2, 5, distribution="gaussian")
        cdp.navigate(profile["url"])
        human_delay(2, 4)

        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            break

        # Brief natural scroll
        scroll_depth = random.uniform(0.2, 0.5)
        sim.scroll_to_bottom(fraction=scroll_depth, speed="slow")
        human_delay(3, 10, distribution="gaussian")

        increment_counter(state, "profile_views")
        result["profiles_visited"].append(
            {
                "url": profile["url"],
                "name": profile.get("name", ""),
                "source": profile.get("source", ""),
            }
        )

    return result


def scan_reaction_list(
    cdp: CDPConnection,
    sim: HumanSimulator,
    post_url: str,
) -> dict[str, Any]:
    """Open the reaction list on a post and extract reactor profiles.

    Approach C: Reaction mining — find new prospects from post reactions.
    Returns reactor data for the agent to evaluate.
    """
    result = {"action": "scan_reaction_list", "post_url": post_url, "reactors": []}

    cdp.navigate(post_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Read post naturally first
    human_delay(2, 5)

    # Click on the reaction count to open the reactor list
    clicked = sim.click_element(
        "button.social-details-social-counts__reactions-count, "
        'button[aria-label*="reaction"], '
        "span.social-details-social-counts__reactions-count"
    )

    if not clicked:
        result["error"] = "reaction_count_not_clickable"
        return result

    human_delay(1.5, 3)

    # Scroll the reactor list a bit
    sim.scroll(random.randint(200, 400))
    human_delay(1, 3)

    # Extract reactor profiles
    reactor_data = cdp.evaluate("""
        (() => {
            const reactors = [];
            const items = document.querySelectorAll(
                '.social-details-reactors-tab-body__profile-link, ' +
                '.social-details-reactors-tab-body li a[href*="/in/"], ' +
                '[class*="reactor"] a[href*="/in/"]'
            );
            items.forEach((item, i) => {
                if (i >= 20) return;
                const href = item.href ? item.href.split('?')[0] : '';
                const nameEl = item.querySelector('span[class*="name"], span[dir="ltr"]');
                const name = nameEl ? nameEl.innerText.trim() : item.innerText.trim().split('\\n')[0];
                const headlineEl = item.closest('li')?.querySelector('[class*="headline"], [class*="subline"]');
                const headline = headlineEl ? headlineEl.innerText.trim() : '';
                if (href.includes('/in/')) {
                    reactors.push({
                        url: href,
                        name: name,
                        headline: headline.substring(0, 100),
                        index: i,
                    });
                }
            });
            return JSON.stringify(reactors);
        })()
    """)

    reactors = json.loads(reactor_data) if reactor_data else []
    result["reactors"] = reactors
    result["count"] = len(reactors)

    # Dismiss the modal
    human_delay(1, 2)
    sim.click_element('button[aria-label="Dismiss"], button[aria-label="Close"]')
    human_delay(0.5, 1.5)

    return result


# ---------------------------------------------------------------------------
# Sent-invitations scraper & subtractive acceptance detection
# ---------------------------------------------------------------------------


def _normalize_linkedin_profile_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    normalized = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    return re.sub(r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", normalized)


def _normalize_sent_invitation_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def scrape_sent_invitations(
    cdp: CDPConnection,
    sim: HumanSimulator,
    max_scroll_passes: int = 20,
) -> dict[str, Any]:
    """Scrape the Sent Invitations page for all currently pending invitations.

    Returns a dict with:
    - 'invitations': list of {url, name, headline, sent_text} for each pending invite
    - 'urls': set of normalized profile URLs for fast lookup

    Uses LazyColumn structure. Scrolls to load more if needed and accumulates
    unique invitations across snapshots because LinkedIn can virtualize the list.
    """
    result: dict[str, Any] = {"action": "scrape_sent_invitations", "invitations": []}

    SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
    EXPECTED_SELECTOR = '[data-component-type="LazyColumn"]'

    nav = _navigate_with_readiness(
        cdp,
        SENT_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            SENT_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Sent invitations page not ready: "
            f"readyState={nav.get('readyState')}, "
            f"overlay={nav.get('overlay_detected')}, "
            f"selector_found={nav.get('selector_found')}"
        )
        return result

    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    cdp.evaluate("window.scrollTo(0, 0)")
    human_delay(1, 2)

    # LinkedIn's sent-invitations page can keep only part of the list stable in
    # the DOM while new rows appear during scrolling, so count unique rows across
    # every observed snapshot instead of trusting the final LazyColumn contents.
    seen_keys: set = set()
    seen_urls: set = set()
    unique: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    stale_passes = 0

    for scroll_pass in range(max_scroll_passes):
        raw = _extract_sent_invitations_dom(cdp)
        invitations = json.loads(raw) if raw else []
        new_count = 0

        for inv in invitations:
            url = _normalize_linkedin_profile_url(inv.get("url", ""))
            name = _normalize_sent_invitation_name(inv.get("name", ""))
            sent_text = (inv.get("sent_text") or "").strip().lower()
            key = url or f"name:{name}|sent:{sent_text}"
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            if url:
                seen_urls.add(url)
            unique.append(inv)
            new_count += 1

        metrics_raw = cdp.evaluate("""
            (() => JSON.stringify({
                scrollY: Math.round(window.scrollY || 0),
                innerHeight: Math.round(window.innerHeight || 0),
                scrollHeight: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0)
            }))()
        """)
        metrics = json.loads(metrics_raw) if metrics_raw else {}
        at_bottom = (
            metrics.get("scrollY", 0) + metrics.get("innerHeight", 0)
            >= metrics.get("scrollHeight", 0) - 50
        )
        snapshots.append(
            {
                "pass": scroll_pass + 1,
                "snapshot_count": len(invitations),
                "new_count": new_count,
                "unique_count": len(unique),
                "scrollY": metrics.get("scrollY", 0),
                "scrollHeight": metrics.get("scrollHeight", 0),
                "at_bottom": at_bottom,
            }
        )

        if new_count == 0:
            stale_passes += 1
        else:
            stale_passes = 0

        if at_bottom and stale_passes >= 2:
            break
        if stale_passes >= 4:
            break

        _safe_scroll_or_js(cdp, sim, random.randint(800, 1400))
        human_delay(1.5, 3)

    result["invitations"] = unique
    result["count"] = len(unique)
    result["urls"] = seen_urls
    result["scroll_snapshots"] = snapshots
    return result


def _extract_sent_invitations_dom(cdp: CDPConnection) -> str | None:
    """Extract sent invitation data from the LazyColumn DOM structure.

    Each invitation card is a div child of LazyColumn (alternating with hr dividers).
    Card innerText format: "Name\\n\\nHeadline\\n\\nSent X ago\\n\\nWithdraw"
    Profile URL is in a[href*="/in/"] (avatar link, no text).
    """
    return cdp.evaluate("""
        (() => {
            const invitations = [];
            const lazy = document.querySelector('[data-component-type="LazyColumn"]');
            if (!lazy) return JSON.stringify(invitations);

            const directChildren = Array.from(lazy.children).filter(
                c => c.tagName.toLowerCase() !== 'hr'
            );
            const nestedCards = Array.from(lazy.querySelectorAll(
                '[data-display-contents="true"], li, .artdeco-list__item'
            ));
            const children = [...directChildren, ...nestedCards].filter((card, idx, arr) =>
                card && arr.indexOf(card) === idx
            );

            children.forEach((card, i) => {
                // Find profile URL from avatar link
                const linkEl = card.querySelector('a[href*="/in/"]');
                const url = linkEl ? linkEl.href.split('?')[0] : '';

                // Parse innerText for name, headline, sent time
                const fullText = card.innerText || '';
                if (!/\\bwithdraw\\b/i.test(fullText) || !/\\bsent\\b/i.test(fullText)) {
                    return;
                }
                const lines = fullText.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l.length > 0 && l.toLowerCase() !== 'withdraw');

                const name = lines.length > 0 ? lines[0] : '';
                let headline = '';
                let sentText = '';

                for (const line of lines) {
                    const lower = line.toLowerCase();
                    if (lower.startsWith('sent ')) {
                        sentText = lower;
                    } else if (line !== name) {
                        headline = headline || line;
                    }
                }

                if (url && name) {
                    invitations.push({
                        url: url,
                        name: name,
                        headline: headline.substring(0, 150),
                        sent_text: sentText,
                        index: i,
                    });
                }
            });

            return JSON.stringify(invitations);
        })()
    """)


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


def _extract_connections_dom(cdp: CDPConnection) -> str | None:
    """Extract connection card data from the DOM via Runtime.evaluate.

    Factored out of check_acceptances to enable retry logic.

    Strategy: LinkedIn uses obfuscated class names that rotate with deploys,
    so we anchor on stable structural attributes:
    - data-component-type="LazyColumn" for the list container
    - data-display-contents="true" for individual card wrappers
    - a[href*="/in/"] for profile links
    - innerText parsing for name, headline, and connection date
    Falls back to scanning all profile links if the structural selectors change.
    """
    return cdp.evaluate("""
        (() => {
            const connections = [];
            let cards = [];

            // Strategy 1: Find cards via LazyColumn > data-display-contents
            const lazyCol = document.querySelector('[data-component-type="LazyColumn"]');
            if (lazyCol) {
                cards = lazyCol.querySelectorAll('[data-display-contents="true"]');
            }

            // Strategy 2 (legacy fallback): old-style selectors
            if (cards.length === 0) {
                cards = document.querySelectorAll(
                    '.mn-connection-card, ' +
                    '[class*="connection-card"], ' +
                    '.scaffold-finite-scroll__content li'
                );
            }

            // Strategy 3 (broad fallback): any container with a /in/ link
            // Group profile links by their nearest shared ancestor
            if (cards.length === 0) {
                const allLinks = document.querySelectorAll('a[href*="/in/"]');
                const seen = new Set();
                allLinks.forEach(a => {
                    // Walk up to find a container-level parent
                    let container = a.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (container && container.parentElement &&
                            container.parentElement.children.length >= 5) {
                            break;
                        }
                        if (container) container = container.parentElement;
                    }
                    if (container && !seen.has(container)) {
                        seen.add(container);
                        cards = [...(cards || []), container];
                    }
                });
            }

            const cardArr = Array.from(cards);
            cardArr.forEach((card, i) => {
                if (i >= 30) return;

                // Find profile link (prefer the one with text content)
                const allLinks = card.querySelectorAll('a[href*="/in/"]');
                let linkEl = null;
                let nameFromLink = '';
                for (const a of allLinks) {
                    const text = a.innerText.trim();
                    if (text.length > 0) {
                        linkEl = a;
                        nameFromLink = text.split('\\n')[0].trim();
                        break;
                    }
                }
                // Fall back to first link if none had text
                if (!linkEl && allLinks.length > 0) {
                    linkEl = allLinks[0];
                }

                const url = linkEl ? linkEl.href.split('?')[0] : '';

                // Parse card text for name, time, etc.
                const fullText = card.innerText || '';
                const lines = fullText.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l.length > 0 && l !== 'Message');

                // Name is the first meaningful line (or from link text)
                const name = nameFromLink || (lines.length > 0 ? lines[0] : '');

                // Time/date: look for "Connected on" or relative time patterns
                let timeText = '';
                for (const line of lines) {
                    const lower = line.toLowerCase();
                    if (lower.includes('connected on') ||
                        lower.includes('ago') ||
                        lower.includes('today') ||
                        lower.includes('yesterday') ||
                        lower.includes('just now')) {
                        timeText = lower;
                        break;
                    }
                }

                if (url && name) {
                    connections.push({
                        url: url,
                        name: name,
                        time_text: timeText,
                        index: i,
                    });
                }
            });
            return JSON.stringify(connections);
        })()
    """)


def scan_prospect_box(
    cdp: CDPConnection,
    sim: HumanSimulator,
) -> dict[str, Any]:
    """Scan the 'People you may know' suggestions box on profiles or My Network.

    Approach D: Prospect box scan — find related prospects from LinkedIn's suggestions.
    Returns suggestion data for the agent to evaluate.
    """
    result = {"action": "scan_prospect_box", "suggestions": []}

    # Check current page for suggestions or navigate to My Network
    page = detect_page(cdp)
    if page["page_type"] not in ("profile", "my_network"):
        cdp.navigate("https://www.linkedin.com/mynetwork/")
        human_delay(2, 4)

    suggestion_data = cdp.evaluate("""
        (() => {
            const suggestions = [];
            const cards = document.querySelectorAll(
                '.discover-entity-card, ' +
                '[class*="pymk"], ' +
                '.mn-pymk-list__card, ' +
                '[class*="people-you-may-know"] li'
            );
            cards.forEach((card, i) => {
                if (i >= 10) return;
                const linkEl = card.querySelector('a[href*="/in/"]');
                const nameEl = card.querySelector(
                    '[class*="discover-person-card__name"], ' +
                    '[class*="entity-result__title"], ' +
                    'span[dir="ltr"]'
                );
                const headlineEl = card.querySelector(
                    '[class*="discover-person-card__occupation"], ' +
                    '[class*="entity-result__summary"], ' +
                    '[class*="subline"]'
                );
                const mutualEl = card.querySelector(
                    '[class*="member-insights"], ' +
                    '[class*="mutual"]'
                );

                const url = linkEl ? linkEl.href.split('?')[0] : '';
                const name = nameEl ? nameEl.innerText.trim() : '';
                const headline = headlineEl ? headlineEl.innerText.trim() : '';
                const mutual = mutualEl ? mutualEl.innerText.trim() : '';

                if (url && name) {
                    suggestions.push({
                        url: url,
                        name: name,
                        headline: headline.substring(0, 120),
                        mutual_connections: mutual.substring(0, 80),
                        index: i,
                    });
                }
            });
            return JSON.stringify(suggestions);
        })()
    """)

    suggestions = json.loads(suggestion_data) if suggestion_data else []
    result["suggestions"] = suggestions
    result["count"] = len(suggestions)

    return result


# ---------------------------------------------------------------------------
# Withdrawal manager — withdraw stale pending connection requests
# ---------------------------------------------------------------------------


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
