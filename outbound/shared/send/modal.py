"""Connection-invite modal interactions (inspect / dismiss / click / type).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S18). Pure move.
"""

import json
import time
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.profile.mapper import _open_more_and_click_connect


def _inspect_connect_modal(cdp: CDPConnection) -> dict[str, Any]:
    """Inspect LinkedIn's invite modal, including open shadow-root render paths."""
    raw = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const modalRoots = deepNodes.filter((el) => {
                const id = el.getAttribute('data-test-modal-id') || '';
                const role = el.getAttribute('role') || '';
                const text = norm(el.innerText || el.textContent || '');
                return id === 'send-invite-modal' ||
                    (role === 'dialog' && /add a note to your invitation\\?/i.test(text));
            }).filter(visible);
            const scope = modalRoots[modalRoots.length - 1] || null;
            const modalText = norm((scope && (scope.innerText || scope.textContent)) || '');
            const buttons = (scope ?
                Array.from(scope.querySelectorAll('button, [role="button"]')) :
                deepNodes.filter((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    return tag === 'button' || el.getAttribute('role') === 'button';
                })
            ).filter(visible);
            const serializeButton = (btn) => btn ? {
                tag: (btn.tagName || '').toLowerCase(),
                text: norm(btn.innerText || btn.textContent || ''),
                ariaLabel: norm(btn.getAttribute('aria-label') || ''),
                disabled: !!btn.disabled || btn.getAttribute('aria-disabled') === 'true',
            } : null;
            const addNote = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label.toLowerCase() === 'add a note';
            }) || null;
            const sendWithoutNote = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label.toLowerCase() === 'send without a note';
            }) || null;
            const likelySend = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent).toLowerCase();
                return label === 'send' || label === 'send now' || label.startsWith('send ');
            }) || null;
            const emailInputs = (scope ?
                Array.from(scope.querySelectorAll('input, textarea')) :
                deepNodes.filter((el) => ['input', 'textarea'].includes((el.tagName || '').toLowerCase()))
            ).filter(visible).filter((el) => {
                const type = norm(el.getAttribute('type') || '').toLowerCase();
                const label = norm([
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('placeholder') || '',
                    el.getAttribute('name') || '',
                    el.getAttribute('id') || '',
                ].join(' ')).toLowerCase();
                return type === 'email' || label.includes('email');
            });
            const emailRequired = /enter (their|this member'?s|the member'?s)?\\s*email to connect/i.test(modalText) ||
                /verify this member knows you/i.test(modalText) ||
                emailInputs.length > 0;
            return JSON.stringify({
                modalRootFound: !!scope,
                dialogFound: !!modalRoots.find((el) => el.getAttribute('role') === 'dialog'),
                modalTextSample: modalText.slice(0, 500),
                emailRequired,
                emailInputCount: emailInputs.length,
                addNote: serializeButton(addNote),
                sendWithoutNote: serializeButton(sendWithoutNote),
                likelySend: serializeButton(likelySend),
                buttonCount: buttons.length,
            });
        })()
    """,
        timeout=5,
    )
    return json.loads(raw) if raw else {"modalRootFound": False, "dialogFound": False}


def _dismiss_connect_modal(cdp: CDPConnection, sim: HumanSimulator | None = None) -> bool:
    """Dismiss the invite modal and confirm that it is no longer visible."""

    def modal_is_open() -> bool:
        try:
            state = _inspect_connect_modal(cdp)
            return bool(state.get("modalRootFound"))
        except Exception:
            # Failure to inspect must never be interpreted as confirmed closed.
            return True

    def wait_until_closed(timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not modal_is_open():
                return True
            time.sleep(0.15)
        return not modal_is_open()

    if not modal_is_open():
        return True

    try:
        clicked = cdp.evaluate(
            """
            (() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const deepNodes = [];
                const walk = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    for (const el of root.querySelectorAll('*')) {
                        deepNodes.push(el);
                        if (el.shadowRoot) walk(el.shadowRoot);
                    }
                };
                walk(document);
                const modalRoots = deepNodes.filter((el) => {
                    const id = el.getAttribute('data-test-modal-id') || '';
                    const role = el.getAttribute('role') || '';
                    const text = norm(el.innerText || el.textContent || '');
                    return visible(el) && (id === 'send-invite-modal' ||
                        (role === 'dialog' && (text.includes('invitation') ||
                            text.includes('send without a note'))));
                });
                const scope = modalRoots[modalRoots.length - 1] || null;
                if (!scope) return false;
                const close = Array.from(scope.querySelectorAll('button, [role="button"]')).find((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag !== 'button' && el.getAttribute('role') !== 'button') return false;
                    if (!visible(el)) return false;
                    const label = norm([
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.innerText || el.textContent || '',
                    ].join(' '));
                    return /(^|\\s)(dismiss|close)(\\s|$)/.test(label);
                });
                if (!close) return false;
                close.click();
                return true;
            })()
        """,
            timeout=5,
        )
        if bool(clicked) and wait_until_closed():
            return True
    except Exception:
        pass
    if sim:
        try:
            clicked = sim.click_element(
                '[data-test-modal-id="send-invite-modal"] button[aria-label="Dismiss"], '
                '[data-test-modal-id="send-invite-modal"] button[aria-label="Close"], '
                '[role="dialog"] button[aria-label="Dismiss"], '
                '[role="dialog"] button[aria-label="Close"]'
            )
            if clicked and wait_until_closed():
                return True
        except Exception:
            pass

    # Escape is a safe modal-dismiss fallback and cannot activate the Send
    # control.  Confirm closure after dispatching it rather than trusting the
    # key event itself.
    try:
        cdp.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        cdp.send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        return wait_until_closed()
    except Exception:
        return False


def _wait_for_connect_modal(cdp: CDPConnection, timeout: float = 3.0) -> bool:
    """Poll for the connect modal (a dialog containing a Send button) to appear after clicking Connect."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            modal = _inspect_connect_modal(cdp)
            if modal.get("sendWithoutNote") or modal.get("likelySend"):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _click_connect_button(
    cdp: CDPConnection,
    sim: HumanSimulator,
    action_state: str = "",
    modal_timeout: float = 30.0,
) -> bool:
    """Click Connect from the profile top-card only.

    Never search the whole document: LinkedIn can render People You May Know
    cards below the profile, and those cards also contain Connect buttons.
    """

    def attempt_click() -> bool:
        if action_state == "connect_in_more":
            more_result = _open_more_and_click_connect(cdp)
            return bool(more_result.get("connect_clicked"))

        # Direct top-card path only. Verified empirically to fire LinkedIn's
        # React handler; CDP mouse events were silently no-op'ing here.
        direct_clicked = cdp.evaluate("""
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
                const nodes = Array.from(topCard.querySelectorAll('button, [role="button"], a'))
                    .filter((node) => !node.closest('.artdeco-dropdown__content'));
                for (const node of nodes) {
                    if (!visible(node)) continue;
                    const text = norm(node.innerText || node.textContent);
                    const aria = norm(node.getAttribute('aria-label'));
                    const isConnectText = text === 'connect';
                    const isConnectAria = /^invite\\b.*\\bto connect\\b/i.test(aria);
                    const componentKey = node.getAttribute('componentkey') || '';
                    const href = node.getAttribute('href') || '';
                    const isConnectComponent =
                        componentKey.startsWith('ConnectButtonstate:invitation:') &&
                        (componentKey.endsWith('_connect') || href.startsWith('/preload/custom-invite/'));
                    if (!isConnectText && !isConnectAria && !isConnectComponent) continue;
                    node.click();
                    return true;
                }
                return false;
            })()
        """)
        return bool(direct_clicked)

    if not attempt_click():
        return False
    # LinkedIn's invitation dialog is sometimes rendered asynchronously after
    # the profile action has accepted the click.  Returning false after the
    # old three-second probe left a real, late-opening modal on screen and
    # made both production sends and no-send checks misreport the action.
    if _wait_for_connect_modal(cdp, timeout=modal_timeout):
        return True
    # Click fired but no modal appeared. Retry once.
    if not attempt_click():
        return False
    return _wait_for_connect_modal(cdp, timeout=modal_timeout)


