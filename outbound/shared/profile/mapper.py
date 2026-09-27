"""Profile action-state mapper (connect / pending / connected classification).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S15). Pure move.
"""

import json
import random
import re
import time
from datetime import datetime
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _wait_for_linkedin_ready
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.diagnostics import _write_profile_mapper_dump

PROFILE_TOPCARD_SELECTOR = (
    '[componentkey*="profile.card"][componentkey*="Topcard"], '
    '[componentkey*="profile.card"][componentkey*="topcard"], '
    '[componentkey*="Topcard"], '
    '[componentkey*="topcard"], '
    ".pv-top-card"
)


PROFILE_READY_SELECTOR = f"main h1, h1, {PROFILE_TOPCARD_SELECTOR}"


PROFILE_ACTION_READY_SELECTOR = (
    f'{PROFILE_READY_SELECTOR}, button, [role="button"], '
    'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"]'
)


PROFILE_MORE_CONNECT_SELECTOR = (
    'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"]'
    '[href^="/preload/custom-invite/"]'
)


def inspect_profile_action_state(cdp: CDPConnection, profile_url: str) -> dict[str, Any]:
    """Inspect the profile top-card action controls without heavy browsing."""
    result: dict[str, Any] = {
        "action": "inspect_profile_action_state",
        "profile_url": profile_url,
        "state": "unknown",
    }
    try:
        navigation_started_at = time.time()
        cdp.navigate(profile_url)
        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_READY_SELECTOR,
            timeout=45,
            stable_for=1.0,
            # LinkedIn keeps this SPA wrapper visible on normal profile pages;
            # it is not the same as an auth wall once profile content is present.
            ignored_overlays={".authentication-outlet"},
        )
        readiness_fired_at = datetime.now().isoformat(timespec="milliseconds")
        result["load_state"] = ready_state
        if not ready_state.get("ready"):
            result["state"] = "unknown"
            result["error"] = "profile_load_timeout"
            _write_profile_mapper_dump(
                cdp, profile_url, result, navigation_started_at, readiness_fired_at
            )
            return result

        danger = check_circuit_breakers(cdp)
        if danger:
            result["state"] = "profile_unavailable"
            result["error"] = danger
            _write_profile_mapper_dump(
                cdp, profile_url, result, navigation_started_at, readiness_fired_at
            )
            return result

        raw = cdp.evaluate(
            """
            (() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0';
                };
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const h1 = document.querySelector('h1');
                const topCard =
                    document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                    document.querySelector('[componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="topcard"]') ||
                    h1?.closest('section') ||
                    h1?.closest('.artdeco-card') ||
                    document.querySelector('.pv-top-card') ||
                    document.querySelector('main');
                const controls = Array.from((topCard || document).querySelectorAll('button, [role="button"], a'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName.toLowerCase(),
                            role: el.getAttribute('role') || '',
                            text: norm(el.innerText || el.textContent || ''),
                            ariaLabel: norm(el.getAttribute('aria-label') || ''),
                            dataControlName: norm(el.getAttribute('data-control-name') || ''),
                            href: el.getAttribute('href') || '',
                            componentKey: norm(el.getAttribute('componentkey') || ''),
                            rect: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                            },
                        };
                    });
                const bodyText = norm((document.body && document.body.innerText) || '');
                return JSON.stringify({
                    url: location.href,
                    readyState: document.readyState,
                    profileName: norm(h1?.innerText || '') || norm((topCard && topCard.innerText) || '').split(' · ')[0].split('\\n')[0],
                    topCardText: norm((topCard && topCard.innerText) || ''),
                    bodySample: bodyText.slice(0, 1000),
                    controls,
                });
            })()
        """,
            timeout=10,
        )
        data = json.loads(raw) if raw else {}
        result.update(data)

        controls = data.get("controls", []) if isinstance(data, dict) else []
        result["top_card_ready_at"] = (
            datetime.now().isoformat(timespec="milliseconds") if controls else None
        )
        result["direct_buttons_seen"] = [
            str(item.get("text", "")).strip()
            for item in controls
            if str(item.get("tag", "")).lower() == "button" and str(item.get("text", "")).strip()
        ]
        top_text = str(data.get("topCardText", "")).strip()
        body_sample = str(data.get("bodySample", "")).strip().lower()

        def exact_text(value: str) -> list[dict[str, Any]]:
            return [
                item
                for item in controls
                if str(item.get("text", "")).strip().lower() == value.lower()
            ]

        pending_controls = [
            item
            for item in controls
            if str(item.get("text", "")).strip().lower() == "pending"
            or str(item.get("ariaLabel", "")).strip().lower().startswith("pending")
            or "withdraw invitation" in str(item.get("ariaLabel", "")).lower()
        ]
        direct_connect = [
            item
            for item in controls
            if str(item.get("text", "")).strip().lower() == "connect"
            and (
                re.match(r"^invite .+ to connect$", str(item.get("ariaLabel", "")).strip(), re.I)
                or str(item.get("componentKey", "")).startswith("ConnectButtonstate:invitation:")
                or str(item.get("tag", "")).lower() in {"button", "a"}
            )
        ]
        message_controls = exact_text("message")

        if any(
            phrase in body_sample
            for phrase in (
                "profile not found",
                "this profile is not available",
                "this profile is unavailable",
                "member not found",
            )
        ):
            result["state"] = "profile_unavailable"
        elif pending_controls:
            result["state"] = "already_pending"
            result["matched_control"] = pending_controls[0]
        elif direct_connect:
            result["state"] = "connect_direct"
            result["matched_control"] = direct_connect[0]
        else:
            more_controls = [
                item
                for item in controls
                if str(item.get("text", "")).strip().lower() == "more"
                or str(item.get("ariaLabel", "")).strip().lower() == "more actions"
            ]
            result["more_button_seen"] = bool(more_controls)
            if more_controls:
                menu_connect = _open_more_and_find_connect(cdp)
                result["more_menu"] = menu_connect
                if menu_connect.get("found"):
                    result["state"] = "connect_in_more"
                    result["matched_control"] = menu_connect.get("control")
                elif menu_connect.get("clicked_more") and not menu_connect.get("menu_populated"):
                    result["state"] = "unknown"
                    result["error"] = "more_menu_unreadable"
            if result["state"] == "unknown":
                top_lower = f" {top_text.lower()} "
                is_first = " 1st " in top_lower or "1st degree connection" in top_lower
                has_connect_like = bool(direct_connect or exact_text("connect"))
                has_pending_like = bool(pending_controls)
                if is_first and message_controls and not has_connect_like and not has_pending_like:
                    result["state"] = "already_connected"
                elif controls and result.get("error") != "more_menu_unreadable":
                    result["state"] = "no_connect_button"
        _write_profile_mapper_dump(
            cdp, profile_url, result, navigation_started_at, readiness_fired_at
        )
        return result
    except Exception as exc:
        result["state"] = "unknown"
        result["error"] = str(exc)
        return result


