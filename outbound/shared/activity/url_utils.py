"""Activity URL helpers and pure activity-page predicates.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S10). Pure move.
"""

import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse

from outbound.shared.activity.tab_policy import ACTIVITY_DISALLOWED_PATH_RE
from outbound.shared.browser.connection import CDPConnection


def _current_activity_url_is_disallowed(cdp: CDPConnection) -> bool:
    """Return True when LinkedIn has navigated to an activity tab we never use."""
    try:
        current_url = str(cdp.evaluate("window.location.href") or "")
    except Exception:
        return False
    return bool(ACTIVITY_DISALLOWED_PATH_RE.search(current_url))


def _activity_url_for_tab(profile_base: str, tab_key: str) -> str:
    endpoint = "all" if tab_key == "posts" else tab_key
    return f"{profile_base}/recent-activity/{endpoint}/"


def canonicalize_linkedin_profile_url(value: Any) -> str:
    """Return the canonical public URL for a recognizable LinkedIn /in/ profile."""
    raw = re.sub(r"\s+", "", str(value or "").strip())
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        raw = "https://" + raw

    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return ""

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() != "in":
        return ""

    slug = unquote(path_parts[1]).strip()
    if not slug or "/" in slug or "\\" in slug or any(char.isspace() for char in slug):
        return ""
    # LinkedIn public identifiers are URL path segments. Encode any harmless
    # non-ASCII character while keeping the characters used by vanity slugs.
    encoded_slug = quote(slug, safe="-._~")
    return f"https://www.linkedin.com/in/{encoded_slug}"


def _activity_profile_slug(profile_url: str) -> str:
    match = re.search(r"/in/([^/?#]+)/?", str(profile_url or ""), re.IGNORECASE)
    return (match.group(1) if match else "").lower()


def _activity_destination_matches(current_url: str, profile_base: str, tab_key: str) -> bool:
    slug = _activity_profile_slug(profile_base)
    if not slug:
        return False
    endpoint = "all" if tab_key == "posts" else tab_key
    path = re.sub(r"^https?://[^/]+", "", str(current_url or "").lower())
    path = re.split(r"[?#]", path, maxsplit=1)[0].rstrip("/")
    expected = f"/in/{slug}/recent-activity/{endpoint}"
    return path == expected


def _page_still_loading(cdp: CDPConnection) -> bool:
    """True when the page reports active loading or in-flight resource fetches."""
    try:
        raw = cdp.evaluate(
            "JSON.stringify({rs: document.readyState, "
            "pending: performance.getEntriesByType('resource').filter(r => !r.responseEnd).length})",
            timeout=5,
        )
        state = json.loads(raw) if raw else {}
        return state.get("rs") == "loading" or int(state.get("pending", 0) or 0) > 2
    except Exception:
        return False