def _click_add_note_button(cdp: CDPConnection, sim: HumanSimulator) -> bool:
    """Click the 'Add a note' button in the connection request modal."""
    clicked_shadow = cdp.evaluate("""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const btn = deepNodes.find((node) => {
                const tag = (node.tagName || '').toLowerCase();
                if (tag !== 'button' && node.getAttribute('role') !== 'button') return false;
                if (!visible(node)) return false;
                const label = norm(node.getAttribute('aria-label') || node.innerText || node.textContent);
                return label === 'add a note';
            });
            if (!btn) return false;
            btn.click();
            return true;
        })()
    """)
    if bool(clicked_shadow):
        return True

    if sim.click_element(
        'button[aria-label*="Add a note"], '
        'button[aria-label*="add a note"], '
        'button[data-control-name*="add_note"], '
        'button[data-control-name*="invite"], '
        "button.artdeco-button--secondary"
    ):
        return True

    # Fallback: find a visible button by text and click it directly.
    clicked = cdp.evaluate("""
        (() => {
            const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const node of nodes) {
                const text = (node.innerText || node.textContent || '').trim().toLowerCase();
                if (!text.includes('add a note')) continue;
                const rect = node.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                node.click();
                return true;
            }
            return false;
        })()
    """)
    return bool(clicked)


def _type_connection_note(cdp: CDPConnection, sim: HumanSimulator, note: str):
    """Type a connection note into the modal textarea."""
    # Find and focus the textarea
    focused = cdp.evaluate("""
        (() => {
            const ta = document.querySelector(
                'textarea[name="message"], textarea#custom-message, ' +
                'textarea[placeholder*="Add a note"], textarea.connect-button-send-invite__custom-message'
            );
            if (ta) { ta.focus(); ta.value = ''; return true; }
            return false;
        })()
    """)
    if focused:
        human_delay(0.3, 0.8)
        sim.type_text(note)


def _click_send_button(cdp: CDPConnection, sim: HumanSimulator) -> bool:
    """Click the Send / Send now button in the connection modal."""
    # Only click inside the visible invite modal. A global primary-button selector
    # can hit unrelated page controls and create a false "sent" path.
    clicked = cdp.evaluate("""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const modalRoots = deepNodes.filter((el) => {
                const id = el.getAttribute('data-test-modal-id') || '';
                const role = el.getAttribute('role') || '';
                const text = norm(el.innerText || el.textContent || '');
                return id === 'send-invite-modal' ||
                    (role === 'dialog' && text.includes('add a note to your invitation'));
            }).filter(visible);
            const scope = modalRoots[modalRoots.length - 1] || null;
            const buttons = (scope ?
                Array.from(scope.querySelectorAll('button, [role="button"]')) :
                deepNodes.filter((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    return tag === 'button' || el.getAttribute('role') === 'button';
                })
            ).filter(visible);
            const preferred = buttons.find((btn) => {
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return false;
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label === 'send without a note';
            });
            if (preferred) {
                preferred.click();
                return true;
            }
            for (const btn of buttons) {
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                if (!(label === 'send' || label === 'send now' || label.startsWith('send '))) continue;
                const rect = btn.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                btn.click();
                return true;
            }
            return false;
        })()
    """)
    return bool(clicked)
