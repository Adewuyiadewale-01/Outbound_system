"""Command-line entry ported from ``job_discovery/src/cli.mjs``.

Commands: ``setup-sheet | reset | run | reverify | smoke-test | live-test |
schedule | status``. Signal semantics are preserved: the first SIGINT/SIGTERM
sets the stop flag so the active query can checkpoint and finish; a second
signal exits immediately with code 130.

Note: the default state path follows this repo's ``state/`` conventions
(``state/job_discovery/state.sqlite``); the reference used ``./data/state.json``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

from outbound.job_discovery.providers import create_listing_reader, create_search_provider
from outbound.job_discovery.queries import build_queries, configuration_from_rows
from outbound.job_discovery.runner import (
    _decide_walk_mode,
    _parked_state,
    classify_query_tier,
    reverify_stored_jobs,
    run_discovery,
)
from outbound.job_discovery.scheduler import SchedulerHandle, start_scheduler
from outbound.job_discovery.settings import (
    PLATFORMS,
    ROLES,
    load_environment,
    load_local_settings,
    settings_from_control,
)
from outbound.job_discovery.sheets import create_sheets_client
from outbound.job_discovery.state import StateStore

_shutdown_requested = False
_scheduler_handle: SchedulerHandle | None = None


def _handle_signal(signum, _frame) -> None:
    global _shutdown_requested
    if _shutdown_requested:
        os._exit(130)
    _shutdown_requested = True
    if _scheduler_handle is not None:
        _scheduler_handle.stop()
    print(
        f"{signal.Signals(signum).name} received; the active query will checkpoint and finish before shutdown.",
        file=sys.stderr,
    )


def build_runtime() -> dict:
    """Port of ``buildRuntime`` (transport selection + lazy sources)."""
    environment = load_environment()
    state_file = environment.get("STATE_FILE") or "./state/job_discovery/state.sqlite"
    state_store = StateStore(state_file)
    sheets = create_sheets_client(environment)
    search_provider = create_search_provider(environment)
    listing_reader = create_listing_reader(environment, search_provider)
    control_source = (environment.get("CONTROL_SOURCE") or "local").lower()
    configuration_source = (environment.get("CONFIGURATION_SOURCE") or "local").lower()

    def get_settings() -> dict:
        if control_source == "sheet" and sheets:
            return settings_from_control(sheets.read_control())
        return load_local_settings(environment.get("LOCAL_CONTROL_FILE") or "./config/runtime.json")

    def get_configuration() -> dict:
        if configuration_source == "sheet" and sheets and hasattr(sheets, "read_configuration"):
            return configuration_from_rows(sheets.read_configuration())
        return configuration_from_rows({})

    dependencies = {
        "search_provider": search_provider,
        "listing_reader": listing_reader,
        "sheets": sheets,
        "should_stop": lambda: _shutdown_requested,
    }

    def get_run_dependencies() -> dict:
        configuration = get_configuration()
        return {
            **dependencies,
            "query_inventory": configuration["queries"],
            "signal_rules": configuration["signalRules"],
        }

    def get_run_context() -> dict:
        settings = get_settings()
        configuration = get_configuration()
        return {
            "settings": settings,
            "configuration": configuration,
            "dependencies": {
                **dependencies,
                "query_inventory": configuration["queries"],
                "signal_rules": configuration["signalRules"],
            },
        }

    return {
        "environment": environment,
        "state_store": state_store,
        "sheets": sheets,
        "control_source": control_source,
        "configuration_source": configuration_source,
        "get_settings": get_settings,
        "get_configuration": get_configuration,
        "get_run_dependencies": get_run_dependencies,
        "get_run_context": get_run_context,
        "dependencies": dependencies,
    }


def _exit_code() -> int:
    return 130 if _shutdown_requested else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:]) if argv is None else list(argv)
    command = arguments[0] if arguments else "help"
    runtime = build_runtime()

    if command == "setup-sheet":
        if not runtime["sheets"]:
            raise RuntimeError(
                "Configure Apps Script URL/token or direct Google Sheets credentials before setup-sheet."
            )
        local_first = (
            runtime["control_source"] == "local" and runtime["configuration_source"] == "local"
        )
        runtime["sheets"].setup(
            platforms=PLATFORMS, roles=ROLES, queries=build_queries(), localFirst=local_first
        )
        print(
            "Google Sheet output tabs and priority views are ready; local-only configuration tabs were removed."
            if local_first
            else "Google Sheet tabs, configuration, query inventory, and headers are ready."
        )
        return _exit_code()

    if command == "reset":
        sheets = runtime["sheets"]
        if not sheets or not hasattr(sheets, "clear_projection"):
            raise RuntimeError(
                "The Apps Script Sheet transport is required to clear the live Sheet projection."
            )
        reset_run_id = f"reset_{int(time.time() * 1000)}"
        if not runtime["state_store"].acquire_run_lock(reset_run_id):
            raise RuntimeError(
                "A discovery run is active; stop or let it finish before resetting state."
            )
        try:
            database = runtime["state_store"].reset()
            projection = sheets.clear_projection()
            print(
                json.dumps(
                    {"reset": True, "database": database, "projection": projection},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        finally:
            runtime["state_store"].release_run_lock()
        return _exit_code()

    if command == "run":
        context = runtime["get_run_context"]()
        run = run_discovery(
            trigger="manual",
            state_store=runtime["state_store"],
            settings=context["settings"],
            **context["dependencies"],
        )
        print(json.dumps(run, indent=2, ensure_ascii=False))
        return _exit_code()

    if command == "reverify":
        configuration = runtime["get_configuration"]()
        result = reverify_stored_jobs(
            state_store=runtime["state_store"],
            sheets=runtime["sheets"],
            signal_rules=configuration["signalRules"],
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return _exit_code()

    if command == "smoke-test":
        smoke_query = {
            "id": "smoke-test",
            "platform": "Ashby",
            "role": "Python Developer",
            "type": "smoke-test",
            "query": "synthetic smoke test",
            "allowedHosts": ["jobs.ashbyhq.com"],
        }

        class _SmokeSearchProvider:
            def search(self, _query, _options=None):
                return [
                    {
                        "title": "Junior Python Developer — Test Record",
                        "link": "https://jobs.ashbyhq.com/daily-job-discovery/test-job-0001",
                        "snippet": "Remote role requiring Python.",
                    }
                ]

        def smoke_listing_reader(_candidate, _options=None):
            return {
                "title": "Junior Python Developer — Test Record",
                "description": "This is a system smoke test. The role is fully remote and requires Python.",
                "company": "Daily Job Discovery Test",
                "location": "Remote",
                "canonicalUrl": "https://jobs.ashbyhq.com/daily-job-discovery/test-job-0001",
            }

        settings = {
            **runtime["get_settings"](),
            "maxQueriesPerRun": 1,
            "maxListingsPerRun": 1,
            "minListingDelayMs": 0,
            "maxListingDelayMs": 0,
            "minQueryDelayMs": 0,
            "maxQueryDelayMs": 0,
            "cooldownMinMs": 0,
            "cooldownMaxMs": 0,
        }
        run = run_discovery(
            trigger="smoke-test",
            state_store=runtime["state_store"],
            search_provider=_SmokeSearchProvider(),
            listing_reader=smoke_listing_reader,
            sheets=runtime["dependencies"]["sheets"],
            should_stop=runtime["dependencies"]["should_stop"],
            settings=settings,
            query_inventory=[smoke_query],
            queries=[smoke_query],
            test_mode=True,
            force_hydration=True,
        )
        print(json.dumps(run, indent=2, ensure_ascii=False))
        return _exit_code()

    if command == "live-test":
        if runtime["environment"].get("SEARCH_PROVIDER") != "playwright-google":
            raise RuntimeError("Set SEARCH_PROVIDER=playwright-google before running a live test.")
        state = runtime["state_store"].read()
        configuration = runtime["get_configuration"]()
        usage = state.get("liveTestUsage") or {}
        query = next(
            (item for item in configuration["queries"] if (usage.get(item["id"]) or 0) < 2), None
        )
        if query is None:
            raise RuntimeError("Every configured query has reached the two-use live-test limit.")
        dependencies = runtime["dependencies"]
        dependencies["search_provider"].max_results = 1
        settings = {**runtime["get_settings"](), "maxQueriesPerRun": 1, "maxListingsPerRun": 1}
        run = run_discovery(
            trigger="live-e2e",
            state_store=runtime["state_store"],
            search_provider=dependencies["search_provider"],
            listing_reader=dependencies["listing_reader"],
            sheets=dependencies["sheets"],
            should_stop=dependencies["should_stop"],
            settings=settings,
            queries=[query],
            signal_rules=configuration["signalRules"],
        )
        if run.get("queriesAttempted"):
            updated = runtime["state_store"].read()
            current = (updated.get("liveTestUsage") or {}).get(query["id"]) or 0
            updated["liveTestUsage"] = {
                **(updated.get("liveTestUsage") or {}),
                query["id"]: current + 1,
            }
            runtime["state_store"].write(updated)
        print(json.dumps(run, indent=2, ensure_ascii=False))
        return _exit_code()

    if command == "schedule":
        global _scheduler_handle
        print(
            f"Scheduler started. It checks {runtime['control_source']} control once per minute; Ctrl+C stops it."
        )
        _scheduler_handle = start_scheduler(
            get_settings=runtime["get_settings"],
            get_dependencies=runtime["get_run_dependencies"],
            state_store=runtime["state_store"],
            dependencies=runtime["dependencies"],
        )
        while _scheduler_handle.thread is not None and _scheduler_handle.thread.is_alive():
            _scheduler_handle.thread.join(timeout=1.0)
        return _exit_code()

    if command == "status":
        context = runtime["get_run_context"]()
        state = runtime["state_store"].read()
        database = runtime["state_store"].stats()
        coverage_map_fn = getattr(runtime["state_store"], "query_coverage_map", None)
        stats_map_fn = getattr(runtime["state_store"], "query_stats_map", None)
        coverage_map = coverage_map_fn() if coverage_map_fn else {}
        stats_map = stats_map_fn() if stats_map_fn else {}
        query_tiers = {"high": 0, "average": 0, "low": 0}
        parked_count = 0
        due_full_checks = 0
        now_ms = time.time() * 1000
        for query in context["configuration"]["queries"]:
            query_id = query["id"]
            query_tiers[classify_query_tier(stats_map.get(query_id))] += 1
            if _parked_state(query_id, stats_map.get(query_id), context["settings"]):
                parked_count += 1
                continue
            if (
                _decide_walk_mode(
                    coverage_map.get(query_id),
                    now_ms=now_ms,
                    interval_days=context["settings"].get("fullCheckIntervalDays"),
                )
                == "full"
            ):
                due_full_checks += 1
        jobs = list((state.get("jobs") or {}).values())
        runs = sorted(
            (state.get("runs") or {}).values(),
            key=lambda run: str(run.get("startedAt") or ""),
            reverse=True,
        )[:12]
        recent_runs = [
            {
                "id": run.get("id"),
                "trigger": run.get("trigger"),
                "status": run.get("status"),
                "startedAt": run.get("startedAt"),
                "endedAt": run.get("endedAt"),
                "queriesAttempted": run.get("queriesAttempted") or 0,
                "queriesCompleted": run.get("queriesCompleted") or 0,
                "hydrated": run.get("hydrated") or 0,
                "newJobs": run.get("newJobs") or 0,
                "errors": (run.get("errors") or [])[:3],
                "stopReason": run.get("stopReason") or "",
            }
            for run in runs
        ]

        def count_by(values: list, key: str) -> dict:
            counts: dict[str, int] = {}
            for item in values:
                value = str(item.get(key) or "unknown")
                counts[value] = counts.get(value, 0) + 1
            return dict(sorted(counts.items()))

        query_progress = list((state.get("queryProgress") or {}).values())
        print(
            json.dumps(
                {
                    "controlSource": runtime["control_source"],
                    "configurationSource": runtime["configuration_source"],
                    "automationEnabled": context["settings"]["automationEnabled"],
                    "dailyRunTime": context["settings"]["dailyRunTime"],
                    "timezone": context["settings"]["timezone"],
                    "enabledQueries": len(context["configuration"]["queries"]),
                    "queryCursor": state.get("queryCursor"),
                    "queryCursorId": state.get("queryCursorId"),
                    "activeRunId": state.get("activeRunId"),
                    "lastScheduledDate": state.get("lastScheduledDate"),
                    "database": database,
                    "coveredQueries": len(coverage_map),
                    "queryTiers": query_tiers,
                    "parkedQueries": parked_count,
                    "fullChecksDue": due_full_checks,
                    "jobStatusCounts": count_by(jobs, "status"),
                    "queryProgressCounts": count_by(query_progress, "status"),
                    "recentRuns": recent_runs,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return _exit_code()

    print(
        "Commands: setup-sheet | reset | run | reverify | smoke-test | live-test | schedule | status"
    )
    return 0


def run(argv: list[str] | None = None) -> int:
    """Entry wrapper: installs signal handlers, reports errors like the reference."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)
    try:
        return main(argv)
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - mirrors the reference catch-all
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
