"""SQLite state store ported from ``job_discovery/src/state-store.mjs``.

Local-first durable state: metadata, jobs/companies/runs/query-progress
records, raw search history, live-test usage, and projection-sync bookkeeping,
plus the single-run lock (exclusive create, dead-pid + age staleness recovery,
heartbeat).

The JS implementation is async; this port is synchronous -- the natural shape
for stdlib SQLite work and how the runner will use it. Timestamps use the
reference's ISO-8601 UTC format (``...T...Z``) and JSON payloads are stored
compactly (``ensure_ascii=False`` separators), matching ``JSON.stringify``.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outbound.job_discovery.urls import stable_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, canonical_url TEXT, fallback_fingerprint TEXT, result_hash TEXT,
  status TEXT, last_seen_at TEXT, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_fallback_idx ON jobs(fallback_fingerprint);
CREATE INDEX IF NOT EXISTS jobs_url_idx ON jobs(canonical_url);
CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT, status TEXT, sheet_synced_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_sync_idx ON runs(sheet_synced_at);
CREATE TABLE IF NOT EXISTS query_progress (id TEXT PRIMARY KEY, status TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_test_usage (query_id TEXT PRIMARY KEY, uses INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS search_results (
  query_id TEXT NOT NULL, url_hash TEXT NOT NULL, url TEXT NOT NULL, title TEXT, snippet TEXT,
  result_hash TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(query_id, url_hash)
);
CREATE INDEX IF NOT EXISTS search_results_url_idx ON search_results(url_hash);
CREATE INDEX IF NOT EXISTS search_results_result_hash_idx ON search_results(result_hash);
CREATE TABLE IF NOT EXISTS projection_sync (
  target TEXT NOT NULL, id TEXT NOT NULL, projection_hash TEXT NOT NULL, synced_at TEXT NOT NULL,
  PRIMARY KEY(target, id)
);
"""

_METADATA_KEYS = (
    "queryCursor",
    "queryCursorId",
    "queryCycle",
    "lastScheduledDate",
    "activeRunId",
    "schemaVersion",
)
_CACHE_TABLES = ("jobs", "companies", "runs", "query_progress")
_ALL_TABLES = (
    "metadata",
    "jobs",
    "companies",
    "runs",
    "query_progress",
    "live_test_usage",
    "search_results",
    "projection_sync",
)
_DEFAULT_LOCK_STALE_MS = 6 * 60 * 60 * 1000
_DEFAULT_RECONCILE_MS = 7 * 24 * 60 * 60 * 1000


def blank_state() -> dict[str, Any]:
    """Fresh top-level state (same shape as the reference's ``blankState``)."""
    return {
        "jobs": {},
        "companies": {},
        "runs": {},
        "liveTestUsage": {},
        "queryProgress": {},
        "queryCursor": 0,
        "queryCursorId": "",
        "queryCycle": 1,
        "lastScheduledDate": "",
        "activeRunId": None,
        "schemaVersion": 3,
    }


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso_ms(value: Any) -> float:
    """ISO timestamp -> epoch ms; NaN when unparsable (JS Date.parse semantics)."""
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


