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
import sys
from pathlib import Path

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
from outbound.shared.session.envelope import session_cool_down, session_warm_up  # noqa: F401
from outbound.shared.session.interleave import _execute_interleave, run_session  # noqa: F401
from outbound.shared.session.manager import LinkedInSession  # noqa: F401
from outbound.shared.session.preflight import preflight_check  # noqa: F401

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


# ---------------------------------------------------------------------------
# Safety rails — pre-flight checks, quota enforcement, circuit breakers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# LinkedInSession — high-level session manager
# ---------------------------------------------------------------------------


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
