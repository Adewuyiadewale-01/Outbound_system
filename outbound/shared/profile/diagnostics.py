"""Profile-mapper diagnostic dumps (debug artifacts only).

Extracted from helpers/linkedin_helper.py during the linkedin_helper carve
(see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S14). Pure move except the debug
path anchor rewrite (architecture §8) — resolved path unchanged.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.shared.browser.connection import CDPConnection

ROOT = Path(__file__).resolve().parents[3]


def _safe_debug_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip())
    return slug.strip("_")[:80] or "profile"


def _write_profile_mapper_dump(
    cdp: CDPConnection,
    profile_url: str,
    result: dict[str, Any],
    navigation_started_at: float,
    readiness_fired_at: str,
) -> None:
    """Write diagnostic-only top-card dumps without changing mapper behavior."""
    try:
        dump_taken_at = datetime.now().isoformat(timespec="milliseconds")
        raw = cdp.evaluate(
            """
            (() => {
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
                const topText = norm((topCard && topCard.innerText) || '');
                return JSON.stringify({
                    url: location.href,
                    readyState: document.readyState,
                    profileName: norm(h1?.innerText || '') || topText.split(' · ')[0].split('\\n')[0],
                    topCardOuterHTML: topCard ? topCard.outerHTML : '',
                    bodyTextSample: ((document.body && document.body.innerText) || '').slice(0, 2000),
                });
            })()
        """,
            timeout=10,
        )
        dump = json.loads(raw) if raw else {}
        debug_dir = str(ROOT / "debug")
        os.makedirs(debug_dir, exist_ok=True)
        lead_slug = _safe_debug_slug(
            dump.get("profileName") or profile_url.rstrip("/").split("/")[-1]
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base = os.path.join(debug_dir, f"{lead_slug}_{timestamp}")
        html_path = f"{base}.html"
        json_path = f"{base}.json"
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(str(dump.get("topCardOuterHTML", "")))
        payload = {
            "profile_url": profile_url,
            "current_url": dump.get("url"),
            "profile_name": dump.get("profileName"),
            "readiness_fired_at": readiness_fired_at,
            "top_card_ready_at": result.get("top_card_ready_at"),
            "dump_taken_at": dump_taken_at,
            "ms_since_navigation": round((time.time() - navigation_started_at) * 1000),
            "classification": result.get("state"),
            "direct_buttons_seen": result.get("direct_buttons_seen", []),
            "more_button_seen": result.get("more_button_seen"),
            "more_clicked_at": result.get("more_menu", {}).get("more_clicked_at")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "more_menu_items_at_read": result.get("more_menu", {}).get("menu_items_count")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "more_scope_found": result.get("more_menu", {}).get("scope_found")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "result": result,
            "html_path": html_path,
            "body_text_sample": dump.get("bodyTextSample", ""),
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        result["debug_dump"] = {"html_path": html_path, "json_path": json_path}
    except Exception as exc:
        result["debug_dump_error"] = str(exc)
