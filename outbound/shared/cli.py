"""Command-line interface for the LinkedIn helper (compatibility entry point).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S28). Pure move.
"""

import argparse
import json
import sys

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import detect_page
from outbound.shared.feed.post_types import execute_feed_scroll, generate_scroll_stop_sequence
from outbound.shared.quota import (
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    MAX_PROFILE_VIEWS_PER_DAY,
    get_counter,
    get_weekly_counter,
    load_state,
)
from outbound.shared.session.manager import LinkedInSession
from outbound.shared.session.preflight import preflight_check


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
