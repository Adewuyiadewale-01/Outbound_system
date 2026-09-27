#!/usr/bin/env python3
"""Compatibility shim for the historical linkedin_helper module.

All logic now lives under outbound/shared/* (the linkedin_helper carve — see
docs/CARVE-LINKEDIN-HELPER-PLAN.md). This file preserves the historical import
surface for its consumers and stays runnable as a CLI:

    python3 helpers/linkedin_helper.py <command>

"""

import sys
from pathlib import Path

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

if __name__ == "__main__":
    from outbound.shared.cli import main

    main()