def _open_more_and_find_connect(cdp: CDPConnection) -> dict[str, Any]:
    """Open the profile More menu once and look for an exact Connect item."""
    more_clicked_at: str | None = None
    clicked = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const h1 = document.querySelector('h1');
            const topCard =
                document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                document.querySelector('[componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="topcard"]') ||
                h1?.closest('section') ||
                h1?.closest('.artdeco-card') ||
                document.querySelector('.pv-top-card');
            if (!topCard) return false;
            const nodes = Array.from(topCard.querySelectorAll('button, [role="button"]'));
            const more = nodes.find((node) =>
                visible(node) &&
                (norm(node.innerText || node.textContent) === 'more' ||
                 norm(node.getAttribute('aria-label')) === 'more actions')
            );
            if (!more) return false;
            more.click();
            return true;
        })()
    """,
        timeout=8,
    )
    if not clicked:
        return {"found": False, "clicked_more": False}

    more_clicked_at = datetime.now().isoformat(timespec="milliseconds")
    deadline = time.time() + 5.0
    last_data: dict[str, Any] = {
        "found": False,
        "clicked_more": True,
        "more_clicked_at": more_clicked_at,
        "menu_items_count": 0,
        "menu_populated": False,
    }
    while time.time() < deadline:
        raw = cdp.evaluate(
            """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const sduiConnect = Array.from(document.querySelectorAll(
                'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]'
            )).find(visible);
            const scope = document.querySelector('.artdeco-dropdown__content--is-open') ||
                (sduiConnect ? sduiConnect.closest('[role="menu"], [data-test-menu], ul, div') : null);
            const menuItems = scope ?
                Array.from(scope.querySelectorAll('.artdeco-dropdown__item, [role="menuitem"]')).filter(visible) :
                Array.from(document.querySelectorAll('[role="menuitem"]')).filter(visible);
            const menuItemsCount = menuItems.length;
            const connect = sduiConnect || menuItems.find((node) =>
                /\\bto connect\\b/i.test(norm(node.getAttribute('aria-label') || '')) ||
                norm(node.innerText || node.textContent).toLowerCase() === 'connect' ||
                (node.getAttribute('componentkey') || '').startsWith('ConnectButtonstate:invitation:')
            );
            if (connect) {
                return JSON.stringify({
                    found: true,
                    menuItemsCount,
                    menuOpen: true,
                    scopeFound: !!scope,
                    control: {
                        tag: connect.tagName.toLowerCase(),
                        role: connect.getAttribute('role') || '',
                        text: norm(connect.innerText || connect.textContent || ''),
                        ariaLabel: norm(connect.getAttribute('aria-label') || ''),
                        href: connect.getAttribute('href') || '',
                        componentKey: norm(connect.getAttribute('componentkey') || ''),
                    },
                });
            }
            return JSON.stringify({
                found: false,
                menuItemsCount,
                menuOpen: !!scope || menuItemsCount > 0,
                scopeFound: !!scope,
            });
        })()
        """,
            timeout=8,
        )
        data = json.loads(raw) if raw else {"found": False, "menuItemsCount": 0}
        menu_items_count = int(data.get("menuItemsCount", 0) or 0)
        last_data = {
            "found": bool(data.get("found")),
            "clicked_more": True,
            "more_clicked_at": more_clicked_at,
            "menu_open": bool(data.get("menuOpen")),
            "scope_found": data.get("scopeFound"),
            "menu_items_count": menu_items_count,
            "menu_populated": menu_items_count > 0,
        }
        if data.get("found"):
            last_data["control"] = data.get("control")
            return last_data
        if menu_items_count > 0:
            return last_data
        time.sleep(0.1)
    last_data["error"] = "more_menu_items_timeout"
    return last_data


def _open_more_and_click_connect(cdp: CDPConnection) -> dict[str, Any]:
    """Open More and click the Connect item inside it.

    Mirrors _open_more_and_find_connect (JS click on More, polling for the open
    dropdown, find Connect by aria-label) but also clicks the matched item.
    Used by the send path so connect_in_more click has the same reliability as
    the mapper's classification.
    """

    def click_open_menu_connect() -> dict[str, Any]:
        raw = cdp.evaluate(
            """
        (() => {
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const sduiConnect = Array.from(document.querySelectorAll(
                'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]'
            )).find(visible);
            const scope = document.querySelector('.artdeco-dropdown__content--is-open') ||
                (sduiConnect ? sduiConnect.closest('[role="menu"], [data-test-menu], ul, div') : null);
            const menuItems = scope ?
                Array.from(scope.querySelectorAll('.artdeco-dropdown__item, [role="menuitem"]')).filter(visible) :
                Array.from(document.querySelectorAll('[role="menuitem"]')).filter(visible);
            const menuItemsCount = menuItems.length;
            const connect = sduiConnect || menuItems.find((node) =>
                /\\bto connect\\b/i.test(norm(node.getAttribute('aria-label') || '')) ||
                norm(node.innerText || node.textContent).toLowerCase() === 'connect' ||
                (node.getAttribute('componentkey') || '').startsWith('ConnectButtonstate:invitation:')
            );
            if (connect) {
                connect.click();
                return JSON.stringify({
                    connectClicked: true,
                    menuItemsCount,
                    menuOpen: true,
                    scopeFound: !!scope,
                    ariaLabel: norm(connect.getAttribute('aria-label') || ''),
                    href: connect.getAttribute('href') || '',
                    componentKey: norm(connect.getAttribute('componentkey') || ''),
                });
            }
            return JSON.stringify({
                connectClicked: false,
                menuItemsCount,
                menuOpen: !!scope || menuItemsCount > 0,
                scopeFound: !!scope,
            });
        })()
        """,
            timeout=8,
        )
        data = json.loads(raw) if raw else {"connectClicked": False, "menuItemsCount": 0}
        return {
            "connect_clicked": bool(data.get("connectClicked")),
            "menu_open": bool(data.get("menuOpen")),
            "scope_found": data.get("scopeFound"),
            "menu_items_count": int(data.get("menuItemsCount", 0) or 0),
            "aria_label": data.get("ariaLabel"),
            "href": data.get("href"),
            "component_key": data.get("componentKey"),
        }

    # The mapper may have already opened More. Do not click More again and
    # accidentally close the menu; consume the open dropdown first.
    existing = click_open_menu_connect()
    if existing.get("connect_clicked"):
        existing["clicked_more"] = False
        existing["used_existing_menu"] = True
        return existing

    more_clicked_at: str | None = None
    clicked = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const h1 = document.querySelector('h1');
            const topCard =
                document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                document.querySelector('[componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="topcard"]') ||
                h1?.closest('section') ||
                h1?.closest('.artdeco-card') ||
                document.querySelector('.pv-top-card');
            if (!topCard) return false;
            const nodes = Array.from(topCard.querySelectorAll('button, [role="button"]'))
                .filter((node) => !node.closest('.artdeco-dropdown__content'));
            const more = nodes.find((node) =>
                visible(node) &&
                (norm(node.innerText || node.textContent) === 'more' ||
                 norm(node.getAttribute('aria-label')) === 'more actions')
            );
            if (!more) return false;
            more.click();
            return true;
        })()
    """,
        timeout=8,
    )
    if not clicked:
        return {"clicked_more": False, "connect_clicked": False}

    more_clicked_at = datetime.now().isoformat(timespec="milliseconds")
    deadline = time.time() + 5.0
    last_data: dict[str, Any] = {
        "clicked_more": True,
        "connect_clicked": False,
        "more_clicked_at": more_clicked_at,
        "menu_items_count": 0,
    }
    while time.time() < deadline:
        data = click_open_menu_connect()
        menu_items_count = int(data.get("menu_items_count", 0) or 0)
        last_data = {
            "clicked_more": True,
            "connect_clicked": bool(data.get("connect_clicked")),
            "more_clicked_at": more_clicked_at,
            "menu_open": bool(data.get("menu_open")),
            "menu_items_count": menu_items_count,
            "aria_label": data.get("aria_label"),
            "href": data.get("href"),
            "component_key": data.get("component_key"),
        }
        if data.get("connect_clicked"):
            return last_data
        if menu_items_count > 0:
            last_data["error"] = "connect_not_in_menu"
            return last_data
        time.sleep(0.1)
    last_data["error"] = "more_menu_items_timeout"
    return last_data


