"""Tests for the ported candidate identity + dedupe (``job_discovery/src/dedupe.mjs``)."""

from __future__ import annotations

from datetime import datetime, timezone

from outbound.job_discovery.dedupe import dedupe_candidates, to_candidate

QUERY = {
    "id": "Ashby:Python Developer:junior",
    "platform": "Ashby",
    "role": "Python Developer",
    "type": "junior",
    "field": "engineering",
}


def _epoch_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def test_normalizes_tracking_urls_and_merges_same_run_query_provenance_before_hydration() -> None:
    first = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=google",
            "snippet": "Remote",
        },
        QUERY,
    )
    second = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh?ref=search",
            "snippet": "Remote",
        },
        {**QUERY, "id": "Ashby:Python Developer:junior-signal"},
    )
    result = dedupe_candidates([first, second])
    assert len(result["uniqueCandidates"]) == 1
    assert sorted(result["uniqueCandidates"][0]["sourceQueries"]) == sorted(
        [QUERY["id"], "Ashby:Python Developer:junior-signal"]
    )
    assert len(result["hydrationQueue"]) == 1
    assert first["canonicalUrl"] == "https://jobs.ashbyhq.com/acme/abcdefgh"
    assert first["jobId"] == second["jobId"]


def test_skips_verified_unchanged_records_before_listing_verification() -> None:
    candidate = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "snippet": "Remote",
        },
        QUERY,
    )
    known = {
        candidate["jobId"]: {
            **candidate,
            "juniorStatus": "verified",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
        }
    }
    assert len(dedupe_candidates([candidate], known)["hydrationQueue"]) == 0


def test_queues_changed_hash_and_unsure_statuses_for_hydration() -> None:
    candidate = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "snippet": "Remote",
        },
        QUERY,
    )
    changed = {candidate["jobId"]: {**candidate, "resultHash": "0" * 24}}
    queued = dedupe_candidates([candidate], changed)["hydrationQueue"]
    assert len(queued) == 1
    unsure = {
        candidate["jobId"]: {
            **candidate,
            "juniorStatus": "unsure",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
        }
    }
    assert len(dedupe_candidates([candidate], unsure)["hydrationQueue"]) == 1


def test_recheck_due_rehydrates_and_recent_check_skips() -> None:
    candidate = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "snippet": "Remote",
        },
        QUERY,
    )
    now = _epoch_ms(2026, 9, 28)
    week = 7 * 24 * 60 * 60 * 1000
    old = {
        candidate["jobId"]: {
            **candidate,
            "juniorStatus": "verified",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
            "verificationCheckedAt": "2026-09-20T00:00:00.000Z",
        }
    }
    assert (
        len(
            dedupe_candidates([candidate], old, recheck_after_ms=week, now_ms=now)["hydrationQueue"]
        )
        == 1
    )
    fresh = {
        candidate["jobId"]: {
            **old[candidate["jobId"]],
            "verificationCheckedAt": "2026-09-25T00:00:00.000Z",
        }
    }
    assert (
        len(
            dedupe_candidates([candidate], fresh, recheck_after_ms=week, now_ms=now)[
                "hydrationQueue"
            ]
        )
        == 0
    )


def test_matches_known_jobs_by_fallback_fingerprint_too() -> None:
    candidate = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "snippet": "Remote",
        },
        QUERY,
    )
    known = {
        "some-other-identity": {
            **candidate,
            "jobId": "some-other-identity",
            "juniorStatus": "verified",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
        }
    }
    assert len(dedupe_candidates([candidate], known)["hydrationQueue"]) == 0


def test_recheck_is_due_when_verified_job_has_no_checked_at() -> None:
    candidate = to_candidate(
        {
            "title": "Junior Python Developer",
            "link": "https://jobs.ashbyhq.com/acme/abcdefgh",
            "snippet": "Remote",
        },
        QUERY,
    )
    known = {
        candidate["jobId"]: {
            **candidate,
            "juniorStatus": "verified",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
        }
    }
    week = 7 * 24 * 60 * 60 * 1000
    result = dedupe_candidates(
        [candidate], known, recheck_after_ms=week, now_ms=_epoch_ms(2026, 9, 28)
    )
    assert len(result["hydrationQueue"]) == 1


def test_to_candidate_identity_chain_and_defaults() -> None:
    candidate = to_candidate(
        {"link": "https://jobs.ashbyhq.com/acme/abcdefgh", "displayLink": None}, QUERY
    )
    assert candidate["title"] == "Untitled job"
    assert candidate["snippet"] == ""
    assert candidate["displayLink"] == "jobs.ashbyhq.com"
    assert candidate["atsJobId"] == "jobs.ashbyhq.com:abcdefgh"
    assert candidate["field"] == "engineering"
    design = to_candidate(
        {"title": "Product Designer", "link": "https://jobs.ashbyhq.com/acme/designerjob"},
        {**QUERY, "field": "design"},
    )
    assert design["field"] == "design"
