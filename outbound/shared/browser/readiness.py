"""Page-readiness gates and reliability-first navigation fallbacks.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S7). Pure move.
"""

import json
import random
import time
from typing import TYPE_CHECKING, Any

from outbound.shared.browser.connection import CDPConnection

if TYPE_CHECKING:
    from outbound.shared.human.simulator import HumanSimulator


def _wait_for_page_ready(
    cdp: CDPConnection,
    expected_selector: str | None = None,
    timeout: float = 20,
    poll_interval: float = 1.0,
    stable_for: float = 0.0,
    ignored_overlays: set | None = None,
    ignored_loaders: set | None = None,
) -> dict[str, Any]:
    """Wait until the page is interactive and optionally until an expected selector appears.

    Checks:
    1. document.readyState is 'complete' or 'interactive'
    2. No overlay/modal blocking interaction (cookie consent, interstitials)
    3. Expected selector is present in the DOM (if provided)
    4. LinkedIn skeleton/loading elements have cleared
    5. Page text length is stable for stable_for seconds (if provided)

    Returns dict with 'ready' bool, 'readyState', 'overlay_detected', 'selector_found'.
    """
    start = time.time()
    last_state: dict[str, Any] = {}
    stable_since: float | None = None
    last_text_len: int | None = None

    while time.time() - start < timeout:
        try:
            check = cdp.evaluate(
                """
                (() => {
                    const rs = document.readyState;

                    // Detect common LinkedIn overlays / modals
                    const overlaySelectors = [
                        '[class*="modal--overlay"]',
                        '[class*="cookie-consent"]',
                        '.artdeco-modal-overlay',
                        '[data-test-modal]',
                    ];
                    let overlay = null;
                    for (const sel of overlaySelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            overlay = sel;
                            break;
                        }
                    }

                    const loadingSelectors = [
                        '.artdeco-loader',
                        '.artdeco-spinner',
                        '.skeleton-loader',
                        '[class*="skeleton"]',
                        '[aria-busy="true"]',
                        '[data-test-id*="loading"]',
                    ];
                    let loading = null;
                    for (const sel of loadingSelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            loading = sel;
                            break;
                        }
                    }

                    return JSON.stringify({
                        readyState: rs,
                        overlay: overlay,
                        loading: loading,
                        textLen: (document.body && document.body.innerText || '').length,
                        url: window.location.href,
                    });
                })()
            """,
                timeout=8,
            )
        except Exception as exc:
            last_state = {
                "ready": False,
                "probe_error": str(exc),
                "elapsed": round(time.time() - start, 1),
            }
            time.sleep(poll_interval)
            continue
        info = json.loads(check) if check else {}
        ready_state = info.get("readyState", "")
        overlay = info.get("overlay")
        blocking_overlay = bool(overlay and overlay not in (ignored_overlays or set()))
        loading = info.get("loading")
        blocking_loading = bool(loading and loading not in (ignored_loaders or set()))
        text_len = int(info.get("textLen", 0) or 0)

        # readyState must be at least interactive
        page_ready = ready_state in ("interactive", "complete")

        # Check selector presence if requested
        selector_found = True
        if expected_selector and page_ready:
            try:
                found = cdp.evaluate(
                    f"!!document.querySelector({json.dumps(expected_selector)})",
                    timeout=8,
                )
                selector_found = bool(found)
            except Exception as exc:
                selector_found = False
                last_state = {
                    "ready": False,
                    "selector_probe_error": str(exc),
                    "elapsed": round(time.time() - start, 1),
                }
                time.sleep(poll_interval)
                continue

        base_ready = page_ready and selector_found and not blocking_overlay and not blocking_loading
        if base_ready and stable_for > 0:
            if last_text_len is None or abs(text_len - last_text_len) > 20:
                stable_since = time.time()
                last_text_len = text_len
            elif stable_since is None:
                stable_since = time.time()
            stable_ready = (time.time() - stable_since) >= stable_for
        else:
            stable_since = None
            last_text_len = text_len
            stable_ready = True

        last_state = {
            "ready": base_ready and stable_ready,
            "readyState": ready_state,
            "overlay_detected": overlay,
            "overlay_blocking": blocking_overlay,
            "loading_detected": loading,
            "loading_blocking": blocking_loading,
            "selector_found": selector_found if expected_selector else None,
            "text_len": text_len,
            "stable_for": stable_for if stable_for else None,
            "url": info.get("url", ""),
            "elapsed": round(time.time() - start, 1),
        }

        if last_state["ready"]:
            return last_state

        # If overlay detected, try to dismiss it
        if blocking_overlay and page_ready:
            try:
                cdp.evaluate(
                    """
                    (() => {
                        // Try dismissing cookie consent / generic modals
                        const dismissBtns = document.querySelectorAll(
                            '[class*="cookie"] button[action-type="ACCEPT"], ' +
                            '.artdeco-modal__dismiss, ' +
                            'button[data-test-modal-close-btn], ' +
                            'button[aria-label="Dismiss"], button[aria-label="Close"]'
                        );
                        if (dismissBtns.length > 0) dismissBtns[0].click();
                    })()
                """,
                    timeout=8,
                )
            except Exception:
                pass

        time.sleep(poll_interval)

    # Timed out
    last_state["ready"] = False
    last_state["timeout"] = True
    return last_state


