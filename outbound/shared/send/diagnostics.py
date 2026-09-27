"""Send-path diagnostics bundle writer.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S17). Pure move.
"""

import json
import os
from datetime import datetime
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.quota import DIAGNOSTIC_DIR, _ensure_diagnostic_dir, _safe_slug


def _write_send_diagnostics(
    cdp: CDPConnection,
    profile_url: str,
    stage: str,
    exc: Exception,
    result: dict[str, Any],
) -> str:
    """Persist a diagnostic bundle for unexpected send-connection failures."""
    _ensure_diagnostic_dir()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _safe_slug(profile_url.rstrip("/").split("/")[-1] or "profile")
    base = os.path.join(DIAGNOSTIC_DIR, f"{ts}-{slug}-{_safe_slug(stage)}")

    payload: dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "profile_url": profile_url,
        "error": str(exc),
        "result_so_far": result,
    }

    try:
        payload["current_url"] = cdp.get_current_url()
    except Exception as current_exc:
        payload["current_url_error"] = str(current_exc)

    try:
        payload["page_title"] = cdp.evaluate("document.title") or ""
    except Exception as title_exc:
        payload["page_title_error"] = str(title_exc)

    try:
        payload["page_state"] = cdp.evaluate("""
            (() => JSON.stringify({
                readyState: document.readyState,
                url: window.location.href,
                title: document.title,
                buttons: Array.from(document.querySelectorAll('button')).slice(0, 25).map((btn) => {
                    const rect = btn.getBoundingClientRect();
                    return {
                        text: (btn.innerText || '').trim().slice(0, 80),
                        ariaLabel: btn.getAttribute('aria-label') || '',
                        disabled: !!btn.disabled,
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        visible: !!(rect.width && rect.height)
                    };
                }),
            }))()
        """)
    except Exception as page_exc:
        payload["page_state_error"] = str(page_exc)

    json_path = f"{base}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    try:
        screenshot_b64 = cdp.capture_screenshot_base64()
        if screenshot_b64:
            import base64

            png_path = f"{base}.png"
            with open(png_path, "wb") as fh:
                fh.write(base64.b64decode(screenshot_b64))
            payload["screenshot_path"] = png_path
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
    except Exception:
        pass

    return json_path
