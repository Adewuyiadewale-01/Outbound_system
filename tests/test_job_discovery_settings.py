"""Tests for job-discovery settings defaults + normalization."""

from __future__ import annotations

import pytest

from outbound.job_discovery.settings import DEFAULTS, PLATFORMS, ROLES, normalize_settings


def test_defaults_fill_and_validate() -> None:
    settings = normalize_settings()
    assert settings["dailyRunTime"] == "08:00"
    assert settings["timezone"] == "Africa/Lagos"
    assert settings["maxQueriesPerRun"] == 180
    assert settings["minListingDelayMs"] == 8_000
    assert settings["maxListingDelayMs"] == 15_000


def test_range_pairs_clamp_minimum_above_maximum() -> None:
    settings = normalize_settings({"minListingDelayMs": 20_000, "maxListingDelayMs": 10_000})
    assert settings["minListingDelayMs"] == 20_000
    assert settings["maxListingDelayMs"] == 20_000


def test_numeric_fields_fall_back_and_floors() -> None:
    settings = normalize_settings(
        {"maxQueriesPerRun": "abc", "maxListingsPerRun": "12.9", "maxPagesPerQuery": -5}
    )
    assert settings["maxQueriesPerRun"] == DEFAULTS["maxQueriesPerRun"]
    assert settings["maxListingsPerRun"] == 12
    assert settings["maxPagesPerQuery"] == 0


def test_invalid_daily_run_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_settings({"dailyRunTime": "8:00"})


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(KeyError):
        normalize_settings({"timezone": "Not/AZone"})


def test_static_inventory_sizes() -> None:
    assert len(PLATFORMS) == 12
    assert len(ROLES) == 8
    assert sum(role["field"] == "engineering" for role in ROLES) == 5


def test_adaptive_depth_defaults() -> None:
    settings = normalize_settings()
    assert settings["adaptiveDepthEnabled"] is True
    assert settings["shallowQuietThreshold"] == 10
    assert settings["shallowQuietPages"] == 3
    assert settings["sparsePageResults"] == 2
    assert settings["sparsePages"] == 3
    assert settings["fullCheckIntervalDays"] == 7
    assert settings["deepBudgetMinutesPerRun"] == 180
    assert settings["parkAfterZeroFullChecks"] == 4
    assert settings["activatedQueries"] == []


def test_adaptive_depth_normalization() -> None:
    settings = normalize_settings(
        {
            "shallowQuietPages": "0",
            "activatedQueries": "nope",
            "adaptiveDepthEnabled": "false",
            "sparsePages": "2.9",
            "shallowQuietThreshold": "abc",
        }
    )
    # falsy values fall back to the default (shared numeric convention), then clamp to >= 1
    assert settings["shallowQuietPages"] == 3
    assert settings["activatedQueries"] == []
    assert settings["adaptiveDepthEnabled"] is False
    assert settings["sparsePages"] == 2
    assert settings["shallowQuietThreshold"] == 10
