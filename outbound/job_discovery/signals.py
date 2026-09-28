"""Signal verification ported from ``job_discovery/src/signals.mjs``.

Verifies junior / remote / python evidence and routes ambiguity to review:
- seniority is only conclusive when it qualifies the job title;
- remote-only is never reported when hybrid or onsite wording co-occurs;
- review reason is "Conflicting signals" or "Signal could not be confirmed".
"""

from __future__ import annotations

import re
from typing import Any, TypedDict


class SignalResult(TypedDict):
    juniorStatus: str
    remoteStatus: str
    pythonStatus: str
    evidenceText: str
    score: int
    reviewReason: str


_DEFAULT_JUNIOR_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bjunior\b",
        r"\bjr\.?\b",
        r"\bassociate\b",
        r"\bentry[ -]level\b",
        r"\bnew grad\b",
        r"\bgraduate\b",
        r"\b(?:engineer|developer)\s+(?:i|1)\b",
        r"\bearly career\b",
    )
]
_DEFAULT_SENIOR_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsenior\b",
        r"\bstaff\b",
        r"\bprincipal\b",
        r"\blead\b",
        r"\bmanager\b",
        r"\bdirector\b",
    )
]
_DEFAULT_REMOTE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"\bremote\b", r"\bwork from home\b", r"\bdistributed\b", r"\banywhere\b")
]
_DEFAULT_HYBRID_PATTERNS = [re.compile(r"\bhybrid\b", re.IGNORECASE)]
_DEFAULT_ONSITE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"\bno remote\b", r"\bon[ -]site\b", r"\bin office\b")
]
_DEFAULT_PYTHON_PATTERNS = [re.compile(r"\bpython\b", re.IGNORECASE)]
_ENGINEER_LEVEL_ONE = re.compile(r"\b(?:engineer|developer)\s+(?:i|1)\b", re.IGNORECASE)


def _escape_term(value: str) -> str:
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", value)


def _patterns_from_terms(
    terms: Any, fallback: list[re.Pattern], *, level_one: bool = False
) -> list[re.Pattern]:
    """Sheet-provided rules terms -> regexes (reference parity, including the
    literal backslash-space handling in the reference's term escaping)."""
    if not isinstance(terms, list) or not terms:
        return fallback
    patterns: list[re.Pattern] = []
    for raw in terms:
        term = str(raw).strip()
        if not term:
            continue
        if level_one and re.fullmatch(r"[iI1]", term):
            patterns.append(_ENGINEER_LEVEL_ONE)
            continue
        body = _escape_term(term).replace("\\ ", "[ -]")
        patterns.append(re.compile(r"\b" + body + r"\b", re.IGNORECASE))
    return patterns if patterns else fallback


def _find_evidence(text: str, patterns: list[re.Pattern]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _compact_evidence(text: str, term: str | None) -> str:
    if not term:
        return ""
    index = text.lower().find(term.lower())
    segment = text[max(0, index - 90) : index + len(term) + 140]
    return re.sub(r"\s+", " ", segment).strip()


def verify_signals(job: dict, rules: dict | None = None) -> SignalResult:
    """Port of ``verifySignals`` for one listing's title/description/location."""
    rules = rules or {}
    title = job.get("title") or ""
    description = job.get("description") or ""
    location = job.get("location") or ""
    role = job.get("role") or ""

    active_junior = _patterns_from_terms(
        rules.get("junior"), _DEFAULT_JUNIOR_PATTERNS, level_one=True
    )
    active_senior = _patterns_from_terms(rules.get("senior"), _DEFAULT_SENIOR_PATTERNS)
    active_remote = _patterns_from_terms(rules.get("remote"), _DEFAULT_REMOTE_PATTERNS)
    # Older configurations used one non-remote rule. Keep those terms working,
    # but split hybrid from onsite so the Sheet reports the stronger outcome.
    legacy_non_remote = rules.get("nonRemote") if isinstance(rules.get("nonRemote"), list) else []
    active_hybrid = [
        *_patterns_from_terms(rules.get("hybrid"), _DEFAULT_HYBRID_PATTERNS),
        *_patterns_from_terms(
            [term for term in legacy_non_remote if re.search("hybrid", str(term), re.IGNORECASE)],
            [],
        ),
    ]
    active_onsite = [
        *_patterns_from_terms(rules.get("onsite"), _DEFAULT_ONSITE_PATTERNS),
        *_patterns_from_terms(
            [
                term
                for term in legacy_non_remote
                if not re.search("hybrid", str(term), re.IGNORECASE)
            ],
            [],
        ),
    ]
    active_python = _patterns_from_terms(rules.get("python"), _DEFAULT_PYTHON_PATTERNS)

    text = f"{title}\n{location}\n{description}"
    title_junior = _find_evidence(title, active_junior)
    title_senior = _find_evidence(title, active_senior)
    junior = title_junior or _find_evidence(description, active_junior)
    # Seniority is only conclusive when it qualifies the job title. Mentions of a
    # senior colleague in the description must not downgrade a junior opening.
    senior = title_senior
    remote = _find_evidence(text, active_remote)
    hybrid = _find_evidence(text, active_hybrid)
    onsite = _find_evidence(text, active_onsite)
    python = _find_evidence(text, active_python)
    title_appears_partial = len(title.strip()) < 5 or bool(
        re.search(r"untitled job", title, re.IGNORECASE)
    )

    junior_status = (
        "conflicting"
        if title_junior and title_senior
        else "verified"
        if junior
        else "senior_verified"
        if senior
        else "unsure"
        if title_appears_partial
        else "not_found"
    )
    # A job cannot be reported as remote-only when the same page explicitly says
    # hybrid or onsite. Mixed wording is retained for review rather than guessed.
    remote_status = (
        "conflicting"
        if remote and (hybrid or onsite)
        else "hybrid_verified"
        if hybrid
        else "onsite_verified"
        if onsite
        else "verified"
        if remote
        else "unsure"
        if title_appears_partial
        else "not_found"
    )
    python_required = bool(re.search(r"python", role, re.IGNORECASE))
    python_status = (
        "verified"
        if python
        else "unsure"
        if python_required and title_appears_partial
        else "not_found"
    )
    verified_signals = sum(
        status == "verified" for status in (junior_status, remote_status, python_status)
    )

    statuses = (junior_status, remote_status, python_status)
    return {
        "juniorStatus": junior_status,
        "remoteStatus": remote_status,
        "pythonStatus": python_status,
        "evidenceText": " | ".join(
            _compact_evidence(text, term)
            for term in (junior or senior, remote, hybrid or onsite, python)
            if term
        ),
        "score": verified_signals * 25
        + (20 if junior_status == "verified" else 0)
        + (15 if remote_status == "verified" else 0),
        "reviewReason": (
            "Conflicting signals"
            if "conflicting" in statuses
            else "Signal could not be confirmed"
            if "unsure" in statuses
            else ""
        ),
    }
