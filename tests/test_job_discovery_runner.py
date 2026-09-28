"""Tests for the ported runner (``job_discovery/src/run.mjs``).

Ported from ``job_discovery/tests/run.test.mjs`` plus extended coverage for
projection sync, reverify, and interrupted-run recovery.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from outbound.job_discovery.browser import PlaywrightGoogleSearchProvider
from outbound.job_discovery.runner import (
    _decide_walk_mode,
    _parked_state,
    _plan_deep_wave,
    classify_query_tier,
    reverify_stored_jobs,
    run_discovery,
    sync_state_to_sheets,
)
from outbound.job_discovery.state import StateStore


class StaticSearchProvider:
    def __init__(self, results):
        self._results = results
        self.calls = 0

    def search(self, query, options=None):
        self.calls += 1
        return self._results(query) if callable(self._results) else list(self._results)

    def close(self):
        pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


FAST_SETTINGS = {
    "minDelayMs": 0,
    "maxDelayMs": 0,
    "queryBurstSize": 99,
    "cooldownMinMs": 0,
    "cooldownMaxMs": 0,
    "closeAfterMisses": 3,
}


def test_deduplicates_before_reading_a_listing_and_avoids_re_reading_an_unchanged_verified_job(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / "state.json")
    provider = StaticSearchProvider(
        [
            {
                "title": "Junior Python Developer",
                "link": "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=one",
                "snippet": "Remote Python role",
            },
            {
                "title": "Junior Python Developer",
                "link": "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=two",
                "snippet": "Remote Python role",
            },
        ]
    )
    reads: list = []

    def listing_reader(candidate, options=None):
        reads.append(candidate["jobId"])
        return {
            "title": "Junior Python Developer",
            "description": "Remote role using Python.",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": "https://jobs.ashbyhq.com/acme/abcdefgh",
        }

    settings = {**FAST_SETTINGS, "maxQueriesPerRun": 1, "maxListingsPerRun": 5}
    first = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
    )
    second = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
    )
    assert first["uniqueCandidates"] == 1
    assert first["hydrated"] == 1
    assert second["hydrated"] == 0
    assert len(reads) == 1


def test_finishes_the_current_query_before_stopping_at_the_daily_listing_target_and_advances_the_cursor(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / "state.json")
    provider = StaticSearchProvider(
        [
            {
                "title": "Junior Python Developer",
                "link": "https://jobs.ashbyhq.com/acme/rotating-job",
                "snippet": "Remote Python role",
            }
        ]
    )

    def listing_reader(candidate, options=None):
        return {
            "title": "Junior Python Developer",
            "description": "Remote role using Python.",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": "https://jobs.ashbyhq.com/acme/rotating-job",
        }

    settings = {**FAST_SETTINGS, "maxQueriesPerRun": 10, "maxListingsPerRun": 1}
    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
    )
    state = state_store.read()
    assert run["queriesAttempted"] == 1
    assert state["queryCursor"] == 1
    assert state["queryProgress"]["Ashby:Python Developer:junior"]["status"] == "completed"


def test_counts_earlier_same_day_runs_toward_the_daily_listing_target(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    prior = state_store.read()
    prior["runs"]["prior"] = {
        "id": "prior",
        "trigger": "manual",
        "startedAt": _iso_now(),
        "hydrated": 1,
        "hydratedByField": {"design": 1},
    }
    state_store.write(prior)
    queries = [
        {
            "id": "q1",
            "platform": "Ashby",
            "role": "Python Developer",
            "type": "junior",
            "query": "one",
        },
        {
            "id": "q2",
            "platform": "Ashby",
            "role": "Python Developer",
            "type": "junior",
            "query": "two",
        },
    ]
    provider = StaticSearchProvider(
        lambda query: [
            {
                "title": f"Junior Python Developer {query['id']}",
                "link": f"https://jobs.ashbyhq.com/acme/{query['id']}-role",
                "snippet": "Remote",
            }
        ]
    )

    def listing_reader(candidate, options=None):
        return {
            "title": candidate["title"],
            "description": "Remote Python",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": candidate["canonicalUrl"],
        }

    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings={
            "timezone": "UTC",
            "maxQueriesPerRun": 2,
            "maxListingsPerRun": 2,
            "minDelayMs": 0,
            "maxDelayMs": 0,
            "minQueryDelayMs": 0,
            "maxQueryDelayMs": 0,
            "queryBurstSize": 99,
            "cooldownMinMs": 0,
            "cooldownMaxMs": 0,
        },
        query_inventory=queries,
    )
    assert run["dailyHydratedAtStart"] == 1
    assert run["queriesAttempted"] == 1
    assert run["stopReason"] == "daily_listing_target"


def test_pauses_a_fields_remaining_hydration_queue_at_its_daily_allocation_and_continues_the_other_field(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / "state.json")
    engineering = {
        "id": "engineering",
        "platform": "Ashby",
        "role": "Backend Developer",
        "field": "engineering",
        "type": "junior",
        "query": "engineering",
    }
    design = {
        "id": "design",
        "platform": "Ashby",
        "role": "Product Designer",
        "field": "design",
        "type": "junior",
        "query": "design",
    }
    provider = StaticSearchProvider(
        lambda query: [
            {
                "title": f"{query['role']} {number}",
                "link": f"https://jobs.ashbyhq.com/acme/{query['id']}-{number}",
                "snippet": "Remote",
            }
            for number in (1, 2)
        ]
    )

    def listing_reader(candidate, options=None):
        return {
            "title": candidate["title"],
            "description": "Remote",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": candidate["canonicalUrl"],
        }

    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings={
            "maxQueriesPerRun": 2,
            "maxListingsPerRun": 2,
            "minDelayMs": 0,
            "maxDelayMs": 0,
            "minQueryDelayMs": 0,
            "maxQueryDelayMs": 0,
            "queryBurstSize": 99,
            "cooldownMinMs": 0,
            "cooldownMaxMs": 0,
        },
        query_inventory=[engineering, design],
    )
    state = state_store.read()
    assert run["hydratedByField"] == {"engineering": 1, "design": 1}
    assert state["queryProgress"]["engineering"]["status"] == "pending_quota"
    assert state["queryProgress"]["design"]["status"] == "pending_quota"
    assert run["hydrated"] == 2


def test_records_ashby_company_careers_pages_without_listing_hydration(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    provider = StaticSearchProvider(
        [
            {
                "title": "Open Positions (7)",
                "link": "https://jobs.ashbyhq.com/cradlebio",
                "snippet": "Careers",
            }
        ]
    )
    reads: list = []

    def listing_reader(candidate, options=None):
        reads.append(candidate)
        return {}

    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings={**FAST_SETTINGS, "maxQueriesPerRun": 1, "maxListingsPerRun": 5},
    )
    state = state_store.read()
    assert len(reads) == 0
    assert run["uniqueCandidates"] == 0
    assert list(state["jobs"].values())[0]["status"] == "company_board"
    assert state["queryProgress"]["Ashby:Python Developer:junior"]["companyBoardCandidates"] == [
        {"url": "https://jobs.ashbyhq.com/cradlebio", "reason": "company_careers_landing_page"}
    ]


def test_keeps_the_cursor_on_a_failed_listing_and_resumes_hydration_without_repeating_google_search(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / "state.json")
    query = {
        "id": "q1",
        "platform": "Ashby",
        "role": "Python Developer",
        "type": "junior",
        "query": "fixture",
        "allowedHosts": ["jobs.ashbyhq.com"],
    }
    provider = StaticSearchProvider(
        [
            {
                "title": "Junior Python Developer",
                "link": "https://jobs.ashbyhq.com/acme/retry-job",
                "snippet": "Remote Python",
            }
        ]
    )
    failing = {"value": True}

    def listing_reader(candidate, options=None):
        if failing["value"]:
            raise RuntimeError("temporary ATS failure")
        return {
            "title": "Junior Python Developer",
            "description": "Remote Python",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": "https://jobs.ashbyhq.com/acme/retry-job",
        }

    settings = {
        **FAST_SETTINGS,
        "maxQueriesPerRun": 1,
        "maxListingsPerRun": 5,
        "searchRetryAttempts": 1,
        "listingRetryAttempts": 1,
        "retryBaseDelayMs": 0,
    }
    first = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
        query_inventory=[query],
    )
    state = state_store.read()
    assert first["stopReason"] == "query_pending_retry"
    assert state["queryCursor"] == 0
    assert state["queryProgress"]["q1"]["status"] == "pending_retry"
    failing["value"] = False
    second = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
        query_inventory=[query],
    )
    state = state_store.read()
    assert second["hydrated"] == 1
    assert provider.calls == 1
    assert state["queryProgress"]["q1"]["status"] == "completed"


def test_does_not_immediately_retry_a_google_verification_challenge(tmp_path: Path) -> None:
    from outbound.job_discovery.browser import SearchBlockedError

    state_store = StateStore(tmp_path / "state.json")
    query = {
        "id": "q1",
        "platform": "Ashby",
        "role": "Python Developer",
        "type": "junior",
        "query": "fixture",
        "allowedHosts": ["jobs.ashbyhq.com"],
    }
    block = {"value": True}
    calls = {"count": 0}

    def search(_query, _options=None):
        calls["count"] += 1
        if block["value"]:
            raise SearchBlockedError("Google presented a verification page")
        return []

    provider = SimpleNamespace(search=search)
    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        settings={
            **FAST_SETTINGS,
            "maxQueriesPerRun": 1,
            "searchRetryAttempts": 3,
            "retryBaseDelayMs": 0,
        },
        query_inventory=[query],
    )
    state = state_store.read()
    assert calls["count"] == 1
    assert run["stopReason"] == "query_pending_retry"
    assert state["queryProgress"]["q1"]["status"] == "pending_retry"


def test_moves_a_job_to_closed_only_after_repeated_misses_from_its_completed_source_query(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / "state.json")
    query = {
        "id": "q1",
        "platform": "Ashby",
        "role": "Python Developer",
        "type": "junior",
        "query": "fixture",
    }
    seed_provider = StaticSearchProvider(
        [
            {
                "title": "Junior Python Developer",
                "link": "https://jobs.ashbyhq.com/acme/lifecycle-job",
                "snippet": "Remote Python",
            }
        ]
    )

    def listing_reader(candidate, options=None):
        return {
            "title": "Junior Python Developer",
            "description": "Remote Python",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": "https://jobs.ashbyhq.com/acme/lifecycle-job",
        }

    settings = {
        **FAST_SETTINGS,
        "maxQueriesPerRun": 1,
        "maxListingsPerRun": 5,
        "searchRetryAttempts": 1,
        "listingRetryAttempts": 1,
        "retryBaseDelayMs": 0,
        "closeAfterMisses": 3,
    }
    run_discovery(
        state_store=state_store,
        search_provider=seed_provider,
        listing_reader=listing_reader,
        settings=settings,
        queries=[query],
    )
    empty_provider = StaticSearchProvider([])
    run_discovery(
        state_store=state_store,
        search_provider=empty_provider,
        listing_reader=listing_reader,
        settings=settings,
        queries=[query],
    )
    state = state_store.read()
    assert list(state["jobs"].values())[0]["status"] == "possibly_closed"
    run_discovery(
        state_store=state_store,
        search_provider=empty_provider,
        listing_reader=listing_reader,
        settings=settings,
        queries=[query],
    )
    run_discovery(
        state_store=state_store,
        search_provider=empty_provider,
        listing_reader=listing_reader,
        settings=settings,
        queries=[query],
    )
    state = state_store.read()
    assert list(state["jobs"].values())[0]["status"] == "closed"


# --------------------------------------------------------------- extensions


class RecordingSheets:
    """Fallback-path recorder (no sync_projection -> retain/upsert/replace)."""

    def __init__(self):
        self.calls: list = []

    def retain(self, tab, ids):
        self.calls.append(("retain", tab, list(ids)))

    def upsert(self, tab, records):
        self.calls.append(("upsert", tab, records))

    def replace(self, tab, rows):
        self.calls.append(("replace", tab, list(rows)))


def test_sync_state_to_sheets_fallback_order_and_row_filtering() -> None:
    sheets = RecordingSheets()
    state = {
        "jobs": {
            "j1": {
                "jobId": "j1",
                "companyId": "c1",
                "status": "active",
                "title": "T1",
                "sourceQueries": ["q1"],
            },
            "j2": {
                "jobId": "j2",
                "companyId": "c1",
                "status": "test",
                "title": "T2",
                "sourceQueries": ["q1"],
            },
            "j3": {
                "jobId": "j3",
                "companyId": "c1",
                "status": "review",
                "title": "T3",
                "sourceQueries": ["q1"],
            },
        },
        "companies": {
            "c1": {
                "companyId": "c1",
                "companyName": "Acme",
                "platforms": ["Ashby"],
                "careerUrls": [],
            },
            "c2": {"companyId": "c2", "companyName": "Unused", "platforms": [], "careerUrls": []},
        },
        "runs": {
            "r1": {"id": "r1", "trigger": "manual", "status": "completed", "errors": []},
            "r2": {
                "id": "r2",
                "trigger": "manual",
                "status": "completed",
                "errors": [],
                "sheetSyncedAt": "already",
            },
        },
    }
    sync_state_to_sheets(sheets, state, SimpleNamespace())
    assert [call[0] for call in sheets.calls] == [
        "retain",
        "retain",
        "upsert",
        "upsert",
        "replace",
        "upsert",
    ]
    assert sheets.calls[0] == ("retain", "Jobs", ["j1", "j3"])
    assert sheets.calls[1] == ("retain", "Companies", ["c1"])
    upserted_jobs = sheets.calls[2][2]
    assert [row["id"] for row in upserted_jobs] == ["j1", "j3"]
    replaced = sheets.calls[4][2]
    assert [row[0] for row in replaced] == ["j3"]
    upserted_runs = sheets.calls[5][2]
    assert [row["id"] for row in upserted_runs] == ["r1"]
    assert state["runs"]["r1"]["sheetSyncedAt"]
    assert state["runs"]["r2"]["sheetSyncedAt"] == "already"


def test_sync_state_to_sheets_prefers_supported_projection() -> None:
    calls: list = []

    class ProjectionSheets:
        def sync_projection(self, payload):
            calls.append(payload)
            return {"supported": True}

    state = {"jobs": {}, "companies": {}, "runs": {}}
    sync_state_to_sheets(ProjectionSheets(), state, SimpleNamespace())
    assert len(calls) == 1
    assert calls[0]["jobIds"] == [] and calls[0]["companyIds"] == []


def test_reverify_reclassifies_careers_pages_and_reruns_signals(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    state = state_store.read()
    state["jobs"] = {
        "board": {
            "jobId": "board",
            "canonicalUrl": "https://jobs.ashbyhq.com/cradlebio",
            "platform": "Ashby",
            "title": "Board",
            "status": "active",
            "reviewReason": "",
            "remoteStatus": "not_found",
            "juniorStatus": "not_found",
            "pythonStatus": "not_found",
            "sourceQueries": ["q1"],
        },
        "job": {
            "jobId": "job",
            "canonicalUrl": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "platform": "Ashby",
            "title": "Junior Python Developer",
            "description": "Fully remote. Python required.",
            "location": "",
            "role": "Python Developer",
            "status": "review",
            "reviewReason": "Signal could not be confirmed",
            "juniorStatus": "unsure",
            "remoteStatus": "unsure",
            "pythonStatus": "unsure",
            "evidenceText": "",
            "score": 0,
            "sourceQueries": ["q1"],
        },
    }
    state_store.write(state)
    result = reverify_stored_jobs(state_store=state_store)
    updated = state_store.read()
    assert result["checked"] == 2
    assert result["changed"] == 2
    assert result["sheetSynced"] is False
    assert updated["jobs"]["board"]["status"] == "company_board"
    assert updated["jobs"]["board"]["recordType"] == "company_careers_landing_page"
    assert updated["jobs"]["job"]["juniorStatus"] == "verified"
    assert updated["jobs"]["job"]["status"] == "active"


def test_recovers_an_abandoned_active_run(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    state = state_store.read()
    state["activeRunId"] = "run_abandoned"
    state["runs"]["run_abandoned"] = {
        "id": "run_abandoned",
        "trigger": "manual",
        "status": "running",
        "startedAt": _iso_now(),
        "errors": [],
    }
    state_store.write(state)
    provider = StaticSearchProvider([])
    run_discovery(
        state_store=state_store,
        search_provider=provider,
        settings={**FAST_SETTINGS, "maxQueriesPerRun": 1},
    )
    updated = state_store.read()
    assert updated["runs"]["run_abandoned"]["status"] == "interrupted_recovered"
    assert "Recovered by" in updated["runs"]["run_abandoned"]["errors"][0]
    assert updated["activeRunId"] is None


class _PageBrowser:
    def __init__(self):
        self.opened: list = []
        self._queue: list = []

    def refill(self, pages):
        self._queue = [dict(page) for page in pages]

    def open(self, url):
        self.opened.append(url)

    def reject_google_cookies_if_present(self):
        return None

    def scroll_search_results(self):
        return None

    def evaluate(self, expression):
        page = self._queue.pop(0)
        return {"url": "https://google.com/search", "title": "Search", "bodyText": "", **page}

    def close(self):
        return None


class _RecordingProvider:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list = []
        self.queries: list = []

    def search(self, query, options=None):
        self.calls.append(dict(options or {}))
        self.queries.append(query["id"])
        return self.inner.search(query, options)

    def close(self):
        return None


def _demo_jobs(*numbers):
    return [
        {
            "title": f"Junior Python Developer {number}",
            "link": f"https://jobs.ashbyhq.com/acme/job-{number}",
            "snippet": "Remote",
        }
        for number in numbers
    ]


def test_second_day_rerun_uses_a_shallow_walk_and_skips_miss_accounting(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    query = {
        "id": "demo:q1",
        "platform": "Ashby",
        "role": "Python Developer",
        "field": "engineering",
        "type": "junior",
        "query": "site:jobs.ashbyhq.com python",
        "allowedHosts": ["jobs.ashbyhq.com"],
    }
    browser = _PageBrowser()
    provider = _RecordingProvider(PlaywrightGoogleSearchProvider(browser=browser))
    reads: list = []

    def listing_reader(candidate, options=None):
        reads.append(candidate["jobId"])
        return {
            "title": candidate["title"],
            "description": "Fully remote role requiring Python.",
            "company": "Acme",
            "location": "Remote",
            "canonicalUrl": candidate["canonicalUrl"],
        }

    settings = {
        "maxQueriesPerRun": 1,
        "maxListingsPerRun": 50,
        "minListingDelayMs": 0,
        "maxListingDelayMs": 0,
        "minPageDelayMs": 0,
        "maxPageDelayMs": 0,
        "searchPageBurstSize": 99,
        "minSearchPageCooldownMs": 0,
        "maxSearchPageCooldownMs": 0,
        "queryBurstSize": 99,
        "cooldownMinMs": 0,
        "cooldownMaxMs": 0,
        "searchRetryAttempts": 1,
        "listingRetryAttempts": 1,
        "retryBaseDelayMs": 0,
    }

    browser.refill(
        [
            {"hasNext": True, "results": _demo_jobs(1, 2, 3)},
            {"hasNext": True, "results": _demo_jobs(4, 5, 6)},
            {"hasNext": True, "results": _demo_jobs(7, 8, 9)},
            {"hasNext": False, "results": _demo_jobs(10)},
        ]
    )
    day1 = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
        query_inventory=[query],
    )
    coverage = state_store.get_query_coverage("demo:q1")
    assert day1["hydrated"] == 10
    assert len(browser.opened) == 4
    assert provider.calls[0].get("shallowStop") is None
    assert (
        coverage is not None
        and coverage["frontierPage"] == 3
        and coverage["lastStopReason"] == "exhausted"
    )

    before = len(browser.opened)
    browser.refill(
        [
            {
                "hasNext": True,
                "results": [
                    {
                        "title": "Junior Python Developer 11",
                        "link": "https://jobs.ashbyhq.com/acme/job-11",
                        "snippet": "Remote",
                    },
                    *_demo_jobs(1, 2),
                ],
            },
            {"hasNext": True, "results": _demo_jobs(3)},
            {"hasNext": True, "results": _demo_jobs(4)},
            {"hasNext": True, "results": _demo_jobs(5)},
        ]
    )
    day2 = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=listing_reader,
        settings=settings,
        query_inventory=[query],
    )
    day2_opened = browser.opened[before:]
    state = state_store.read()
    assert len(day2_opened) == 3
    assert "start=20" in day2_opened[-1] and "start=30" not in day2_opened[-1]
    assert day2["hydrated"] == 1
    shallow_call = provider.calls[-1]
    assert shallow_call["shallowStop"]["enabled"] is True
    assert shallow_call["shallowStop"]["frontierPage"] == 3
    assert shallow_call["knownUrls"]
    assert state["queryProgress"]["demo:q1"]["completionReason"] == "known_frontier"
    coverage = state_store.get_query_coverage("demo:q1")
    assert coverage["frontierPage"] == 3 and coverage["lastStopReason"] == "known_frontier"
    jobs = list(state["jobs"].values())
    assert all(job.get("misses") == 0 for job in jobs)
    assert all(job.get("status") == "active" for job in jobs)
    stats = state_store.get_query_stats("demo:q1")
    assert [crawl["mode"] for crawl in stats["crawls"]] == ["full", "shallow"]
    assert stats["crawls"][1]["newResults"] == 1


def test_decide_walk_mode_requires_fresh_complete_coverage() -> None:
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    fresh = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    stale = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    assert _decide_walk_mode(None, now_ms=now_ms, interval_days=7) == "full"
    assert (
        _decide_walk_mode(
            {"frontierPage": None, "lastStopReason": "exhausted", "coveredAt": fresh},
            now_ms=now_ms,
            interval_days=7,
        )
        == "full"
    )
    assert (
        _decide_walk_mode(
            {"frontierPage": 3, "lastStopReason": "", "coveredAt": fresh},
            now_ms=now_ms,
            interval_days=7,
        )
        == "full"
    )
    assert (
        _decide_walk_mode(
            {"frontierPage": 3, "lastStopReason": "exhausted", "coveredAt": fresh},
            now_ms=now_ms,
            interval_days=7,
        )
        == "shallow"
    )
    assert (
        _decide_walk_mode(
            {"frontierPage": 3, "lastStopReason": "known_frontier", "coveredAt": fresh},
            now_ms=now_ms,
            interval_days=7,
        )
        == "shallow"
    )
    assert (
        _decide_walk_mode(
            {"frontierPage": 3, "lastStopReason": "exhausted", "coveredAt": stale},
            now_ms=now_ms,
            interval_days=7,
        )
        == "full"
    )


def _fast_settings(**overrides):
    settings = {
        "maxQueriesPerRun": 2,
        "maxListingsPerRun": 50,
        "minDelayMs": 0,
        "maxDelayMs": 0,
        "minPageDelayMs": 0,
        "maxPageDelayMs": 0,
        "searchPageBurstSize": 99,
        "minSearchPageCooldownMs": 0,
        "maxSearchPageCooldownMs": 0,
        "queryBurstSize": 99,
        "cooldownMinMs": 0,
        "cooldownMaxMs": 0,
        "minQueryDelayMs": 0,
        "maxQueryDelayMs": 0,
        "searchRetryAttempts": 1,
        "listingRetryAttempts": 1,
        "retryBaseDelayMs": 0,
    }
    settings.update(overrides)
    return settings


def _adaptive_listing_reader(candidate, options=None):
    return {
        "title": candidate["title"],
        "description": "Fully remote role requiring Python.",
        "company": "Acme",
        "location": "Remote",
        "canonicalUrl": candidate["canonicalUrl"],
    }


def _ashby_query(query_id: str, needle: str) -> dict:
    return {
        "id": query_id,
        "platform": "Ashby",
        "role": "Python Developer",
        "field": "engineering",
        "type": "junior",
        "query": f"site:jobs.ashbyhq.com {needle}",
        "allowedHosts": ["jobs.ashbyhq.com"],
    }


def test_classify_query_tier_from_recent_crawls() -> None:
    assert classify_query_tier(None) == "average"
    assert classify_query_tier({}) == "average"
    assert (
        classify_query_tier(
            {
                "crawls": [
                    {"mode": "full", "newJobs": 1, "newResults": 3},
                    {"mode": "full", "newJobs": 1, "newResults": 1},
                ]
            }
        )
        == "high"
    )
    assert (
        classify_query_tier(
            {
                "crawls": [
                    {"mode": "full", "newJobs": 0, "newResults": 0},
                    {"mode": "full", "newJobs": 0, "newResults": 0},
                    {"mode": "full", "newJobs": 0, "newResults": 0},
                ]
            }
        )
        == "low"
    )
    assert (
        classify_query_tier({"crawls": [{"mode": "full", "newJobs": 0, "newResults": 2}]})
        == "average"
    )


def test_parked_state_precedence_and_auto_park() -> None:
    zero_full = {"mode": "full", "newJobs": 0, "newResults": 0}
    stats = {"crawls": [zero_full] * 4}
    settings = {"parkAfterZeroFullChecks": 4, "activatedQueries": [], "parkedQueries": []}
    assert _parked_state("q1", stats, settings) is True
    assert _parked_state("q1", stats, {**settings, "activatedQueries": ["q1"]}) is False
    assert _parked_state("q1", {"crawls": [zero_full] * 3}, settings) is False
    assert (
        _parked_state(
            "q2", {"crawls": [{"mode": "full", "newJobs": 0, "newResults": 4}] * 4}, settings
        )
        is False
    )
    assert _parked_state("q3", {}, {**settings, "parkedQueries": ["q3"]}) is True
    shallow_mix = {
        "crawls": [
            zero_full,
            {"mode": "shallow", "newJobs": 0, "newResults": 0},
            zero_full,
            zero_full,
            zero_full,
        ]
    }
    assert _parked_state("q4", shallow_mix, settings) is True


def test_deep_wave_orders_high_before_average_and_skips_parked() -> None:
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    stale = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    coverage_complete = {
        "frontierPage": 5,
        "frontierReason": "exhausted",
        "coveredAt": stale,
        "checkedAt": stale,
        "lastStopReason": "exhausted",
    }
    inventory = [
        _ashby_query("q_avg", "a"),
        _ashby_query("q_high", "b"),
        _ashby_query("q_parked", "c"),
        _ashby_query("q_new", "d"),
    ]
    coverage_map = {
        "q_avg": coverage_complete,
        "q_high": coverage_complete,
        "q_parked": coverage_complete,
    }
    stats_map = {
        "q_high": {"crawls": [{"mode": "full", "newJobs": 2, "newResults": 5}]},
        "q_parked": {"crawls": [{"mode": "full", "newJobs": 0, "newResults": 0}] * 4},
    }
    settings = {
        "fullCheckIntervalDays": 7,
        "activatedQueries": [],
        "parkedQueries": [],
        "parkAfterZeroFullChecks": 4,
    }
    wave = _plan_deep_wave(
        query_inventory=inventory,
        coverage_map=coverage_map,
        stats_map=stats_map,
        settings=settings,
        now_ms=now_ms,
    )
    assert [query["id"] for query in wave] == ["q_high", "q_avg"]


def test_wave_queries_run_first_and_stay_out_of_the_rotation(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    stale = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    state_store.set_query_coverage(
        "qA",
        {
            "frontierPage": 9,
            "frontierReason": "exhausted",
            "coveredAt": stale,
            "checkedAt": stale,
            "lastStopReason": "exhausted",
        },
    )
    state_store.set_query_stats("qA", {"crawls": [{"mode": "full", "newJobs": 3, "newResults": 9}]})

    qa = _ashby_query("qA", "a")
    qb = _ashby_query("qB", "b")
    browser = _PageBrowser()
    provider = _RecordingProvider(PlaywrightGoogleSearchProvider(browser=browser))
    browser.refill(
        [
            {
                "hasNext": True,
                "results": [
                    {
                        "title": "A1",
                        "link": "https://jobs.ashbyhq.com/acme/a-1",
                        "snippet": "Remote",
                    }
                ],
            },
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "A2",
                        "link": "https://jobs.ashbyhq.com/acme/a-2",
                        "snippet": "Remote",
                    }
                ],
            },
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "B1",
                        "link": "https://jobs.ashbyhq.com/acme/b-1",
                        "snippet": "Remote",
                    }
                ],
            },
        ]
    )
    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=_adaptive_listing_reader,
        settings=_fast_settings(),
        query_inventory=[qb, qa],  # the rotation would run qB first; the wave pulls qA forward
    )
    assert run["deepWaveQueued"] == 1
    assert run["deepWaveProcessed"] == ["qA"]
    assert provider.queries == ["qA", "qB"]
    refresh = state_store.get_query_coverage("qA")
    assert refresh["coveredAt"] != stale and refresh["lastStopReason"] == "exhausted"
    state = state_store.read()
    assert state["queryCursorId"] == "qA"  # qB advanced the cursor; the wave item did not


def test_deep_budget_defers_further_full_walks(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "state.json")
    stale = (
        (datetime.now(timezone.utc) - timedelta(days=10))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    for query_id in ("qA", "qB"):
        state_store.set_query_coverage(
            query_id,
            {
                "frontierPage": 4,
                "frontierReason": "exhausted",
                "coveredAt": stale,
                "checkedAt": stale,
                "lastStopReason": "exhausted",
            },
        )
    qa = _ashby_query("qA", "a")
    qb = _ashby_query("qB", "b")
    browser = _PageBrowser()
    provider = _RecordingProvider(PlaywrightGoogleSearchProvider(browser=browser))
    browser.refill(
        [
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "A1",
                        "link": "https://jobs.ashbyhq.com/acme/a-1",
                        "snippet": "Remote",
                    }
                ],
            },
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "B1",
                        "link": "https://jobs.ashbyhq.com/acme/b-1",
                        "snippet": "Remote",
                    }
                ],
            },
        ]
    )
    run = run_discovery(
        state_store=state_store,
        search_provider=provider,
        listing_reader=_adaptive_listing_reader,
        settings=_fast_settings(deepBudgetMinutesPerRun=0.000001),
        query_inventory=[qa, qb],
    )
    assert run["deepWaveQueued"] == 2
    assert run["deepWaveProcessed"] == ["qA"]
    assert provider.queries == ["qA"]
    unchanged = state_store.get_query_coverage("qB")
    assert unchanged["coveredAt"] == stale
