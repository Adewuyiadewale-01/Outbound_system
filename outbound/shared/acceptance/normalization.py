"""Acceptance-feed URL and name normalization.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S20). Pure move.
"""

import re


def _normalize_linkedin_profile_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    normalized = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    return re.sub(r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", normalized)


def _normalize_sent_invitation_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