def _process_is_alive(pid: Any) -> bool:
    try:
        number = float(pid)
    except (TypeError, ValueError):
        return False
    if not number.is_integer() or number <= 0:
        return False
    try:
        os.kill(int(number), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class StateStore:
    """Port of the reference ``StateStore`` (sync API; see module docstring)."""

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        resolved = str(Path(file_path).resolve())
        if resolved.endswith(".json"):
            self.legacy_json_path: str | None = resolved
            self.file_path = re.sub(r"\.json$", ".sqlite", resolved, flags=re.IGNORECASE)
        else:
            self.legacy_json_path = None
            self.file_path = resolved
        self.lock_path = f"{self.file_path}.lock"
        self.db: sqlite3.Connection | None = None
        self.cache: dict[str, str] = {}

    def ensure_database(self) -> None:
        if self.db is not None:
            return
        Path(self.file_path).parent.mkdir(parents=True, exist_ok=True)
        legacy: dict[str, Any] | None = None
        if self.legacy_json_path:
            try:
                legacy = json.loads(Path(self.legacy_json_path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                legacy = None
        self.db = sqlite3.connect(self.file_path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.executescript(_SCHEMA)
        self._reload_cache()
        has_state = self.db.execute("SELECT 1 AS present FROM metadata LIMIT 1").fetchone()
        if not has_state and legacy:
            self.write({**blank_state(), **legacy})

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    # ------------------------------------------------------------------ locks

    def acquire_run_lock(
        self, run_id: str, *, stale_after_ms: int = _DEFAULT_LOCK_STALE_MS
    ) -> bool:
        self.ensure_database()
        now = _iso_now()
        lock = {
            "runId": run_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "createdAt": now,
            "heartbeatAt": now,
        }
        for _attempt in range(2):
            try:
                with open(self.lock_path, "x", encoding="utf-8") as handle:
                    handle.write(f"{_json(lock)}\n")
                return True
            except FileExistsError:
                stale = False
                try:
                    source = Path(self.lock_path).read_text(encoding="utf-8")
                    mtime_ms = os.stat(self.lock_path).st_mtime * 1000
                    try:
                        existing = json.loads(source)
                    except json.JSONDecodeError:
                        existing = {}
                    same_host_dead_process = bool(
                        existing.get("hostname") == socket.gethostname()
                        and existing.get("pid")
                        and not _process_is_alive(existing.get("pid"))
                    )
                    stale = same_host_dead_process or (
                        time.time() * 1000 - mtime_ms > stale_after_ms
                    )
                except FileNotFoundError:
                    stale = True
                if not stale:
                    return False
                try:
                    os.unlink(self.lock_path)
                except FileNotFoundError:
                    pass
        return False

    def heartbeat_run_lock(self, run_id: str) -> None:
        source = Path(self.lock_path).read_text(encoding="utf-8")
        try:
            lock = json.loads(source)
        except json.JSONDecodeError as error:
            raise RuntimeError("Run lock is malformed") from error
        if lock.get("runId") != run_id:
            raise RuntimeError(f"Run lock belongs to {lock.get('runId') or 'another process'}")
        lock["heartbeatAt"] = _iso_now()
        Path(self.lock_path).write_text(f"{_json(lock)}\n", encoding="utf-8")

    def release_run_lock(self) -> None:
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------- data

    def _reload_cache(self) -> None:
        self.cache.clear()
        assert self.db is not None
        for table in _CACHE_TABLES:
            for row in self.db.execute(f"SELECT id, data FROM {table}").fetchall():
                self.cache[f"{table}:{row[0]}"] = row[1]

    def rows_as_map(self, table: str) -> dict[str, Any]:
        assert self.db is not None
        return {
            row[0]: json.loads(row[1])
            for row in self.db.execute(f"SELECT id, data FROM {table}").fetchall()
        }

    def read(self) -> dict[str, Any]:
        self.ensure_database()
        assert self.db is not None
        state = blank_state()
        for key, value in self.db.execute("SELECT key, value FROM metadata").fetchall():
            state[key] = json.loads(value)
        state["jobs"] = self.rows_as_map("jobs")
        state["companies"] = self.rows_as_map("companies")
        state["runs"] = self.rows_as_map("runs")
        state["queryProgress"] = self.rows_as_map("query_progress")
        state["liveTestUsage"] = {
            row[0]: row[1]
            for row in self.db.execute("SELECT query_id, uses FROM live_test_usage").fetchall()
        }
        return state

    def sync_records(
        self,
        table: str,
        records: dict[str, Any],
        columns: list[tuple[str, Callable[[Any], Any]]],
    ) -> None:
        assert self.db is not None
        ids = set(records)
        existing = [row[0] for row in self.db.execute(f"SELECT id FROM {table}").fetchall()]
        column_names = ["id", *(name for name, _ in columns), "data"]
        placeholders = ",".join("?" for _ in column_names)
        updates = ",".join(
            [f"{name}=excluded.{name}" for name, _ in columns] + ["data=excluded.data"]
        )
        sql = (
            f"INSERT INTO {table}({','.join(column_names)}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        cursor = self.db.cursor()
        for record_id, value in records.items():
            data = _json(value)
            cache_key = f"{table}:{record_id}"
            if self.cache.get(cache_key) == data:
                continue
            cursor.execute(sql, [record_id, *(getter(value) for _, getter in columns), data])
            self.cache[cache_key] = data
        for record_id in existing:
            if record_id not in ids:
                cursor.execute(f"DELETE FROM {table} WHERE id = ?", [record_id])
                self.cache.pop(f"{table}:{record_id}", None)

    def _transaction(self, action: Callable[[], Any]) -> Any:
        assert self.db is not None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            self._reload_cache()
            raise

    def write(self, state: dict[str, Any]) -> None:
        self.ensure_database()
        assert self.db is not None
        blank = blank_state()

        def action() -> None:
            cursor = self.db.cursor()
            for key in _METADATA_KEYS:
                value = state.get(key)
                if value is None:
                    value = blank[key]
                cursor.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [key, _json(value)],
                )
            self.sync_records(
                "jobs",
                state.get("jobs") or {},
                [
                    ("canonical_url", lambda item: item.get("canonicalUrl") or ""),
                    ("fallback_fingerprint", lambda item: item.get("fallbackFingerprint") or ""),
                    ("result_hash", lambda item: item.get("resultHash") or ""),
                    ("status", lambda item: item.get("status") or ""),
                    ("last_seen_at", lambda item: item.get("lastSeenAt") or ""),
                ],
            )
            self.sync_records("companies", state.get("companies") or {}, [])
            self.sync_records(
                "runs",
                state.get("runs") or {},
                [
                    ("started_at", lambda item: item.get("startedAt") or ""),
                    ("status", lambda item: item.get("status") or ""),
                    ("sheet_synced_at", lambda item: item.get("sheetSyncedAt") or None),
                ],
            )
            self.sync_records(
                "query_progress",
                state.get("queryProgress") or {},
                [("status", lambda item: item.get("status") or "")],
            )
            usage_cursor = self.db.cursor()
            for query_id, uses in (state.get("liveTestUsage") or {}).items():
                try:
                    number = float(uses)
                except (TypeError, ValueError):
                    number = 0.0
                if not math.isfinite(number):
                    number = 0.0
                value = int(number) if number.is_integer() else number
                usage_cursor.execute(
                    "INSERT INTO live_test_usage(query_id,uses) VALUES(?,?) "
                    "ON CONFLICT(query_id) DO UPDATE SET uses=excluded.uses",
                    [query_id, value],
                )

        self._transaction(action)

    def record_search_results(self, query_id: str, results: list[dict] | None) -> None:
        if not results:
            return
        self.ensure_database()
        assert self.db is not None
        now = _iso_now()
        sql = (
            "INSERT INTO search_results(query_id,url_hash,url,title,snippet,result_hash,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(query_id,url_hash) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,"
            "result_hash=excluded.result_hash,last_seen_at=excluded.last_seen_at"
        )

        def action() -> None:
            cursor = self.db.cursor()
            for item in results:
                title = item.get("title") or ""
                snippet = item.get("snippet") or ""
                link = item.get("link") or ""
                cursor.execute(
                    sql,
                    [
                        query_id,
                        stable_hash(link),
                        link,
                        title,
                        snippet,
                        stable_hash(f"{title}|{snippet}|{link}"),
                        now,
                        now,
                    ],
                )

        self._transaction(action)

    def delete_search_results_for_query(self, query_id: str) -> None:
        self.ensure_database()
        assert self.db is not None
        self.db.execute("DELETE FROM search_results WHERE query_id = ?", [query_id])

    def lookup_jobs(self, candidates: list[dict]) -> dict[str, Any]:
        self.ensure_database()
        assert self.db is not None
        by_id = self.db.cursor()
        by_fallback = self.db.cursor()
        found: dict[str, Any] = {}
        for candidate in candidates:
            row = by_id.execute(
                "SELECT id, data FROM jobs WHERE id = ?", [candidate["jobId"]]
            ).fetchone()
            if not row and candidate.get("fallbackFingerprint"):
                row = by_fallback.execute(
                    "SELECT id, data FROM jobs WHERE fallback_fingerprint = ? LIMIT 1",
                    [candidate["fallbackFingerprint"]],
                ).fetchone()
            if row:
                found[row[0]] = json.loads(row[1])
        return found

    def stats(self) -> dict[str, Any]:
        self.ensure_database()
        assert self.db is not None

        def count(table: str) -> int:
            return int(self.db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[0])

        return {
            "databasePath": self.file_path,
            "jobs": count("jobs"),
            "companies": count("companies"),
            "runs": count("runs"),
            "queryProgress": count("query_progress"),
            "searchResults": count("search_results"),
        }

    def reset(self) -> dict[str, Any]:
        self.ensure_database()
        assert self.db is not None

        def action() -> None:
            cursor = self.db.cursor()
            for table in _ALL_TABLES:
                cursor.execute(f"DELETE FROM {table}")

        self._transaction(action)
        self.cache.clear()
        if self.legacy_json_path:
            Path(self.legacy_json_path).write_text(f"{_json(blank_state())}\n", encoding="utf-8")
        self.write(blank_state())
        return self.stats()

    # ------------------------------------------------------------- projections

    def projection_changes(
        self,
        target: str,
        records: list[dict],
        *,
        reconcile_after_ms: int = _DEFAULT_RECONCILE_MS,
    ) -> list[dict]:
        self.ensure_database()
        assert self.db is not None
        existing = {
            row[0]: {"projection_hash": row[1], "synced_at": row[2]}
            for row in self.db.execute(
                "SELECT id, projection_hash, synced_at FROM projection_sync WHERE target = ?",
                [target],
            ).fetchall()
        }
        now_ms = time.time() * 1000
        changed: list[dict] = []
        for record in records:
            previous = existing.get(record["id"])
            projection_hash = stable_hash(_json(record["values"]))
            if (
                previous is None
                or previous["projection_hash"] != projection_hash
                or now_ms - _parse_iso_ms(previous["synced_at"]) >= reconcile_after_ms
            ):
                changed.append(record)
        return changed

    def mark_projection_synced(self, target: str, records: list[dict]) -> None:
        if not records:
            return
        self.ensure_database()
        assert self.db is not None
        sql = (
            "INSERT INTO projection_sync(target,id,projection_hash,synced_at) VALUES(?,?,?,?) "
            "ON CONFLICT(target,id) DO UPDATE SET projection_hash=excluded.projection_hash,"
            "synced_at=excluded.synced_at"
        )
        synced_at = _iso_now()

        def action() -> None:
            cursor = self.db.cursor()
            for record in records:
                cursor.execute(
                    sql, [target, record["id"], stable_hash(_json(record["values"])), synced_at]
                )

        self._transaction(action)
