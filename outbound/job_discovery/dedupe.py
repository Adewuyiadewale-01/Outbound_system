"""Candidate identity + dedupe ported from ``job_discovery/src/dedupe.mjs``.

Dedupes search results *before* any listing is opened: in-run duplicates merge
query provenance; already-verified unchanged records are skipped, while
changed hashes, unsure signals, or due rechecks queue for hydration.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import TypedDict
from urllib.parse import urlsplit

from outbound.job_discovery.urls import (
    canonicalize_url,
    find_ats_job_id,
    normalized_text,
    stable_hash,
)


class Candidate(TypedDict):
    jobId: str
    canonicalUrl: str
    atsJobId: str | None
    title: str
    snippet: str
    displayLink: str
    platform: str
    role: str
    field: str
    sourceQueries: list[str]
    queryType: str
    resultHash: str
    fallbackFingerprint: str


def to_candidate(result: dict, query: dict) -> Candidate:
    """Port of ``toCandidate``: identity chain ATS id -> canonical URL -> fallback."""
    canonical_url = canonicalize_url(result["link"])
    ats_job_id = find_ats_job_id(canonical_url)
    title = result.get("title") or "Untitled job"
    snippet = result.get("snippet") or ""
    fallback = "|".join(
        [
            query.get("platform", ""),
            normalized_text(result.get("company") or ""),
            normalized_text(title),
            normalized_text(result.get("location") or ""),
        ]
    )
    identity = f"ats:{ats_job_id}" if ats_job_id else f"url:{canonical_url}"
    display_link = result.get("displayLink") or urlsplit(canonical_url).hostname or ""
    return {
        "jobId": stable_hash(identity),
        "canonicalUrl": canonical_url,
        "atsJobId": ats_job_id,
        "title": title,
        "snippet": snippet,
        "displayLink": display_link,
        "platform": query.get("platform", ""),
        "role": query.get("role", ""),
        "field": "design" if query.get("field") == "design" else "engineering",
        "sourceQueries": [query.get("id", "")],
        "queryType": query.get("type", ""),
        "resultHash": stable_hash(f"{title}|{snippet}|{canonical_url}"),
        "fallbackFingerprint": stable_hash(fallback),
    }


def _parse_checked_ms(value: object) -> float:
    """Parse an ISO timestamp to epoch ms; NaN when unparsable (JS Date.parse)."""
    if not isinstance(value, str):
        return math.nan
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return math.nan
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000


def dedupe_candidates(
    candidates: list[Candidate],
    known_jobs: dict[str, dict] | None = None,
    *,
    recheck_after_ms: float = math.inf,
    now_ms: int | None = None,
) -> dict[str, list]:
    """Port of ``dedupeCandidates``.

    Returns ``uniqueCandidates`` (provenance merged, insertion order kept) and
    ``hydrationQueue`` (records that need their listing opened/verified).
    """
    known = known_jobs or {}
    now = int(time.time() * 1000) if now_ms is None else now_ms
    run: dict[str, Candidate] = {}
    hydration_queue: list[Candidate] = []
    for candidate in candidates:
        existing = run.get(candidate["jobId"])
        if existing is None:
            for item in run.values():
                if item["fallbackFingerprint"] == candidate["fallbackFingerprint"]:
                    existing = item
                    break
        if existing is not None:
            existing["sourceQueries"] = list(
                dict.fromkeys(existing["sourceQueries"] + candidate["sourceQueries"])
            )
            continue
        run[candidate["jobId"]] = candidate
        known_job = known.get(candidate["jobId"])
        if known_job is None:
            for job in known.values():
                if job.get("fallbackFingerprint") == candidate["fallbackFingerprint"]:
                    known_job = job
                    break
        if known_job is not None and known_job.get("verificationCheckedAt"):
            checked_at = _parse_checked_ms(known_job["verificationCheckedAt"])
        else:
            checked_at = 0
        recheck_due = known_job is not None and (
            not math.isfinite(checked_at) or now - checked_at >= recheck_after_ms
        )
        if (
            known_job is None
            or known_job.get("resultHash") != candidate["resultHash"]
            or known_job.get("juniorStatus") == "unsure"
            or known_job.get("remoteStatus") == "unsure"
            or known_job.get("pythonStatus") == "unsure"
            or recheck_due
        ):
            hydration_queue.append(candidate)
    return {"uniqueCandidates": list(run.values()), "hydrationQueue": hydration_queue}
