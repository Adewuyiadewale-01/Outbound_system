"""Connection-request engine (send path + no-note modal verification).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S19). Pure move.
"""

from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _wait_for_linkedin_ready
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.mapper import (
    PROFILE_ACTION_READY_SELECTOR,
    inspect_profile_action_state,
    view_profile,
)
from outbound.shared.quota import (
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    MAX_PROFILE_VIEWS_PER_DAY,
    get_counter,
    get_weekly_counter,
    increment_counter,
)
from outbound.shared.send.diagnostics import _write_send_diagnostics
from outbound.shared.send.modal import (
    _click_add_note_button,
    _click_connect_button,
    _click_send_button,
    _dismiss_connect_modal,
    _inspect_connect_modal,
    _type_connection_note,
    _wait_for_connect_modal,
)


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
