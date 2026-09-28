"""Tests for the ported runner (``job_discovery/src/run.mjs``).

Ported from ``job_discovery/tests/run.test.mjs`` plus extended coverage for
projection sync, reverify, and interrupted-run recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from outbound.job_discovery.runner import (
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
