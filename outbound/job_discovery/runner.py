"""Discovery-run orchestration ported from ``job_discovery/src/run.mjs``.

The runner is synchronous (the reference is async): locking, checkpoints and
pacing sleeps are blocking calls, which is how the CLI uses the port. All
durable state flows through the verified StateStore; all pure logic (dedupe,
signals, querying) reuses the earlier phases.

One deliberate note: ``DEFAULTS`` is merged into settings *without* running
normalization, mirroring the reference (callers normalize first).
"""

from __future__ import annotations

import json
import math
import random
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from outbound.job_discovery.dedupe import dedupe_candidates, to_candidate
from outbound.job_discovery.listing import read_listing
from outbound.job_discovery.providers import with_pagination_stop
from outbound.job_discovery.queries import build_queries
from outbound.job_discovery.settings import DEFAULTS
from outbound.job_discovery.sheets import company_row, job_row, run_row
from outbound.job_discovery.signals import verify_signals
from outbound.job_discovery.urls import (
    is_career_landing_page_url,
    is_job_posting_candidate,
    normalized_text,
    stable_hash,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _random_between(minimum: float, maximum: float) -> float:
    return minimum + math.floor(random.random() * max(1, maximum - minimum + 1))


def _n(value: object) -> float:
    """JS ``Number(value) || 0`` (int-normalized)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number == 0:
        return 0
    return int(number) if number.is_integer() else number


def _num_or(value: object, fallback: float) -> float:
    """JS ``Number(value) || fallback`` (0 and NaN are falsy)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or number == 0:
        return fallback
    return int(number) if number.is_integer() else number


def _coalesce(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _always_false() -> bool:
    return False


def _log_error(logger, message: str) -> None:
    if logger is None:
        print(message, file=sys.stderr)
    elif hasattr(logger, "error"):
        logger.error(message)
    elif callable(logger):
        logger(message)


def _discovery_field(value: object) -> str:
    return "design" if value == "design" else "engineering"


def _date_key_in_timezone(value: str | datetime, timezone_name: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    else:
        moment = value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")


def _completed_hydrations_today(state: dict, *, timezone_name: str, excluding_run_id: str) -> dict:
    today = _date_key_in_timezone(datetime.now(timezone.utc), timezone_name)
    total = 0
    by_field = {"engineering": 0, "design": 0}
    for prior_run in (state.get("runs") or {}).values():
        if (
            prior_run.get("id") == excluding_run_id
            or prior_run.get("trigger") == "smoke-test"
            or not prior_run.get("startedAt")
        ):
            continue
        if _date_key_in_timezone(prior_run["startedAt"], timezone_name) != today:
            continue
        fields = prior_run.get("hydratedByField") or {"engineering": _n(prior_run.get("hydrated"))}
        for field, value in fields.items():
            by_field[_discovery_field(field)] += _n(value)
        total += _n(prior_run.get("hydrated"))
    return {"total": total, "byField": by_field}


def _field_quota(settings: dict, field: object) -> float:
    maximum = _n(settings.get("maxListingsPerRun"))
    half = math.floor(maximum / 2)
    return half if _discovery_field(field) == "design" else maximum - half


def _merge_sources(previous: list | None = None, current: list | None = None) -> list:
    return list(dict.fromkeys([*(previous or []), *(current or [])]))


def _company_identity(company: str, canonical_url: str) -> dict:
    domain = ""
    try:
        domain = re.sub(r"^www\.", "", urlsplit(canonical_url).hostname or "")
    except ValueError:  # candidate URLs are normalized before this point
        domain = ""
    return {
        "companyId": stable_hash(f"company:{normalized_text(company)}:{domain}"),
        "companyDomain": domain,
    }


def _refresh_company(companies: dict, job: dict) -> str:
    identity = _company_identity(job.get("company") or "", job.get("canonicalUrl") or "")
    existing = companies.get(identity["companyId"])
    if existing is None:
        existing = {
            "companyId": identity["companyId"],
            "companyName": job.get("company"),
            "companyDomain": identity["companyDomain"],
            "platforms": [],
            "careerUrls": [],
            "firstSeenAt": job.get("firstSeenAt"),
            "notes": "",
        }
    existing["companyName"] = job.get("company") or existing.get("companyName")
    existing["companyDomain"] = identity["companyDomain"] or existing.get("companyDomain")
    existing["platforms"] = list(
        dict.fromkeys([*(existing.get("platforms") or []), job.get("platform")])
    )
    parts = urlsplit(job["canonicalUrl"])
    existing["careerUrls"] = list(
        dict.fromkeys([*(existing.get("careerUrls") or []), f"{parts.scheme}://{parts.netloc}"])
    )
    existing["lastSeenAt"] = job.get("lastSeenAt")
    existing["remoteHiringSignal"] = (
        "verified"
        if job.get("remoteStatus") == "verified"
        else existing.get("remoteHiringSignal") or "unsure"
    )
    existing["status"] = "active"
    companies[identity["companyId"]] = existing
    return identity["companyId"]


def _company_name_from_career_url(url: str, fallback: str = "") -> str:
    try:
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
        value = unquote(segments[0] if segments else "", errors="strict")
        name = re.sub(r"\b\w", lambda match: match.group(0).upper(), re.sub(r"[-_]+", " ", value))
        return name or fallback
    except (ValueError, UnicodeDecodeError):
        return fallback


def _record_company_board(
    state: dict, candidate: dict, now: str, reason: str = "company_careers_landing_page"
) -> bool:
    previous = state["jobs"].get(candidate["jobId"])
    job = {
        **candidate,
        "title": candidate.get("title") or (previous or {}).get("title") or "Company careers board",
        "description": candidate.get("snippet") or (previous or {}).get("description") or "",
        "company": (previous or {}).get("company")
        or _company_name_from_career_url(
            candidate["canonicalUrl"], candidate.get("displayLink") or ""
        ),
        "location": "",
        "juniorStatus": "not_applicable",
        "remoteStatus": "not_applicable",
        "pythonStatus": "not_applicable",
        "evidenceText": "Company careers landing page; not an individual job posting.",
        "score": 0,
        "reviewReason": "Company careers landing page, not a job posting",
        "closed": False,
        "firstSeenAt": (previous or {}).get("firstSeenAt") or now,
        "lastSeenAt": now,
        "verificationCheckedAt": now,
        "misses": 0,
        "status": "company_board",
        "recordType": reason,
    }
    job["companyId"] = _refresh_company(state["companies"], job)
    state["jobs"][job["jobId"]] = job
    return previous is None


def _recalculate_companies(state: dict) -> None:
    for company in state["companies"].values():
        company["activeJobCount"] = 0
        company["remoteHiringSignal"] = "unsure"
    for job in state["jobs"].values():
        company = state["companies"].get(job.get("companyId"))
        if company is not None:
            if job.get("status") in ("active", "review", "possibly_closed"):
                company["activeJobCount"] += 1
            if job.get("remoteStatus") == "verified":
                company["remoteHiringSignal"] = "verified"
    for company in state["companies"].values():
        company["status"] = "active" if company["activeJobCount"] else "inactive"


def _query_sequence(all_queries: list, state: dict, supplied_queries: list | None) -> list:
    if supplied_queries:
        return supplied_queries
    id_index = -1
    if state.get("queryCursorId"):
        for index, query in enumerate(all_queries):
            if query["id"] == state["queryCursorId"]:
                id_index = index
                break
    cursor = (
        id_index
        if id_index >= 0
        else min(_n(state.get("queryCursor")), max(0, len(all_queries) - 1))
    )
    cursor = int(cursor)
    return [*all_queries[cursor:], *all_queries[:cursor]]


def _advance_query_cursor(state: dict, all_queries: list, query: dict) -> None:
    index = next((i for i, item in enumerate(all_queries) if item["id"] == query["id"]), -1)
    if index < 0:
        return
    next_index = (index + 1) % len(all_queries)
    state["queryCursor"] = next_index
    state["queryCursorId"] = all_queries[next_index]["id"] if next_index < len(all_queries) else ""
    if next_index == 0:
        state["queryCycle"] = _n(state.get("queryCycle")) + 1


def _record_query_misses(
    state: dict, query: dict, seen_job_ids: set, close_after_misses: object
) -> None:
    threshold = _n(close_after_misses)
    for job in state["jobs"].values():
        if job.get("status") in ("test", "company_board") or query["id"] not in (
            job.get("sourceQueries") or []
        ):
            continue
        if job.get("jobId") in seen_job_ids:
            job["misses"] = 0
            continue
        job["misses"] = _n(job.get("misses")) + 1
        job["status"] = "closed" if job["misses"] >= threshold else "possibly_closed"


_COMPLETE_STOPS = ("exhausted", "low_yield", "known_frontier")
_FRONTIER_STOPS = ("exhausted", "low_yield")


def _parse_iso_ms(value: object) -> float:
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


def _decide_walk_mode(coverage: dict | None, *, now_ms: float, interval_days: object) -> str:
    """Shallow only for fresh, complete coverage; otherwise walk full
    (docs/SEARCH-DEPTH-FIX.md §2)."""
    if not coverage or coverage.get("lastStopReason") not in _COMPLETE_STOPS:
        return "full"
    if coverage.get("frontierPage") is None or not coverage.get("coveredAt"):
        return "full"
    age_ms = now_ms - _parse_iso_ms(coverage.get("coveredAt"))
    if not math.isfinite(age_ms):
        return "full"
    interval_ms = max(1.0, _n(interval_days)) * 24 * 60 * 60 * 1000
    return "shallow" if age_ms < interval_ms else "full"


def _update_crawl_memory(
    *,
    state_store,
    query_id: str,
    results: list,
    walk_mode: str,
    known_urls: set,
    previous_coverage: dict | None,
    last_page_hint: int | None,
    new_jobs: float,
    hydrated: float,
    minutes: float,
) -> None:
    """Persist coverage + rolling crawl stats after a rotational crawl completes."""
    stop = getattr(results, "pagination_stop", None) or ""
    last_page = getattr(results, "last_page", None)
    if last_page is None:
        last_page = last_page_hint
    set_coverage = getattr(state_store, "set_query_coverage", None)
    if set_coverage:
        previous = previous_coverage or {}
        coverage = {
            "frontierPage": previous.get("frontierPage"),
            "frontierReason": previous.get("frontierReason"),
            "coveredAt": previous.get("coveredAt"),
            "checkedAt": _iso_now(),
            "lastStopReason": stop,
        }
        if stop in _FRONTIER_STOPS and last_page is not None:
            coverage["frontierPage"] = last_page
            coverage["frontierReason"] = stop
            coverage["coveredAt"] = _iso_now()
        set_coverage(query_id, coverage)
    get_stats = getattr(state_store, "get_query_stats", None)
    set_stats = getattr(state_store, "set_query_stats", None)
    if get_stats and set_stats:
        stats = get_stats(query_id) or {}
        crawls = list(stats.get("crawls") or [])
        crawls.append(
            {
                "at": _iso_now(),
                "mode": walk_mode,
                "pages": (last_page + 1) if isinstance(last_page, int) else None,
                "results": len(results),
                "newResults": sum(1 for item in results if item.get("link") not in known_urls),
                "newJobs": new_jobs,
                "hydrated": hydrated,
                "minutes": minutes,
            }
        )
        set_stats(query_id, {**stats, "crawls": crawls[-8:]})


def _wait_with_heartbeat(
    ms: float, *, state_store, run_id: str, should_stop: Callable[[], bool]
) -> None:
    remaining = max(0, ms)
    while remaining > 0 and not should_stop():
        chunk = min(remaining, 30_000)
        time.sleep(chunk / 1000)
        remaining -= chunk
        state_store.heartbeat_run_lock(run_id)


def sync_state_to_sheets(sheets, state: dict, state_store) -> None:
    """Port of ``syncStateToSheets`` (incremental projections + fallbacks)."""
    final_jobs = [job for job in state["jobs"].values() if job.get("status") != "test"]
    final_company_ids = {job.get("companyId") for job in final_jobs}
    final_companies = [
        company
        for company in state["companies"].values()
        if company.get("companyId") in final_company_ids
    ]
    all_job_rows = [{"id": job["jobId"], "values": job_row(job)} for job in final_jobs]
    all_company_rows = [
        {"id": company["companyId"], "values": company_row(company)} for company in final_companies
    ]
    projection_changes = getattr(state_store, "projection_changes", None)
    jobs = projection_changes("Jobs", all_job_rows) if projection_changes else all_job_rows
    companies = (
        projection_changes("Companies", all_company_rows)
        if projection_changes
        else all_company_rows
    )
    reviews = [
        [
            job.get("jobId"),
            job.get("company"),
            job.get("title"),
            job.get("canonicalUrl"),
            job.get("reviewReason"),
            job.get("juniorStatus"),
            job.get("remoteStatus"),
            job.get("pythonStatus"),
            job.get("verificationCheckedAt"),
        ]
        for job in final_jobs
        if job.get("status") == "review"
    ]
    pending_runs = [item for item in state["runs"].values() if not item.get("sheetSyncedAt")]
    runs = [{"id": item["id"], "values": run_row(item)} for item in pending_runs]
    sync_projection = getattr(sheets, "sync_projection", None)
    projection = (
        sync_projection(
            {
                "jobIds": [job["jobId"] for job in final_jobs],
                "companyIds": [company["companyId"] for company in final_companies],
                "jobs": jobs,
                "companies": companies,
                "reviews": reviews,
                "runs": runs,
            }
        )
        if sync_projection
        else None
    )
    if not (projection or {}).get("supported"):
        retain = getattr(sheets, "retain", None)
        if retain:
            retain("Jobs", [job["jobId"] for job in final_jobs])
            retain("Companies", [company["companyId"] for company in final_companies])
        sheets.upsert("Jobs", jobs)
        sheets.upsert("Companies", companies)
        replace = getattr(sheets, "replace", None)
        if replace:
            replace("Review Queue", reviews)
        elif reviews:
            sheets.upsert(
                "Review Queue", [{"id": values[0], "values": values} for values in reviews]
            )
        sheets.upsert("Runs", runs)
    mark = getattr(state_store, "mark_projection_synced", None)
    if mark:
        mark("Jobs", jobs)
        mark("Companies", companies)
    synced_at = _iso_now()
    for item in pending_runs:
        item["sheetSyncedAt"] = synced_at


def reverify_stored_jobs(*, state_store, sheets=None, signal_rules: dict | None = None) -> dict:
    """Port of ``reverifyStoredJobs`` (no network; re-classify + re-verify)."""
    run_id = f"reverify_{int(time.time() * 1000)}"
    stale_after_ms = 360 * 60_000
    if not state_store.acquire_run_lock(run_id, stale_after_ms=stale_after_ms):
        raise RuntimeError("A run is already active.")
    try:
        state = state_store.read()
        if state.get("activeRunId"):
            raise RuntimeError("A discovery run is already active.")
        checked = 0
        changed = 0
        for job in state["jobs"].values():
            if job.get("status") == "test":
                continue
            if is_career_landing_page_url(job.get("canonicalUrl", ""), job.get("platform", "")):
                fields = ("status", "reviewReason", "remoteStatus", "juniorStatus", "pythonStatus")
                before = json.dumps({key: job.get(key) for key in fields})
                job.update(
                    {
                        "status": "company_board",
                        "recordType": "company_careers_landing_page",
                        "reviewReason": "Company careers landing page, not a job posting",
                        "juniorStatus": "not_applicable",
                        "remoteStatus": "not_applicable",
                        "pythonStatus": "not_applicable",
                        "evidenceText": "Company careers landing page; not an individual job posting.",
                        "score": 0,
                        "verificationCheckedAt": _iso_now(),
                    }
                )
                if before != json.dumps({key: job.get(key) for key in fields}):
                    changed += 1
                checked += 1
                continue
            signals = verify_signals(
                {
                    "title": job.get("title"),
                    "description": job.get("description"),
                    "location": job.get("location"),
                    "role": job.get("role"),
                },
                signal_rules or {},
            )
            fields = (
                "juniorStatus",
                "remoteStatus",
                "pythonStatus",
                "evidenceText",
                "score",
                "reviewReason",
                "status",
            )
            before = json.dumps({key: job.get(key) for key in fields})
            job.update(signals)
            job["verificationCheckedAt"] = _iso_now()
            if job.get("status") in ("active", "review"):
                job["status"] = "review" if signals["reviewReason"] else "active"
            if before != json.dumps({key: job.get(key) for key in fields}):
                changed += 1
            checked += 1
        _recalculate_companies(state)
        state_store.write(state)
        if sheets:
            sync_state_to_sheets(sheets, state, state_store)
            state_store.write(state)
        return {"checked": checked, "changed": changed, "sheetSynced": bool(sheets)}
    finally:
        state_store.release_run_lock()


def run_discovery(
    *,
    trigger: str = "manual",
    state_store,
    search_provider,
    sheets=None,
    settings: dict | None = None,
    query_inventory: list | None = None,
    queries: list | None = None,
    listing_reader: Callable | None = None,
    signal_rules: dict | None = None,
    test_mode: bool = False,
    force_hydration: bool = False,
    should_stop: Callable[[], bool] | None = None,
    logger=None,
) -> dict:
    """Port of ``runDiscovery`` (see the module docstring)."""
    provided = settings or {}
    settings = {**DEFAULTS, **provided}
    if "minListingDelayMs" not in provided and "minDelayMs" in provided:
        settings["minListingDelayMs"] = provided["minDelayMs"]
    if "maxListingDelayMs" not in provided and "maxDelayMs" in provided:
        settings["maxListingDelayMs"] = provided["maxDelayMs"]
    if query_inventory is None:
        query_inventory = build_queries()
    if listing_reader is None:
        listing_reader = read_listing
    if signal_rules is None:
        signal_rules = {}
    if should_stop is None:
        should_stop = _always_false
    adaptive_enabled = bool(settings.get("adaptiveDepthEnabled"))

    run_id = f"run_{int(time.time() * 1000)}"
    stale_after_ms = _num_or(settings.get("staleLockMinutes"), 360) * 60_000
    if not state_store.acquire_run_lock(run_id, stale_after_ms=stale_after_ms):
        raise RuntimeError("A run is already active.")
    state = state_store.read()
    if state.get("activeRunId"):
        abandoned = state["runs"].get(state["activeRunId"])
        if abandoned is not None:
            abandoned["status"] = "interrupted_recovered"
            abandoned["endedAt"] = abandoned.get("endedAt") or _iso_now()
            abandoned["errors"] = [
                *(abandoned.get("errors") or []),
                f"Recovered by {run_id} after an unclean shutdown.",
            ]
        state["activeRunId"] = None
    hydrated_before_run_today = _completed_hydrations_today(
        state, timezone_name=settings.get("timezone"), excluding_run_id=run_id
    )
    run = {
        "id": run_id,
        "trigger": trigger,
        "status": "running",
        "startedAt": _iso_now(),
        "endedAt": "",
        "lastCheckpointAt": _iso_now(),
        "queriesAttempted": 0,
        "queriesCompleted": 0,
        "resultsFound": 0,
        "uniqueCandidates": 0,
        "hydrated": 0,
        "hydratedByField": {"engineering": 0, "design": 0},
        "dailyHydratedAtStart": hydrated_before_run_today["total"],
        "dailyHydratedByFieldAtStart": hydrated_before_run_today["byField"],
        "newJobs": 0,
        "errors": [],
        "stopReason": "",
    }

    def hydrated_for_field_today(field: object) -> float:
        key = _discovery_field(field)
        return hydrated_before_run_today["byField"][key] + run["hydratedByField"][key]

    def checkpoint() -> None:
        run["lastCheckpointAt"] = _iso_now()
        state["activeRunId"] = run["id"]
        state["runs"][run["id"]] = run
        state_store.write(state)
        state_store.heartbeat_run_lock(run["id"])

    state["activeRunId"] = run["id"]
    state["runs"][run["id"]] = run
    checkpoint()
    thrown: Exception | None = None
    try:
        if not query_inventory:
            raise RuntimeError("No enabled queries are configured.")
        ordered_queries = _query_sequence(query_inventory, state, queries)[
            : int(_num_or(settings.get("maxQueriesPerRun"), 180))
        ]
        seen_in_run: dict = {}
        for query in ordered_queries:
            if should_stop() and run["queriesAttempted"] == 0:
                run["stopReason"] = "shutdown_requested"
                break
            field = _discovery_field(query.get("field"))
            existing_progress = (
                {} if queries else (state.get("queryProgress", {}).get(query["id"]) or {})
            )
            if hydrated_for_field_today(field) >= _field_quota(settings, field):
                continue
            run["queriesAttempted"] += 1
            walk_mode = "full"
            shallow_stop_options = None
            known_urls: set = set()
            coverage_snapshot = None
            if adaptive_enabled and not queries:
                get_coverage = getattr(state_store, "get_query_coverage", None)
                coverage_snapshot = get_coverage(query["id"]) if get_coverage else None
                history = getattr(state_store, "search_history_urls", None)
                if history:
                    known_urls = set(history(query["id"]))
                walk_mode = _decide_walk_mode(
                    coverage_snapshot,
                    now_ms=time.time() * 1000,
                    interval_days=settings.get("fullCheckIntervalDays"),
                )
                if walk_mode == "shallow":
                    shallow_stop_options = {
                        "enabled": True,
                        "quietThreshold": settings.get("shallowQuietThreshold"),
                        "quietPages": settings.get("shallowQuietPages"),
                        "frontierPage": coverage_snapshot.get("frontierPage"),
                    }
            query_started_ms = time.time() * 1000
            hydrated_at_query_start = run["hydrated"]
            new_jobs_at_query_start = run["newJobs"]
            if existing_progress.get("status") in (
                "hydrating",
                "pending_retry",
                "pending_quota",
            ) and existing_progress.get("searchResults"):
                results = with_pagination_stop(
                    existing_progress["searchResults"],
                    existing_progress.get("paginationStop") or "resumed",
                )
            else:
                state["queryProgress"][query["id"]] = {
                    **existing_progress,
                    "status": "searching",
                    "startedAt": existing_progress.get("startedAt") or _iso_now(),
                    "runId": run["id"],
                }
                checkpoint()
                search_error: Exception | None = None
                attempts = int(
                    _num_or(settings.get("searchRetryAttempts"), DEFAULTS["searchRetryAttempts"])
                )
                for attempt in range(1, attempts + 1):
                    try:
                        progress = state["queryProgress"][query["id"]]

                        def on_page(page_checkpoint: dict, _query_id: str = query["id"]) -> None:
                            record = getattr(state_store, "record_search_results", None)
                            if record:
                                record(_query_id, page_checkpoint.get("partialResults") or [])
                            state["queryProgress"][_query_id] = {
                                **state["queryProgress"][_query_id],
                                **page_checkpoint,
                                "status": "searching",
                                "runId": run["id"],
                                "checkpointedAt": _iso_now(),
                            }
                            checkpoint()

                        results = search_provider.search(
                            query,
                            {
                                "resume": {
                                    "partialResults": progress.get("partialResults") or [],
                                    "nextPage": progress.get("nextPage") or 0,
                                    "sparseStreak": progress.get("sparseStreak")
                                    or progress.get("thinPages")
                                    or 0,
                                    "quietStreak": progress.get("quietStreak") or 0,
                                },
                                "maxPages": settings.get("maxPagesPerQuery"),
                                "maxMinutes": settings.get("maxSearchMinutesPerQuery"),
                                "minPageDelayMs": settings.get("minPageDelayMs"),
                                "maxPageDelayMs": settings.get("maxPageDelayMs"),
                                "searchPageBurstSize": settings.get("searchPageBurstSize"),
                                "minSearchPageCooldownMs": settings.get("minSearchPageCooldownMs"),
                                "maxSearchPageCooldownMs": settings.get("maxSearchPageCooldownMs"),
                                "sparsePageResults": settings.get("sparsePageResults"),
                                "sparsePages": settings.get("sparsePages"),
                                "knownUrls": known_urls
                                if (adaptive_enabled and not queries)
                                else None,
                                "shallowStop": shallow_stop_options,
                                "onPage": on_page,
                            },
                        )
                        search_error = None
                        break
                    except Exception as error:  # noqa: BLE001 - mirrors the reference catch-all
                        search_error = error
                        state["queryProgress"][query["id"]] = {
                            **state["queryProgress"][query["id"]],
                            "status": "search_retry",
                            "runId": run["id"],
                            "attempt": attempt,
                            "error": str(error),
                            "checkpointedAt": _iso_now(),
                        }
                        checkpoint()
                        # A challenge is an explicit instruction to stop requesting
                        # Google. Keep the checkpoint and defer; do not retry now.
                        if getattr(error, "name", None) == "SearchBlockedError":
                            break
                        if attempt < attempts:
                            _wait_with_heartbeat(
                                _n(settings.get("retryBaseDelayMs")) * 2 ** (attempt - 1),
                                state_store=state_store,
                                run_id=run["id"],
                                should_stop=lambda: False,
                            )
                if search_error is not None:
                    run["errors"].append(f"{query['id']}: {search_error}")
                    state["queryProgress"][query["id"]] = {
                        **state["queryProgress"][query["id"]],
                        "status": "pending_retry",
                        "stage": "search",
                        "runId": run["id"],
                        "error": str(search_error),
                        "interruptedAt": _iso_now(),
                    }
                    run["stopReason"] = "query_pending_retry"
                    checkpoint()
                    break
            record = getattr(state_store, "record_search_results", None)
            if record:
                record(query["id"], results)
            run["resultsFound"] += len(results)
            state["queryProgress"][query["id"]] = {
                **state["queryProgress"][query["id"]],
                "status": "hydrating",
                "stage": "listing",
                "runId": run["id"],
                "searchResults": [*results],
                "paginationStop": getattr(results, "pagination_stop", None) or "exhausted",
                "searchCompletedAt": _iso_now(),
            }
            checkpoint()
            query_candidates: list = []
            company_board_candidates: list = []
            query_seen_job_ids: set = set()
            for result in [item for item in results if (item.get("link") or "").startswith("http")]:
                candidate = to_candidate(result, query)
                if not is_job_posting_candidate(candidate):
                    _record_company_board(state, candidate, _iso_now())
                    company_board_candidates.append(
                        {"url": candidate["canonicalUrl"], "reason": "company_careers_landing_page"}
                    )
                    continue
                query_seen_job_ids.add(candidate["jobId"])
                prior = seen_in_run.get(candidate["jobId"])
                if prior:
                    prior["sourceQueries"] = _merge_sources(
                        prior["sourceQueries"], candidate["sourceQueries"]
                    )
                    stored = state["jobs"].get(prior["jobId"])
                    if stored is not None:
                        stored["sourceQueries"] = _merge_sources(
                            stored.get("sourceQueries"), candidate["sourceQueries"]
                        )
                    continue
                seen_in_run[candidate["jobId"]] = candidate
                query_candidates.append(candidate)
            recheck_after_ms = _n(settings.get("recheckAfterDays")) * 24 * 60 * 60 * 1000
            lookup = getattr(state_store, "lookup_jobs", None)
            indexed_known_jobs = lookup(query_candidates) if lookup else {}
            dedupe_result = dedupe_candidates(
                query_candidates,
                {**state["jobs"], **indexed_known_jobs},
                recheck_after_ms=recheck_after_ms,
            )
            unique_candidates = dedupe_result["uniqueCandidates"]
            hydration_queue = (
                unique_candidates if force_hydration else dedupe_result["hydrationQueue"]
            )
            run["uniqueCandidates"] += len(unique_candidates)
            now = _iso_now()
            for candidate in unique_candidates:
                previous = state["jobs"].get(candidate["jobId"])
                if previous:
                    previous["lastSeenAt"] = now
                    previous["sourceQueries"] = _merge_sources(
                        previous.get("sourceQueries"), candidate["sourceQueries"]
                    )
                    previous["misses"] = 0
                    if previous.get("status") in ("possibly_closed", "closed"):
                        previous["status"] = "review" if previous.get("reviewReason") else "active"
            failed_candidates: list = []
            deferred_for_quota = False
            for candidate in hydration_queue:
                if hydrated_for_field_today(field) >= _field_quota(settings, field):
                    deferred_for_quota = True
                    state["queryProgress"][query["id"]] = {
                        **state["queryProgress"][query["id"]],
                        "status": "pending_quota",
                        "stage": "listing",
                        "field": field,
                        "searchResults": [*results],
                        "paginationStop": getattr(results, "pagination_stop", None) or "exhausted",
                        "deferredAt": _iso_now(),
                        "deferredReason": f"{field}_daily_listing_quota",
                    }
                    checkpoint()
                    break
                listing = None
                listing_error: Exception | None = None
                listing_attempts = int(
                    _num_or(settings.get("listingRetryAttempts"), DEFAULTS["listingRetryAttempts"])
                )
                for attempt in range(1, listing_attempts + 1):
                    try:
                        listing = listing_reader(
                            candidate,
                            {
                                "minDwellMs": settings.get("minListingDwellMs"),
                                "maxDwellMs": settings.get("maxListingDwellMs"),
                            },
                        )
                        listing_error = None
                        break
                    except Exception as error:  # noqa: BLE001
                        listing_error = error
                        if attempt < listing_attempts:
                            _wait_with_heartbeat(
                                _n(settings.get("retryBaseDelayMs")) * 2 ** (attempt - 1),
                                state_store=state_store,
                                run_id=run["id"],
                                should_stop=lambda: False,
                            )
                if listing_error is not None:
                    failed_candidates.append(
                        {
                            "jobId": candidate["jobId"],
                            "url": candidate["canonicalUrl"],
                            "error": str(listing_error),
                        }
                    )
                    run["errors"].append(f"{candidate['canonicalUrl']}: {listing_error}")
                elif listing.get("isJobPosting") is False:
                    _record_company_board(
                        state,
                        candidate,
                        _iso_now(),
                        listing.get("exclusionReason") or "careers_landing_page",
                    )
                    company_board_candidates.append(
                        {
                            "url": candidate["canonicalUrl"],
                            "reason": listing.get("exclusionReason") or "careers_landing_page",
                        }
                    )
                else:
                    signals = verify_signals(
                        {
                            "title": listing.get("title"),
                            "description": listing.get("description"),
                            "location": listing.get("location"),
                            "role": candidate.get("role"),
                        },
                        signal_rules,
                    )
                    previous = state["jobs"].get(candidate["jobId"])
                    job = {
                        **candidate,
                        **signals,
                        **listing,
                        "company": listing.get("company")
                        or (previous or {}).get("company")
                        or candidate.get("displayLink"),
                        "jobId": candidate["jobId"],
                        "role": candidate.get("role"),
                        "platform": candidate.get("platform"),
                        "sourceQueries": _merge_sources(
                            (previous or {}).get("sourceQueries"), candidate.get("sourceQueries")
                        ),
                        "firstSeenAt": (previous or {}).get("firstSeenAt") or now,
                        "lastSeenAt": now,
                        "verificationCheckedAt": _iso_now(),
                        "misses": 0,
                        "status": "test"
                        if test_mode
                        else (
                            "closed"
                            if listing.get("closed")
                            else ("review" if signals["reviewReason"] else "active")
                        ),
                    }
                    job["companyId"] = _refresh_company(state["companies"], job)
                    state["jobs"][job["jobId"]] = job
                    if previous is None:
                        run["newJobs"] += 1
                    run["hydrated"] += 1
                    run["hydratedByField"][field] += 1
                state["queryProgress"][query["id"]] = {
                    **state["queryProgress"][query["id"]],
                    "failedCandidates": failed_candidates,
                    "companyBoardCandidates": company_board_candidates,
                    "lastCandidateId": candidate["jobId"],
                    "hydratedSoFar": run["hydrated"],
                    "checkpointedAt": _iso_now(),
                }
                checkpoint()
                delay_ms = _random_between(
                    _n(_coalesce(settings.get("minListingDelayMs"), settings.get("minDelayMs"), 0)),
                    _n(_coalesce(settings.get("maxListingDelayMs"), settings.get("maxDelayMs"), 0)),
                )
                _wait_with_heartbeat(
                    delay_ms, state_store=state_store, run_id=run["id"], should_stop=lambda: False
                )
            if deferred_for_quota:
                continue
            if failed_candidates:
                state["queryProgress"][query["id"]] = {
                    **state["queryProgress"][query["id"]],
                    "status": "pending_retry",
                    "stage": "listing",
                    "failedCandidates": failed_candidates,
                    "runId": run["id"],
                    "interruptedAt": _iso_now(),
                }
                run["stopReason"] = "query_pending_retry"
                checkpoint()
                break
            # Miss/closure accounting only trusts full walks (docs/SEARCH-DEPTH-FIX.md §2.3):
            # shallow walks never see below their stop point and must not decay jobs.
            if not adaptive_enabled or walk_mode != "shallow":
                _record_query_misses(
                    state, query, query_seen_job_ids, settings.get("closeAfterMisses")
                )
            state["queryProgress"][query["id"]] = {
                "status": "completed",
                "completionReason": getattr(results, "pagination_stop", None) or "exhausted",
                "completedAt": _iso_now(),
                "runId": run["id"],
                "resultsFound": len(results),
                "uniqueCandidates": len(unique_candidates),
                "companyBoardCandidates": company_board_candidates,
            }
            if adaptive_enabled and not queries:
                _update_crawl_memory(
                    state_store=state_store,
                    query_id=query["id"],
                    results=results,
                    walk_mode=walk_mode,
                    known_urls=known_urls,
                    previous_coverage=coverage_snapshot,
                    last_page_hint=existing_progress.get("lastPage"),
                    new_jobs=run["newJobs"] - new_jobs_at_query_start,
                    hydrated=run["hydrated"] - hydrated_at_query_start,
                    minutes=round((time.time() * 1000 - query_started_ms) / 60_000, 2),
                )
            run["queriesCompleted"] += 1
            if not queries:
                _advance_query_cursor(state, query_inventory, query)
            checkpoint()
            if all(
                hydrated_for_field_today(candidate_field) >= _field_quota(settings, candidate_field)
                for candidate_field in ("engineering", "design")
            ):
                run["stopReason"] = "daily_listing_target"
                break
            if should_stop():
                run["stopReason"] = "shutdown_requested"
                break
            if query is ordered_queries[-1]:
                break
            burst = _n(settings.get("queryBurstSize"))
            if burst and run["queriesCompleted"] % burst == 0:
                delay_range = (_n(settings.get("cooldownMinMs")), _n(settings.get("cooldownMaxMs")))
            else:
                delay_range = (
                    _n(_coalesce(settings.get("minQueryDelayMs"), settings.get("minDelayMs"), 0)),
                    _n(_coalesce(settings.get("maxQueryDelayMs"), settings.get("maxDelayMs"), 0)),
                )
            _wait_with_heartbeat(
                _random_between(*delay_range),
                state_store=state_store,
                run_id=run["id"],
                should_stop=should_stop,
            )
        if not run["stopReason"] and hydrated_before_run_today["total"] + run["hydrated"] >= _n(
            settings.get("maxListingsPerRun")
        ):
            run["stopReason"] = "daily_listing_target"
        _recalculate_companies(state)
        run["status"] = "completed_with_errors" if run["errors"] else "completed"
    except Exception as error:  # noqa: BLE001 - mirrors the reference catch-all
        thrown = error
        run["status"] = "failed"
        run["errors"].append(str(error))
    finally:
        run["endedAt"] = _iso_now()
        state["activeRunId"] = None
        state["runs"][run["id"]] = run
        state_store.write(state)
        close = getattr(search_provider, "close", None)
        if close:
            try:
                close()
            except Exception as error:  # noqa: BLE001
                _log_error(logger, f"Browser cleanup failed: {error}")
        if sheets:
            try:
                sync_state_to_sheets(sheets, state, state_store)
                state_store.write(state)
            except Exception as error:  # noqa: BLE001
                run["errors"].append(f"Google Sheets sync failed: {error}")
                if run["status"] != "failed":
                    run["status"] = "completed_with_sync_error"
                state["runs"][run["id"]] = run
                state_store.write(state)
                _log_error(logger, f"Google Sheets sync failed: {error}")
        state_store.release_run_lock()
    if thrown:
        raise thrown
    return run
