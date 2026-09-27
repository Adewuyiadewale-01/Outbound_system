"""Text, name, URL, and title-scoring helpers for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S2). Pure move.
"""

import re
import unicodedata
import urllib.parse
from typing import Any

from outbound.leads.config import EXEC_TITLE_PATTERNS


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def strip_accents(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_url(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    raw = raw.replace(" ", "")
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    return raw


def meaningful_name_parts(name: str) -> list[str]:
    parts = re.split(r"[^a-zA-Z0-9]+", name.lower())
    blocked = {"de", "den", "der", "van", "von", "the", "and", "of", "mr", "mrs", "ms"}
    return [p for p in parts if len(p) >= 4 and p not in blocked]


def token_overlap_score(left: str, right: str) -> float:
    left_tokens = set(meaningful_name_parts(left))
    right_tokens = set(meaningful_name_parts(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def linkedin_url_name_score(name: str, url: str) -> float:
    url_norm = normalize_key(urllib.parse.unquote(url))
    parts = meaningful_name_parts(name)
    if not parts or not url_norm:
        return 0.0
    hits = sum(1 for part in parts if part in url_norm)
    return hits / len(parts)


def is_linkedin_profile_url(url: str) -> bool:
    return bool(re.search(r"linkedin\.[a-z.]+/in/", normalize_url(url), flags=re.I))


def title_weight(text: str) -> int:
    lowered = text.lower()
    best = 0
    for title, weight in EXEC_TITLE_PATTERNS:
        title_pattern = r"\b" + re.escape(title.lower()).replace(r"\ ", r"\s+") + r"\b"
        if re.search(title_pattern, lowered):
            best = max(best, weight)
    return best


def compact_company_name(company_name: str) -> str:
    compact = re.split(r"\s+\|\s+", clean_text(company_name), maxsplit=1)[0]
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact or clean_text(company_name)


def clean_person_name(name: str) -> str:
    name = re.sub(r"\b(LinkedIn|Profile|About|Team|Leadership|People|Company)\b", "", name)
    return clean_text(name.strip(" -|,.;:"))


def clean_role_for_person(name: str, role: str) -> str:
    cleaned = clean_text(role)
    if not cleaned:
        return ""
    name_key = normalize_key(name)
    role_key = normalize_key(cleaned)
    if name_key and role_key.startswith(name_key + " "):
        words_to_drop = len(name_key.split())
        return clean_text(" ".join(cleaned.split()[words_to_drop:]))
    if role_key == name_key:
        return ""
    return cleaned


def is_plausible_person_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 5:
        return False
    blocked = {"Chief Executive", "Managing Director", "Company LinkedIn"}
    suffixes = {
        "limited",
        "ltd",
        "bv",
        "b.v",
        "inc",
        "llc",
        "plc",
        "capital",
        "management",
        "company",
    }
    lowered_parts = {part.lower().strip(".") for part in parts}
    if name in blocked or lowered_parts & suffixes:
        return False
    return sum(1 for part in parts if part[:1].isupper()) >= 2
