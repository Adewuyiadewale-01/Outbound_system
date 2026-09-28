"""Job-discovery defaults and settings normalization.

Ported from ``job_discovery/config/defaults.mjs`` + ``normalizeSettings`` in
``job_discovery/src/config.mjs``. Key names mirror the JS/Sheets wire contract.
"""

from __future__ import annotations

import math
import re
from typing import Any
from zoneinfo import ZoneInfo

PLATFORMS: list[dict[str, Any]] = [
    {"name": "Ashby", "siteTarget": "site:jobs.ashbyhq.com", "enabled": True},
    {"name": "Greenhouse", "siteTarget": "site:greenhouse.io", "enabled": True},
    {
        "name": "Lever",
        "siteTarget": "(site:jobs.lever.co OR site:jobs.eu.lever.co)",
        "enabled": True,
    },
    {"name": "Workable", "siteTarget": "site:apply.workable.com", "enabled": True},
    {"name": "SmartRecruiters", "siteTarget": "site:jobs.smartrecruiters.com", "enabled": True},
    {"name": "Teamtailor", "siteTarget": "site:career.teamtailor.com", "enabled": True},
    {"name": "Recruitee", "siteTarget": "site:recruitee.com", "enabled": True},
    {"name": "Pinpoint", "siteTarget": "site:pinpointhq.com", "enabled": True},
    {"name": "Breezy HR", "siteTarget": "site:breezy.hr", "enabled": True},
    {"name": "Comeet", "siteTarget": "site:comeet.co", "enabled": True},
    {"name": "Personio", "siteTarget": "site:jobs.personio.com", "enabled": True},
    {"name": "Workday", "siteTarget": "site:myworkdayjobs.com", "enabled": True},
]

ROLES: list[dict[str, Any]] = [
    {
        "name": "Python Developer",
        "field": "engineering",
        "junior": [
            "junior python developer",
            "junior python engineer",
            "jr python developer",
            "associate python developer",
            "associate python engineer",
            "entry level python developer",
            "python engineer I",
            "python engineer 1",
        ],
        "unfiltered": [
            "python developer",
            "python engineer",
            "software engineer python",
            "python backend engineer",
            "backend engineer python",
        ],
        "strongSignals": ["python"],
    },
    {
        "name": "Backend Developer",
        "field": "engineering",
        "junior": [
            "junior backend engineer",
            "junior backend developer",
            "jr backend engineer",
            "associate backend engineer",
            "associate backend developer",
            "entry level backend engineer",
            "backend engineer I",
            "backend engineer 1",
        ],
        "unfiltered": [
            "backend engineer",
            "backend developer",
            "software engineer backend",
            "backend software engineer",
        ],
        "strongSignals": [],
    },
    {
        "name": "Automation Developer",
        "field": "engineering",
        "junior": [
            "junior automation engineer",
            "junior automation developer",
            "jr automation engineer",
            "associate automation engineer",
            "associate automation developer",
            "entry level automation engineer",
            "automation engineer I",
            "automation engineer 1",
        ],
        "unfiltered": [
            "automation engineer",
            "automation developer",
            "software engineer automation",
        ],
        "strongSignals": [],
    },
    {
        "name": "Software Engineer",
        "field": "engineering",
        "junior": [
            "junior software engineer",
            "jr software engineer",
            "associate software engineer",
            "entry level software engineer",
            "graduate software engineer",
            "new grad software engineer",
            "software engineer I",
            "software engineer 1",
        ],
        "unfiltered": ["software engineer", "software developer"],
        "strongSignals": [],
    },
    {
        "name": "Full Stack Developer",
        "field": "engineering",
        "junior": [
            "junior full stack engineer",
            "junior full stack developer",
            "junior fullstack engineer",
            "jr full stack engineer",
            "associate full stack engineer",
            "entry level full stack engineer",
            "full stack engineer I",
            "full stack engineer 1",
        ],
        "unfiltered": [
            "full stack engineer",
            "full stack developer",
            "fullstack engineer",
            "fullstack developer",
        ],
        "strongSignals": [],
    },
    {
        "name": "Product Designer",
        "field": "design",
        "junior": [
            "junior product designer",
            "associate product designer",
            "entry level product designer",
            "product designer I",
            "product designer 1",
            "graduate product designer",
        ],
        "unfiltered": ["product designer", "digital product designer", "product design"],
        "strongSignals": [],
    },
    {
        "name": "UI/UX Designer",
        "field": "design",
        "junior": [
            "junior ui ux designer",
            "junior ux designer",
            "junior ui designer",
            "associate ux designer",
            "entry level ux designer",
            "ux designer I",
            "ui ux designer I",
        ],
        "unfiltered": [
            "ui ux designer",
            "ux ui designer",
            "ux designer",
            "ui designer",
            "user experience designer",
        ],
        "strongSignals": [],
    },
    {
        "name": "Web Designer",
        "field": "design",
        "junior": [
            "junior web designer",
            "associate web designer",
            "entry level web designer",
            "web designer I",
            "web designer 1",
        ],
        "unfiltered": ["web designer", "website designer", "digital designer"],
        "strongSignals": [],
    },
]

