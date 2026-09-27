"""LinkedInSession — high-level session manager (the hub class).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S27). Pure move.
"""

import random
import time
from typing import Any

from outbound.shared.acceptance.notifications import check_acceptance_notifications
from outbound.shared.acceptance.subtractive import (
    check_acceptances,
    check_acceptances_subtractive,
)
from outbound.shared.actions.engagement import (
    follow_engagement_trail,
    like_post,
    scan_reaction_list,
)
from outbound.shared.actions.prospecting import scan_prospect_box
from outbound.shared.actions.withdrawal import withdraw_connection
from outbound.shared.activity.readers import read_activity_tab, read_activity_tabs_detail
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.feed.post_types import execute_feed_scroll, generate_scroll_stop_sequence
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.mapper import inspect_profile_action_state, view_profile
from outbound.shared.profile.notif_toggle import toggle_profile_notifications
from outbound.shared.quota import (
    MAX_ACTIONS_PER_MINUTE,
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    MAX_PROFILE_VIEWS_PER_DAY,
    get_counter,
    get_weekly_counter,
    increment_counter,
    load_state,
)
from outbound.shared.send.engine import (
    send_connection_only,
    send_connection_request,
    verify_no_note_send_ui,
)
from outbound.shared.session.envelope import session_cool_down, session_warm_up
from outbound.shared.session.interleave import run_session
from outbound.shared.session.preflight import preflight_check


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