def browse_profile_briefly(
    cdp: CDPConnection, sim: HumanSimulator, seconds: float = 4.0
) -> dict[str, Any]:
    """Bounded profile browsing used after a profile's action state is known."""
    started = time.time()
    try:
        sim.scroll_to_bottom(
            fraction=random.uniform(0.18, 0.32),
            speed="normal",
            max_seconds=max(1.0, min(seconds, 6.0)),
            max_distance=1800,
        )
        human_delay(1.0, 2.5)
        return {"ok": True, "elapsed": round(time.time() - started, 1)}
    except Exception as exc:
        return {"ok": False, "elapsed": round(time.time() - started, 1), "error": str(exc)}


def view_profile(cdp: CDPConnection, sim: HumanSimulator, profile_url: str) -> dict[str, Any]:
    """Navigate to a profile, scroll naturally, and extract key data.

    Returns extracted profile info.
    """
    inspected = inspect_profile_action_state(cdp, profile_url)
    if inspected.get("error") and inspected.get("state") == "unknown":
        return {"error": True, "danger": inspected.get("error"), "inspection": inspected}

    browse = browse_profile_briefly(cdp, sim, seconds=5.0)
    top_text = str(inspected.get("topCardText", ""))
    return {
        "name": str(inspected.get("profileName", "")).strip(),
        "headline": "",
        "location": "",
        "about": "",
        "connection_degree": "1st"
        if re.search(r"\b1st\b|1st degree connection", top_text, re.I)
        else "",
        "has_connect_button": inspected.get("state") in {"connect_direct", "connect_in_more"},
        "is_pending": inspected.get("state") == "already_pending",
        "is_connected": inspected.get("state") == "already_connected",
        "action_state": inspected.get("state"),
        "inspection": inspected,
        "browse": browse,
    }