DEFAULTS: dict[str, Any] = {
    "automationEnabled": False,
    "dailyRunTime": "08:00",
    "timezone": "Africa/Lagos",
    "maxQueriesPerRun": 180,
    "maxListingsPerRun": 180,
    "minListingDelayMs": 8_000,
    "maxListingDelayMs": 15_000,
    "minListingDwellMs": 4_000,
    "maxListingDwellMs": 7_000,
    "minPageDelayMs": 15_000,
    "maxPageDelayMs": 30_000,
    "minSearchPageCooldownMs": 180_000,
    "maxSearchPageCooldownMs": 300_000,
    "searchPageBurstSize": 3,
    "minQueryDelayMs": 90_000,
    "maxQueryDelayMs": 180_000,
    "queryBurstSize": 3,
    "cooldownMinMs": 600_000,
    "cooldownMaxMs": 900_000,
    # Zero disables the total-page ceiling. Query completion is instead driven
    # by Google's next-page signal, the two-thin-pages rule, and the session
    # timer -- same meaning as the reference implementation.
    "maxPagesPerQuery": 0,
    "maxSearchMinutesPerQuery": 75,
    "searchRetryAttempts": 2,
    "listingRetryAttempts": 3,
    "retryBaseDelayMs": 2_000,
    "recheckAfterDays": 7,
    "staleLockMinutes": 360,
    "maxConsecutiveErrorsPerPlatform": 3,
    "closeAfterMisses": 3,
}

_RANGE_PAIRS: list[tuple[str, str]] = [
    ("minListingDelayMs", "maxListingDelayMs"),
    ("minListingDwellMs", "maxListingDwellMs"),
    ("minPageDelayMs", "maxPageDelayMs"),
    ("minSearchPageCooldownMs", "maxSearchPageCooldownMs"),
    ("minQueryDelayMs", "maxQueryDelayMs"),
    ("cooldownMinMs", "cooldownMaxMs"),
]

_MIN_ONE_KEYS = [
    "maxQueriesPerRun",
    "maxListingsPerRun",
    "searchPageBurstSize",
    "queryBurstSize",
    "maxSearchMinutesPerQuery",
    "searchRetryAttempts",
    "listingRetryAttempts",
    "staleLockMinutes",
    "closeAfterMisses",
]


def _to_number(value: Any) -> float:
    """Mimic JS ``Number(value)``; unparsable values become NaN."""
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def _number_or(value: Any, fallback: float) -> float:
    """Mimic JS ``Number(value) || fallback`` (0 and NaN are falsy)."""
    parsed = _to_number(value)
    if math.isnan(parsed) or parsed == 0:
        return fallback
    return parsed


def normalize_settings(source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Port of ``normalizeSettings``: defaults + input, clamped and validated."""
    settings = {**DEFAULTS, **(source or {})}
    for minimum, maximum in _RANGE_PAIRS:
        settings[minimum] = max(0, _number_or(settings[minimum], 0))
        settings[maximum] = max(settings[minimum], _number_or(settings[maximum], 0))
    for key in _MIN_ONE_KEYS:
        settings[key] = max(1, math.floor(_number_or(settings[key], DEFAULTS[key])))
    settings["maxPagesPerQuery"] = max(0, math.floor(_number_or(settings["maxPagesPerQuery"], 0)))
    if not re.fullmatch(r"\d{2}:\d{2}", str(settings["dailyRunTime"])):
        raise ValueError("dailyRunTime must use HH:MM format")
    ZoneInfo(str(settings["timezone"]))
    return settings
