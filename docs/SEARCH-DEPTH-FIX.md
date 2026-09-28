# Search-Depth Fix — Spec (deficiency #1: no cross-run crawl memory)

**Status:** Phase 1 of 5 — foundation landed 2026-09-29. Decisions: all recommendations accepted
(full parking for low tier; auto-park after 4 zero-yield full checks; activation via `runtime.json` for v1).
**Scope:** the ported job-discovery system (`outbound/job_discovery/`). The retired Node reference had the
same deficiency; this fix is port-only.

## 1. Problem (confirmed 2026-09-28, see workspace audit `jd-audit/`)

Every crawl of a query restarts at page 0 and walks to its natural end regardless of when it last ran:
- Completion replaces the progress record with a summary — depth (`nextPage`/`thinPages`) is discarded;
  the schema stores no crawl memory.
- The resume path applies only to mid-flight queries (hydrating / pending_retry / pending_quota + stored results).
- The thin-pages stop counts all valid results (known or not), so re-crawls never short-circuit.
- Only hydration is deduped across runs (verified unchanged jobs are not re-opened).
- Measured: `Ashby:Software Engineer:unfiltered` = 296 results ≈ 20–30 pages ≈ **2h38m** per pass, repeated.
- Real repro (local): day-2 re-run walked `start=0,10,20,30` again; listings 10 → 0 saved, pages 0 saved.

## 2. Design — two walk speeds + coverage memory

**2.1 Coverage memory (per query).**
- `frontierPage` — deepest page reached by a *completed* crawl (`exhausted` or tail rule); partial stops
  (challenge / limits / shutdown) never update it.
- `frontierReason`, `coveredAt` (when the frontier was set), `checkedAt` (last visit), `lastStopReason`.

**2.2 Shallow walk (default visit).**
- Walk from page 0; a page is **"quiet"** when it yields fewer than `shallowQuietThreshold` (10) results
  that are **new for this query** (never previously recorded in local `search_results`).
- Stop after `shallowQuietPages` (3) consecutive quiet pages — but only while inside covered territory
  (page index ≤ frontier). Stop reason: **`known_frontier`**.
- Walks onward while novelty appears (adaptive on churn days). Walks below the frontier are not allowed to
  use this stop (unexplored territory is walked per full rules).

**2.3 Full walk.**
- Thorough walk (classic rules + tail rule below). Updates the frontier when it completes.
- Required when: no coverage, partial coverage (last stop not completion-grade), or the 7-day clock is due.
- **Miss/closure guard:** `record_query_misses` (auto-close logic) runs ONLY on full walks — shallow walks
  skip miss accounting entirely (otherwise deep jobs would falsely decay to "closed").

**2.4 Tail rule (applies in all walks).**
- Stop when `sparsePages` (3) consecutive pages each yield fewer than `sparsePageResults` (2) valid results.
  Replaces the old "<3 results × 2 pages" rule. Stop reason: `low_yield`.

**2.5 Interrupted crawls.** Non-completion stops leave coverage partial → the next visit is a full walk.\n\n**2.6 Stop-reason precedence (per page).** `result_limit` → `exhausted` → sparse tail (`low_yield`) → shallow\nknown-stop (`known_frontier`). The shallow stop additionally requires being within the covered frontier;\nbeyond it, full rules govern.

## 3. Ranking & parking

- **Rolling stats per query** (last ~6 full checks): pages walked, results, new results, new jobs, minutes.
- **Tiers:** HIGH (finds new jobs regularly) / AVERAGE (occasional; defaults for new queries) / LOW (repeated zero).
- **Parking (decision: auto with veto):** a query auto-parks after `parkAfterZeroFullChecks` (4) consecutive
  zero-yield full checks. Parked = fully skipped (no shallow, no full) until activated.
  Each run's report lists parked queries + recent changes; user can veto/reactivate.
- **Activation (v1):** `activatedQueries` list in `config/job_discovery/runtime.json`; dashboard/sheet toggle later.
- Manual overrides: force tier / park / activate (config; power-user level).

## 4. Full-check scheduling — priority wave

- Every non-parked query has a `fullCheckIntervalDays` (7) clock from its last full check.
- Each day the DUE list is worked in priority order: **HIGH first, then AVERAGE**; parked skipped.
  A `deepBudgetMinutesPerRun` (180) cap limits deep minutes per run; overflow carries to the next day.
- Highs are few → checked ~weekly; averages take remaining capacity (typically every ~2–4 weeks); the run
  report surfaces backlog so starvation is visible and tunable.

## 5. Dials (defaults; all editable in runtime.json)

| Key | Meaning | Default |
|---|---|---|
| `adaptiveDepthEnabled` | master switch (off = old behavior) | true |
| `shallowQuietThreshold` | new-results/page below which a page is quiet | 10 |
| `shallowQuietPages` | consecutive quiet pages before shallow stop | 3 |
| `sparsePageResults` | tail rule: results/page considered sparse | 2 |
| `sparsePages` | tail rule: consecutive sparse pages before stop | 3 |
| `fullCheckIntervalDays` | per-query full-check clock | 7 |
| `deepBudgetMinutesPerRun` | max deep minutes per run | 180 |
| `parkAfterZeroFullChecks` | zero-yield full checks before auto-park | 4 |
| `activatedQueries` | reactivated (un-parked) query ids | [] |

## 6. State additions

- `query_coverage(id, frontier_page, frontier_reason, covered_at, checked_at, last_stop_reason)`
- `query_stats(id, data)` — JSON blob (rolling crawl records).
- `StateStore` APIs: `query_coverage_map`, `get/set_query_coverage`, `query_stats_map`, `get/set_query_stats`,
  `search_history_urls(query_id)`. Both tables clear on `reset()`. Existing DBs upgrade automatically
  (CREATE IF NOT EXISTS on open).

## 7. Phases

1. **Foundation** — this spec + state tables/APIs + settings keys. *(done)*
2. **Stop rules (provider)** — shallow stop (knownUrls/threshold/buffer), tail rule, `known_frontier`,
   `last_page`; unit tests. *(done)*
3. **Runner integration** — shallow/full decision, coverage/stats write-back, miss-guard, master switch;
   two-day regression test (day 2 = 1–3 pages).
4. **Policy** — tier classification, auto-park + overrides, 7-day clock, priority wave, deep budget, backlog reporting.
5. **Proof & polish** — `status` telemetry, seeded-DB dry simulation (projected savings), fake-browser E2E;
   live validation rides the cutover gate.

## 8. Safety / migration

- Master switch `adaptiveDepthEnabled` restores pre-fix behavior instantly.
- Seeded/old DBs have no coverage → first visit per query = full walk (as today), then memory engages.
- Closures only on full walks; interrupted crawls force a full walk next time.
- Nothing goes live before the cutover gate; each phase merges independently (gates + CI + approval).

## 9. Testing strategy

- Unit: provider stop semantics (quiet counting, frontier bound, sparse tail, `known_frontier`/`low_yield` reasons).
- Regression: the audit two-day scenario — day 2 must fetch ≤3 pages and hydrate 0 listings.
- Runner: mode decision, miss-guard, stats/coverage write-back.
- Policy: tier transitions, auto-park thresholds, activation, priority ordering, budget cutoff/overflow.
- Evidence: dry simulation over the seeded production state (per-query projected plan + savings).
