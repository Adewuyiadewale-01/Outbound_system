"""Tests for the ported scheduler, config helpers, and CLI wiring (Phase 6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from outbound.job_discovery import cli
from outbound.job_discovery.queries import configuration_from_rows
from outbound.job_discovery.scheduler import minute_of_day, run_if_due, scheduled_moment
from outbound.job_discovery.settings import (
    load_environment,
    load_local_settings,
    settings_from_control,
)


class MockStateStore:
    def __init__(self, state=None):
        self.state = state or {
            "jobs": {},
            "companies": {},
            "runs": {},
            "queryProgress": {},
            "liveTestUsage": {},
            "queryCursor": 0,
            "queryCycle": 1,
            "lastScheduledDate": "",
            "activeRunId": None,
        }
        self.locks: list = []

    def read(self):
        return self.state

    def write(self, next_state):
        self.state = next_state

    def acquire_run_lock(self, run_id, stale_after_ms=None):
        self.locks.append(run_id)
        return True

    def heartbeat_run_lock(self, run_id):
        return None

    def release_run_lock(self):
        return None


class EmptyProvider:
    def search(self, query, options=None):
        return []

    def close(self):
        return None


def _full_settings() -> dict:
    return {
        "automationEnabled": True,
        "timezone": "Africa/Lagos",
        "dailyRunTime": "08:00",
        "maxQueriesPerRun": 1,
        "maxListingsPerRun": 1,
        "minListingDelayMs": 0,
        "maxListingDelayMs": 0,
        "minQueryDelayMs": 0,
        "maxQueryDelayMs": 0,
        "queryBurstSize": 10,
        "cooldownMinMs": 0,
        "cooldownMaxMs": 0,
        "searchRetryAttempts": 1,
        "listingRetryAttempts": 1,
        "retryBaseDelayMs": 0,
        "maxPagesPerQuery": 2,
        "maxSearchMinutesPerQuery": 1,
        "recheckAfterDays": 7,
        "staleLockMinutes": 360,
        "closeAfterMisses": 3,
    }


# ---------------------------------------------------------------- scheduler


def test_uses_the_configured_timezone_when_deciding_whether_the_run_is_due() -> None:
    moment = scheduled_moment(
        {"timezone": "Africa/Lagos"}, datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
    )
    assert moment == {"dateKey": "2026-08-20", "time": "08:00"}


def test_runs_later_the_same_day_when_the_exact_scheduled_minute_was_missed() -> None:
    state_store = MockStateStore()
    query = {
        "id": "q1",
        "platform": "Ashby",
        "role": "Python Developer",
        "type": "junior",
        "query": "fixture",
    }
    result = run_if_due(
        settings=_full_settings(),
        state_store=state_store,
        dependencies={
            "query_inventory": [query],
            "search_provider": EmptyProvider(),
            "listing_reader": lambda candidate, options=None: {},
        },
        date=datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc),
    )
    assert result["started"] is True
    assert state_store.state["lastScheduledDate"] == "2026-08-20"


def test_run_if_due_gates() -> None:
    disabled = run_if_due(
        settings={**_full_settings(), "automationEnabled": False},
        state_store=MockStateStore(),
        dependencies={},
    )
    assert disabled == {"started": False, "reason": "automation_disabled"}

    early = run_if_due(
        settings=_full_settings(),
        state_store=MockStateStore(),
        dependencies={},
        date=datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc),
    )
    assert early == {"started": False, "reason": "not_due"}

    already = MockStateStore()
    already.state["lastScheduledDate"] = "2026-08-20"
    duplicate = run_if_due(
        settings=_full_settings(),
        state_store=already,
        dependencies={},
        date=datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc),
    )
    assert duplicate == {"started": False, "reason": "already_started"}

    busy = MockStateStore()
    busy.state["activeRunId"] = "run_x"
    active = run_if_due(
        settings=_full_settings(),
        state_store=busy,
        dependencies={},
        date=datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc),
    )
    assert active == {"started": False, "reason": "already_started"}


def test_minute_of_day_parses_and_rejects() -> None:
    assert minute_of_day("08:00") == 480
    assert minute_of_day("9:05") == 545
    with pytest.raises(RuntimeError, match="HH:MM"):
        minute_of_day("8")


# ------------------------------------------------------ runtime configuration


def test_uses_enabled_sheet_queries_and_platform_domains_as_the_runtime_inventory() -> None:
    configuration = configuration_from_rows(
        {
            "platformRows": [
                ["Ashby", "site:jobs.ashbyhq.com", "TRUE"],
                ["Lever", "site:jobs.lever.co", "FALSE"],
            ],
            "queryRows": [
                [
                    "q1",
                    "Ashby",
                    "Python Developer",
                    "junior",
                    "site:jobs.ashbyhq.com python",
                    "TRUE",
                ],
                ["q2", "Lever", "Python Developer", "junior", "site:jobs.lever.co python", "TRUE"],
                ["q3", "Ashby", "Backend Developer", "junior", "disabled", "FALSE"],
            ],
            "ruleRows": [["Python signals", "python | django", "2"]],
        }
    )
    assert [query["id"] for query in configuration["queries"]] == ["q1"]
    assert configuration["queries"][0]["allowedHosts"] == ["jobs.ashbyhq.com"]
    assert configuration["signalRules"]["python"] == ["python", "django"]


def test_configuration_falls_back_to_defaults_when_rows_are_empty() -> None:
    configuration = configuration_from_rows({})
    assert len(configuration["queries"]) == 288
    assert configuration["signalRules"]["junior"] is None
    role_rows = configuration_from_rows(
        {
            "roleRows": [
                [
                    "Product Designer",
                    "junior",
                    "junior product designer | graduate product designer",
                ]
            ]
        }
    )
    assert role_rows["roles"][0]["name"] == "Product Designer"
    assert role_rows["roles"][0]["field"] == "design"
    assert role_rows["roles"][0]["junior"] == [
        "junior product designer",
        "graduate product designer",
    ]


# ------------------------------------------------------------- config helpers


def test_settings_from_control_maps_keys_and_legacy_aliases() -> None:
    settings = settings_from_control(
        {
            "Automation Enabled": "TRUE",
            "Daily Run Time": "09:30",
            "Max Queries Per Run": "5",
            "Minimum Delay (ms)": "1234",
            "Batch Size": "4",
        }
    )
    assert settings["automationEnabled"] is True
    assert settings["dailyRunTime"] == "09:30"
    assert settings["maxQueriesPerRun"] == 5
    assert settings["minListingDelayMs"] == 1234
    assert settings["queryBurstSize"] == 4
    assert settings["timezone"] == "Africa/Lagos"


def test_settings_from_control_defaults_when_empty() -> None:
    settings = settings_from_control({})
    assert settings["automationEnabled"] is False
    assert settings["dailyRunTime"] == "08:00"
    assert settings["maxListingsPerRun"] == 180


def test_load_environment_reads_file_and_respects_process_env(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FROM_FILE=1\nQUOTED='value'\nDQUOTED=\"value2\"\nlower=no\n# comment\n")
    environment = load_environment(str(env_file))
    assert environment["FROM_FILE"] == "1"
    assert environment["QUOTED"] == "value"
    assert environment["DQUOTED"] == "value2"
    assert "lower" not in environment
    monkeypatch.setenv("FROM_FILE", "from-env")
    assert load_environment(str(env_file))["FROM_FILE"] == "from-env"
    assert "FROM_FILE" in load_environment(str(tmp_path / "missing.env"))


def test_load_local_settings_normalizes(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {"maxQueriesPerRun": "3", "minListingDelayMs": 20000, "maxListingDelayMs": 10000}
        )
    )
    settings = load_local_settings(str(runtime_file))
    assert settings["maxQueriesPerRun"] == 3
    assert settings["maxListingDelayMs"] == 20000
    runtime_file.write_text(json.dumps({"dailyRunTime": "8:00"}))
    with pytest.raises(ValueError):
        load_local_settings(str(runtime_file))


# ----------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Hermetic isolation: the shared repo env loader (outbound/shared/env.py)
    # mutates os.environ at import time, so ambient job-discovery keys from the
    # repo .env must be scrubbed or these tests would reach the live transports.
    for key in (
        "SHEETS_TRANSPORT",
        "GOOGLE_APPS_SCRIPT_URL",
        "APPS_SCRIPT_TOKEN",
        "GOOGLE_SHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "CONTROL_SOURCE",
        "CONFIGURATION_SOURCE",
        "SEARCH_PROVIDER",
        "PLAYWRIGHT_HEADED",
        "PLAYWRIGHT_PROFILE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("LOCAL_CONTROL_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("SEARCH_PROVIDER", "fixture")
    (tmp_path / "runtime.json").write_text("{}")
    return tmp_path


def test_cli_status_smoke(cli_env, capsys) -> None:
    code = cli.main(["status"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["enabledQueries"] == 288
    assert payload["controlSource"] == "local"
    assert payload["jobStatusCounts"] == {}
    assert payload["recentRuns"] == []


def test_cli_smoke_test_runs_in_test_mode(cli_env, capsys) -> None:
    code = cli.main(["smoke-test"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["trigger"] == "smoke-test"
    assert payload["queriesAttempted"] == 1
    assert payload["hydrated"] == 1
    state = cli.build_runtime()["state_store"].read()
    assert list(state["jobs"].values())[0]["status"] == "test"


def test_cli_unknown_command_prints_help(cli_env, capsys) -> None:
    code = cli.main(["bogus"])
    assert code == 0
    assert "Commands:" in capsys.readouterr().out


def test_cli_run_reports_unconfigured_sheets_gracefully(cli_env) -> None:
    with pytest.raises(RuntimeError, match="Apps Script Sheet transport"):
        cli.main(["reset"])


def test_cli_build_runtime_selects_transports(cli_env, monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "playwright-google")
    runtime = cli.build_runtime()
    from outbound.job_discovery.browser import PlaywrightBrowser

    assert isinstance(runtime["dependencies"]["search_provider"].browser, PlaywrightBrowser)
    assert callable(runtime["dependencies"]["listing_reader"])
    assert runtime["sheets"] is None

    monkeypatch.setenv("SHEETS_TRANSPORT", "apps-script")
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", "https://example.com/apps")
    monkeypatch.setenv("APPS_SCRIPT_TOKEN", "token-1")
    runtime = cli.build_runtime()
    from outbound.job_discovery.sheets import AppsScriptSheetsClient

    assert isinstance(runtime["sheets"], AppsScriptSheetsClient)
    runtime["sheets"].close()