def _wait_for_linkedin_ready(
    cdp: CDPConnection,
    expected_selector: str | None = None,
    timeout: float = 45,
    stable_for: float = 1.5,
    ignored_overlays: set | None = None,
    ignored_loaders: set | None = None,
) -> dict[str, Any]:
    """Wait for LinkedIn's SPA shell and expected content to finish rendering."""
    return _wait_for_page_ready(
        cdp,
        expected_selector=expected_selector,
        timeout=timeout,
        poll_interval=1.0,
        stable_for=stable_for,
        ignored_overlays=ignored_overlays,
        ignored_loaders=ignored_loaders,
    )


def _navigate_with_readiness(
    cdp: CDPConnection,
    url: str,
    expected_selector: str | None = None,
    nav_timeout: float = 30,
    ready_timeout: float = 20,
) -> dict[str, Any]:
    """Navigate to a URL using JS (no mouse events) and wait for readiness.

    Designed for the acceptance-check path where reliability > human-likeness.
    Falls back to window.location if cdp.navigate has issues.
    """
    result: dict[str, Any] = {"url": url, "method": "cdp_navigate"}

    try:
        cdp.navigate(url, wait_load=True, timeout=nav_timeout)
    except (TimeoutError, RuntimeError) as exc:
        # Fallback: JS-based navigation (no mouse/CDP Page dependency)
        result["method"] = "js_location_fallback"
        result["primary_error"] = str(exc)
        try:
            cdp.evaluate(f"window.location.href = '{url}'")
            # Give the page a moment to start loading
            time.sleep(2)
        except Exception as exc2:
            result["ready"] = False
            result["error"] = f"Both navigation methods failed: {exc}; {exc2}"
            return result

    # Wait for readiness
    readiness = _wait_for_page_ready(cdp, expected_selector, timeout=ready_timeout)
    result.update(readiness)
    return result


def _safe_scroll_or_js(
    cdp: CDPConnection,
    sim: "HumanSimulator",
    pixels: int,
) -> dict[str, Any]:
    """Attempt a human-sim scroll; fall back to JS scrollBy on timeout.

    Scoped to acceptance-check path where we prefer reliability over
    pixel-perfect human simulation.
    """
    result: dict[str, Any] = {"method": "human_sim"}
    try:
        sim.scroll(pixels)
    except TimeoutError as exc:
        result["method"] = "js_fallback"
        result["fallback_reason"] = str(exc)
        jitter = random.randint(-30, 30)
        cdp.evaluate(f"window.scrollBy(0, {pixels + jitter})")
    return result
