"""Tests for the ported state store (``job_discovery/src/state-store.mjs``)."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from outbound.job_discovery.state import StateStore


def test_migrates_json_state_into_indexed_sqlite_tables_and_records_raw_search_history_locally(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "state.json"
    legacy_path.write_text(
        json.dumps(
            {
                "jobs": {
                    "abc": {
                        "jobId": "abc",
                        "canonicalUrl": "https://jobs.ashbyhq.com/acme/abcde",
                        "fallbackFingerprint": "fallback",
                    }
                },
                "queryCursor": 7,
            }
        )
    )
    store = StateStore(legacy_path)
    state = store.read()
    assert state["queryCursor"] == 7
    assert state["jobs"]["abc"]["jobId"] == "abc"
    store.record_search_results(
        "q1", [{"title": "Job", "link": "https://jobs.ashbyhq.com/acme/abcde", "snippet": "Remote"}]
    )
    assert store.db is not None
    assert store.db.execute("SELECT COUNT(*) AS count FROM search_results").fetchone()[0] == 1
    assert str(store.file_path).endswith(".sqlite")
    store.close()


def test_recovers_a_lock_owned_by_a_dead_local_process(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.ensure_database()
    Path(store.lock_path).write_text(
        json.dumps({"runId": "dead", "pid": 99999999, "hostname": socket.gethostname()})
    )
    assert store.acquire_run_lock("replacement") is True
    store.release_run_lock()
    assert not Path(store.lock_path).exists()
    store.close()


def test_tracks_only_changed_final_sheet_projections_for_incremental_synchronization(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.json")
    rows = [
        {"id": "job-1", "values": ["job-1", "First"]},
        {"id": "job-2", "values": ["job-2", "Second"]},
    ]
    assert len(store.projection_changes("Jobs", rows)) == 2
    store.mark_projection_synced("Jobs", rows)
    assert len(store.projection_changes("Jobs", rows)) == 0
    changed = [{"id": "job-1", "values": ["job-1", "Updated"]}, rows[1]]
    assert [row["id"] for row in store.projection_changes("Jobs", changed)] == ["job-1"]
    store.close()


def test_resets_durable_records_raw_search_history_and_legacy_state(tmp_path: Path) -> None:
    legacy_path = tmp_path / "state.json"
    store = StateStore(legacy_path)
    store.write(
        {
            "jobs": {"job": {"jobId": "job", "canonicalUrl": "https://example.com/job"}},
            "companies": {"company": {"companyId": "company"}},
            "runs": {"run": {"id": "run"}},
            "queryProgress": {"query": {"id": "query"}},
            "liveTestUsage": {"query": 2},
            "queryCursor": 9,
            "queryCursorId": "query",
        }
    )
    store.record_search_results("query", [{"title": "Job", "link": "https://example.com/job"}])
    store.mark_projection_synced("Jobs", [{"id": "job", "values": ["job"]}])

    stats = store.reset()
    state = store.read()
    assert stats["jobs"] == 0
    assert stats["companies"] == 0
    assert stats["runs"] == 0
    assert stats["queryProgress"] == 0
    assert stats["searchResults"] == 0
    assert state["queryCursor"] == 0
    assert state["liveTestUsage"] == {}
    assert len(store.projection_changes("Jobs", [{"id": "job", "values": ["job"]}])) == 1
    assert json.loads(legacy_path.read_text())["jobs"] == {}
    store.close()


def test_acquire_lock_fails_while_live_holder_exists_and_heartbeat_validates_owner(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.ensure_database()
    Path(store.lock_path).write_text(
        json.dumps({"runId": "holder", "pid": os.getpid(), "hostname": socket.gethostname()})
    )
    assert store.acquire_run_lock("me") is False
    store.heartbeat_run_lock("holder")
    with pytest.raises(RuntimeError):
        store.heartbeat_run_lock("someone-else")
    Path(store.lock_path).write_text("not json")
    with pytest.raises(RuntimeError):
        store.heartbeat_run_lock("holder")
    store.release_run_lock()
    store.close()


def test_lock_goes_stale_by_age(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.ensure_database()
    lock_path = Path(store.lock_path)
    lock_path.write_text(json.dumps({"runId": "old", "pid": os.getpid(), "hostname": "other-host"}))
    old_time = time.time() - 100
    os.utime(lock_path, (old_time, old_time))
    assert store.acquire_run_lock("me", stale_after_ms=5000) is True
    store.release_run_lock()
    store.close()


def test_lookup_jobs_by_id_and_fallback_fingerprint(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.write(
        {
            "jobs": {
                "a": {"jobId": "a", "fallbackFingerprint": "fp-a"},
                "b": {"jobId": "b", "fallbackFingerprint": "fp-b"},
            }
        }
    )
    found = store.lookup_jobs(
        [
            {"jobId": "a", "fallbackFingerprint": "x"},
            {"jobId": "missing", "fallbackFingerprint": "fp-b"},
        ]
    )
    assert found["a"]["jobId"] == "a"
    assert found["b"]["jobId"] == "b"
    assert "missing" not in found
    store.close()


def test_write_reconciles_deleted_records_and_read_returns_blank_defaults(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    fresh = store.read()
    assert fresh["queryCursor"] == 0
    assert fresh["queryCycle"] == 1
    assert fresh["activeRunId"] is None
    assert fresh["schemaVersion"] == 3
    store.write({"jobs": {"a": {"jobId": "a"}, "b": {"jobId": "b"}}})
    store.write({"jobs": {"b": {"jobId": "b"}}})
    assert list(store.read()["jobs"]) == ["b"]
    store.close()


def test_projection_reconcile_window_forces_resync(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    rows = [{"id": "job-1", "values": ["job-1"]}]
    store.mark_projection_synced("Jobs", rows)
    assert len(store.projection_changes("Jobs", rows)) == 0
    assert len(store.projection_changes("Jobs", rows, reconcile_after_ms=0)) == 1
    store.close()


def test_schema_tables_and_indexes_exist(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.ensure_database()
    assert store.db is not None
    names = {
        row[0]
        for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    assert {
        "metadata",
        "jobs",
        "companies",
        "runs",
        "query_progress",
        "live_test_usage",
        "search_results",
        "projection_sync",
        "jobs_fallback_idx",
        "jobs_url_idx",
        "runs_sync_idx",
        "search_results_url_idx",
        "search_results_result_hash_idx",
    } <= names
    store.close()
