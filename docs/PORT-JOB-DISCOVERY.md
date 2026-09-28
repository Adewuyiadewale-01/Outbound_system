# Job Discovery — Python Port · Phase 1: Portrait & Plan

**Date:** 2026-09-28 · **Status:** Phase 1 (spec) — local only, not pushed
**Source:** `job_discovery/` (JS/ESM import of `~/Documents/Automation Journey/daily-job-discovery`, 36 files / ~1,722 lines src, 22 tests)
**Method:** full read of every module + configs + tests; every claim below is from the code, not the README.

---

## 1. What the system does (end-to-end)

A daily job-discovery pipeline that turns a 12-platform × 8-role × 3-strategy search matrix (288 queries, default ceiling 180/run) into a curated Google Sheet:

```
Query inventory (alternating engineering/design)
  → Google search (Playwright, paced) per query, paginated with checkpoints
    → dedupe BEFORE opening listings (ATS id → canonical URL → fallback fingerprint)
      → hydrate candidates (open ATS listing, read JSON-LD/text, dwell)
        → verify signals (junior / remote / python) → score, route to review
          → persist to local SQLite (source of truth)
            → sync final projections to Google Sheets (Jobs, Companies, Review Queue, Runs)
```

Key design facts: **zero npm runtime dependencies** (built-in `fetch` + `node:sqlite`; Playwright via an `npx` CLI wrapper); local SQLite is durable truth, Sheets gets only finalized projections; everything is resumable via query checkpoints; pacing is first-class (anti-abuse, not evasion).

---

## 2. Module-by-module behavioral spec

### 2.1 `config/defaults.mjs`
- **Platforms (12):** Ashby, Greenhouse, Lever (`jobs.lever.co OR jobs.eu.lever.co`), Workable, SmartRecruiters, Teamtailor, Recruitee, Pinpoint, Breezy HR, Comeet, Personio, Workday — each `{name, siteTarget, enabled}`.
- **Roles (8):** Engineering: Python Developer, Backend Developer, Automation Developer, Software Engineer, Full Stack Developer. Design: Product Designer, UI/UX Designer, Web Designer. Each has `junior[]`, `unfiltered[]` phrase lists + `field`.
- **Settings defaults:** `automationEnabled=false`, `dailyRunTime 08:00`, `timezone Africa/Lagos`; pacing (ms): listing delay 8–15k, dwell 4–7k, page delay 15–30k, search-page burst 3 → cooldown 180–300k; query delay 90–180k, query burst 3 → cooldown 600–900k; `maxQueriesPerRun=180`, `maxListingsPerRun=180`; retries: search 2 / listing 3, base 2k exp; `maxPagesPerQuery=0` (off), `maxSearchMinutesPerQuery=75`; `recheckAfterDays=7`, `staleLockMinutes=360`, `closeAfterMisses=3`.

### 2.2 `src/url.mjs` (pure)
- `canonicalizeUrl`: strip hash; lowercase host; remove tracking params (`gclid, fbclid, ref, source, utm_*` — case-insensitive); collapse trailing slashes (`/` kept).
- `stableHash`: sha256 hex, **first 24 chars**.
- `normalizedText`: lowercase, non-alnum → spaces, collapsed.
- `findAtsJobId`: query params `jobId|job_id|gh_jid|lever-origin|opening|requisitionId`; else last path segment if ≥5 chars; result `{hostname}:{segment}`.

### 2.3 `src/dedupe.mjs` (pure)
- `toCandidate(result, query)`: builds `jobId = stableHash("ats:"+atsId | "url:"+canonicalUrl)`, `resultHash = stableHash(title|snippet|canonicalUrl)`, `fallbackFingerprint = stableHash(platform|company|title|location normalized)`.
- `dedupeCandidates(candidates, knownJobs, {recheckAfterMs})`: dedupe in-run by jobId **or** fallbackFingerprint (sources merged); hydration queue if: unknown, `resultHash` changed, any signal `unsure`, or recheck due.

### 2.4 `src/query-builder.mjs` (pure)
- Per platform×role → 3 queries: `junior` (`intitle:` chain of junior phrases, `remote -"no remote" -intitle:senior -intitle:staff -intitle:principal`), `unfiltered`, `junior-signal` (unfiltered phrases non-intitle + `junior OR "entry level" OR "new grad" OR associate OR "early career"`).
- `alternateFields`: interleave engineering/design queries. `allowedHosts` parsed from `site:` targets.

### 2.5 `src/signals.mjs` (pure)
- Rule terms → regexes (`\b`-anchored, spaces → `[ -]`; special case: term `I|1` → `(?:engineer|developer)\s+(?:i|1)`). Legacy `nonRemote` split into hybrid vs onsite.
- Verdicts — **junior**: `conflicting` (title has both junior+senior) | `verified` | `senior_verified` (title-only senior) | `unsure` (partial/untitled title) | `not_found`. **remote**: `conflicting` (remote + hybrid/onsite co-present) | `hybrid_verified` | `onsite_verified` | `verified` | `unsure` | `not_found`. **python**: `verified` | `unsure` | `not_found`.
- `score = 25×verifiedCount + 20 (junior verified) + 15 (remote verified)`; `reviewReason` = "Conflicting signals" / "Signal could not be confirmed" / "". Evidence: ±90/+140-char window around matched term, joined ` | `.

### 2.6 `src/job-posting-url.mjs` (pure)
- Ashby `/{company}` (segments < 2, host ashbyhq.com) = careers **board** (not a posting). Narrow by design.

### 2.7 `src/listing-reader.mjs` (HTTP fallback reader)
- `fetch(canonicalUrl)` 15s timeout, `Accept: text/html…`; extract JSON-LD `JobPosting` (first of `@type` JobPosting incl. arrays/`@graph`); title from schema → candidate → `<title>`; description = stripped schema description else stripped HTML (max 35,000 chars); company = `hiringOrganization.name` else displayLink first label; location = `jobLocation.address.addressLocality` else `applicantLocationRequirements.name`; canonicalize `response.url`.

### 2.8 `src/search-provider.mjs` (provider factory)
- Providers: `FixtureSearchProvider` (default when unset; reads `fixtures/search-results.json`), `GoogleCustomSearchProvider` (CSE API; 10/page), `HttpSearchProvider` (POST `{query, queryId}` → `{results:[{title,link,snippet,displayLink?}]}`), `PlaywrightGoogleSearchProvider` (via browser). `createListingReader`: playwright → browser-based reader; else default HTTP reader.

### 2.9 `src/playwright-provider.mjs` (browser + Google search + listing reader)
- **Browser**: wraps `playwright-cli` (shell): `open/goto` (`--headed`, first open `--persistent --profile <dir>`), `eval` (parse `### Result` JSON), `run-code`, `close`. Session `daily-job-discovery`. Timeout 45s.
- **Google search** (`search`): resumable (`partialResults`, `nextPage`, `thinPages`); URL `google.com/search?q=…&start=page*10`; rejects Google cookie banner; scrolls page before extract; extracts anchors with `h3` (+ unwraps Google `/url?q=`); **challenge regex** `/\/sorry\/|unusual traffic|verify (that )?you(’re| are) human|not a robot|captcha/i` → `SearchBlockedError` (checkpoint, stop, no immediate retry); filters to allowedHosts; dedupe; **two-consecutive-thin-pages** (<3 valid each) → `low_yield`; `hasNext` check → `exhausted`; `maxResults` → `result_limit`; `onPage` checkpoint per page; page delay + burst cooldown sleeps. Safety: page ceiling (`maxPagesPerQuery`) & session minutes → `SearchSafetyLimitError`.
- **Listing reader** (browser): dwell 4–7s + scroll; extract JSON-LD / `h1` / `document.title`; `closed` & `blocked` regexes; `isJobPosting` = JSON-LD present OR no `open positions (N)` pattern; tenant inference for workday/recruitee/breezy/pinpoint.

### 2.10 `src/sheets.mjs` (two transports + row builders)
- **Tab schemas:** `Control, ATS Platforms, Roles & Vocabulary, Queries, Jobs (19 cols), Companies (10 cols), Runs (12 cols), Review Queue, Rules` + default Control rows (27 keys) & Rules (v3).
- **AppsScriptSheetsClient** (default transport): POST `{action, token, ...}` → `bootstrap|getControl|getConfiguration|append|upsert|replace|retain|syncProjection|clearProjection`; graceful fallbacks when an action is "unsupported" (replace→upsert, retain/syncProjection→no-op flag).
- **GoogleSheetsClient** (service-account): JWT RS256 → OAuth token → Sheets v4; `setup` (create missing tabs; local-first variant removes local-only tabs), `readControl`, `readConfiguration` (4 tab reads), `upsert` (by id via batchUpdate + append), `replace` (clear + append).
- **Row builders**: `jobRow` (19 cols — exact order frozen), `companyRow` (10 cols, **Notes deliberately never written**), `runRow` (notes = `stop=…` + errors).

### 2.11 `src/state-store.mjs` (SQLite, built-in `node:sqlite`)
- Schema: `metadata(k,v)` + `jobs(id, canonical_url, fallback_fingerprint, result_hash, status, last_seen_at, data)` (+2 idx) + `companies(id, data)` + `runs(id, started_at, status, sheet_synced_at, data)` (+sync idx) + `query_progress(id, status, data)` + `live_test_usage(query_id, uses)` + `search_results(query_id, url_hash, …)` (+2 idx) + `projection_sync(target, id, projection_hash, synced_at)`. WAL, `synchronous=NORMAL`, `busy_timeout=5000`.
- **Migration**: `.json` state file → sibling `.sqlite` (legacy file kept as backup).
- **Lock**: `<db>.lock` JSON `{runId, pid, hostname, createdAt, heartbeatAt}`; `wx` create; stale = dead **local** pid or age > `staleAfterMs` (360 min default); heartbeat during waits (30s chunks); release on exit.
- **Write path**: metadata keys + table syncs with change-detection cache; single transaction; delete-missing reconciliation; cache refresh.
- **Projection sync**: `projectionChanges(target, records)` returns changed (hash) or old (>7 days) rows; `markProjectionSynced`. `reset()` clears all + re-writes blank state. `stats()` counts. `lookupJobs(candidates)` (by id + fallback).

### 2.12 `src/run.mjs` (orchestrator)
- `runDiscovery`: acquires lock (stale recovery), recovers abandoned `activeRunId` as `interrupted_recovered`; run record (per-field hydration counters, daily-at-start baselines); ordered queries (cursor round-robin via `queryCursorId` + `queryCursor`, wrapping increments `queryCycle`) limited by `maxQueriesPerRun`; resume from `queryProgress.searchResults` without re-searching; search retries (challenge = stop, no immediate retry); per-result candidate → company-board branch (Ashby) vs candidate flow; in-run dedupe + indexed lookup → hydration queue; field quota gate (eng = `max - floor(max/2)`, design = `floor(max/2)`; counts earlier same-day runs, excludes smoke-test) → `pending_quota` checkpoint, continue other field; listing retries; signal verification; job write + company refresh (`companyId = hash(company|domain)`, platform/career URL merge, remote signal); checkpoints after every step; query completion → misses bookkeeping (`closeAfterMisses` → `closed`), cursor advance, burst cooldowns; stop reasons: `daily_listing_target`, `query_pending_retry`, `shutdown_requested`; final `recalculateCompanies` (active counts, remote signal, active/inactive); Sheets sync in `finally` (+ `completed_with_sync_error` handling); browser close; lock release.
- `syncStateToSheets`: filter `test`; incremental projections; Reviews = `status==review` rows; pending runs; `retain` fallback semantics.
- `reverifyStoredJobs`: no network; re-classify careers landings + re-run signals; update `verificationCheckedAt`; recount companies; optional sheets sync.

### 2.13 `src/scheduler.mjs`
- `runIfDue`: `automationEnabled` gate; due when local-time ≥ `dailyRunTime`; skip if `lastScheduledDate==today` or active run; runs + stamps `lastScheduledDate`. **Missed same-day runs execute when back online.**
- `startScheduler`: 60s ticks, overlap guard, error backoff 15 min, logs.

### 2.14 `src/cli.mjs` (+ `src/config.mjs`, `src/runtime-configuration.mjs`)
- Commands: `setup-sheet | reset | run | reverify | smoke-test | live-test | schedule | status`; SIGINT/SIGTERM → graceful (checkpoint, finish query, exit 130); non-schedule commands close the browser then exit.
- `loadEnvironment`: `.env` loader (process env wins); `loadLocalSettings`: `config/runtime.json` + normalization (min≤max clamping; ints ≥1; `HH:MM` + timezone validation). `settingsFromControl`: Control-tab keys (incl. legacy aliases).
- `configurationFromRows`: Sheet-config mode — platforms/roles/queries/rules from 4 tabs when `CONFIGURATION_SOURCE=sheet`; else defaults. Query rows: enabled + platform-enabled filters.
- `live-test` guard: only `playwright-google`; per-query 2-use cap (`liveTestUsage`); maxResults=1.

---

## 3. Contracts

### 3.1 Sheet contract
- Tabs & column orders (§2.10) — **frozen** (existing data must survive the port).
- Default transport: **Apps Script Web App** (`apps-script/Code.gs` deployed by the user; token + URL in env; document lock + batched rewrites; formula priority tabs `High/Medium/Low I/Low II/Needs Review/Company Boards`; menu "Refresh priority views"; company notes preserved).
- Local-first mode (default): runtime controls from `config/runtime.json`; `setup-sheet` removes the local-only config tabs; `CONTROL_SOURCE=sheet` / `CONFIGURATION_SOURCE=sheet` remain optional.

### 3.2 State contract
- SQLite schema, lock protocol, projection-sync tables (§2.11). Legacy JSON import.

### 3.3 Config contract
- `config/runtime.json` keys = Control-tab keys (§2.1 defaults; normalization rules §2.14).

### 3.4 CLI contract
- §2.14 commands + exit behavior; `npm run` scripts mirror them.

### 3.5 Env contract
`SEARCH_PROVIDER` (default fixture; `playwright-google`|`http`|`google-cse`), `PLAYWRIGHT_HEADED` (≠"false"), `PLAYWRIGHT_PROFILE_DIR` (`./data/browser-profile`), `PLAYWRIGHT_CLI_PATH`, `STATE_FILE`, `SHEETS_TRANSPORT` (`apps-script`), `GOOGLE_APPS_SCRIPT_URL`, `APPS_SCRIPT_TOKEN`, `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `CONTROL_SOURCE`/`CONFIGURATION_SOURCE` (`local`), `LOCAL_CONTROL_FILE`, `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_ID`, `SEARCH_API_URL`/`SEARCH_API_TOKEN`.

### 3.6 External interface
- `scripts/playwright-cli.sh` = `npx --yes --package @playwright/cli@0.1.18 playwright-cli "$@"` — the ONLY external runtime dependency.

---

## 4. Behavioral invariants (must be preserved exactly in the port)

1. Identity chain: ATS id → canonical URL → fallback fingerprint; 24-char hashes everywhere.
2. Dedupe **before** hydration; re-hydrate only when hash changed / unsure / recheck-due.
3. Two-consecutive-thin-pages (<3 valid) pagination stop + `hasNext` exhaustion + resumable checkpoints; **zero page ceiling by default**.
4. Challenge page → checkpoint + defer the query, **never immediate retry**.
5. Field quotas count earlier same-day runs; finish current query before stopping at targets; `pending_quota` defers remainder.
6. Cursor advances only on completed queries; miss counters only from completed source queries.
7. Company-board handling at two levels (candidate URL + listing check); Notes column untouched.
8. Pacing values/random ranges and burst logic (§2.1) — sequential by design.
9. Lock semantics incl. dead-pid recovery + heartbeats; unclean-run recovery to `interrupted_recovered`.
10. Review routing: any `conflicting`/`unsure` → Review Queue; scores as specified.
11. Incremental projection sync (never rewrite whole sheets; reconcile 7d; `test` rows excluded).
12. Local-first configuration is authoritative; scheduler performs no Google requests while idle.

---

## 5. Test inventory (22 tests — the port's parity floor)

`dedupe` (2) · `job-posting-url` (1) · `playwright-provider` (6: parsing, headed/profile open, pagination/thin-pages/resume, tracking normalization, challenge) · `query-builder` (1) · `run` (8: dedupe-before-read, target+cursor, same-day totals, field quotas, Ashby boards, resume-without-re-search, no-retry-on-challenge, close-after-misses) · `runtime-configuration` (1) · `scheduler` (2: timezone, missed-run) · `signals` (4: verify, conflict, hybrid/onsite evidence, mixed→review) · `state-store` (4: JSON→SQLite, dead-pid lock recovery, incremental projections, reset).

---

## 6. Port plan

### 6.1 Destination & module mapping
```
outbound/job_discovery/
├── settings.py     defaults (12 platforms, 8 roles, settings) + normalization + Control mapping
├── urls.py         canonicalize_url, stable_hash, normalized_text, find_ats_job_id, careers-page checks
├── queries.py      build_queries, alternate_fields, configuration_from_rows, hosts_from_target
├── dedupe.py       to_candidate, dedupe_candidates
├── signals.py      verify_signals + pattern builder
├── listing.py      HTTP listing reader (JSON-LD/title/strip)
├── state.py        StateStore (sqlite3 + lock + projections)
├── sheets.py       AppsScriptClient, GspreadClient, tab schemas, row builders
├── browser.py      Playwright browser wrapper (eval/run-code helpers, challenge detection)
├── providers.py    Fixture/Http/CSE/PlaywrightGoogle providers + listing-reader factory
├── runner.py       run_discovery, reverify_stored_jobs, sync_state_to_sheets
├── scheduler.py    start_scheduler, run_if_due
└── cli.py          argparse CLI (8 commands)
```
Thin entry at `scripts/job_discovery.py` (system convention) + optional `mac/` launcher later. The JS copy stays as the reference implementation until parity is proven.

### 6.2 Library decisions (for your approval)

| Concern | Recommendation | Why (vs alternatives) |
|---|---|---|
| Browser automation | **`playwright` (Python, sync API)** with persistent context | Canonical implementation; removes the `npx`/CLI layer + version pinning; same engine, same visible profile semantics; keeps `PLAYWRIGHT_HEADED`/profile-dir contract |
| HTML extraction | **`lxml`** (JSON-LD scan + text extraction) | Robust, fast, battle-tested; mirrors semantics (schema first, title, strip scripts/styles) |
| HTTP client | **`httpx`** | Timeouts, connection pooling, modern API; used for listing fetch + Apps Script calls |
| Sheets (Apps Script transport) | `httpx` POST client, same action protocol | Existing deployment keeps working unchanged |
| Sheets (service-account transport) | **`gspread`** (+ `google-auth`) | System standard; handles auth/batch; direct path only |
| State | **stdlib `sqlite3`** + `os.open(O_EXCL)` lock | Zero deps; schema frozen; matches file-lock + pid-liveness semantics |
| Timezone | stdlib **`zoneinfo`** | Equivalent to `Intl` usage |
| Retries | explicit helpers (no `tenacity`) | Semantics are custom (challenge no-retry, pending states); library would fight us |
| CLI | **argparse** | System consistency |
| Tests | **pytest** + Node golden-check harness (dev-only) | Parity proof for pure modules |

New deps added: `playwright`, `lxml`, `httpx`, `gspread` (existing in ecosystem), `google-auth`. (`tenacity` considered — rejected.)

### 6.3 Parity risks & mitigations
1. **URL canonicalization** (Node `URL` vs Python): encoding/case/default-port edge cases → golden corpus tests (100+ real URLs through both implementations, assert identical).
2. **Regex semantics** (`\b`, escaping, case): port with golden tests per rules table.
3. **Intl date keys / sort**: `zoneinfo` + ISO string sort — trivially equivalent; tested.
4. **DOM extraction**: browser path keeps the *same JS snippets* run via `page.evaluate` (exact parity); HTTP path uses lxml — golden HTML fixture tests.
5. **`node:sqlite` → `sqlite3`**: same pragmas/schema; migration test.

### 6.4 Implementation phases (each ends with: local gates → your review → push + PR + merge)
- **Phase 2 — Pure core:** `urls`, `queries`, `dedupe`, `signals` + ported tests + Node golden corpus. *(No I/O.)*
- **Phase 3 — State:** `state.py` + lock + projections + tests (migration included).
- **Phase 4 — Sheets:** `sheets.py` (both transports), row builders, setup/reset + tests (fixture-backed).
- **Phase 5 — Providers:** `providers.py` + `browser.py` (pagination/challenge logic under test with fake browser) + listing readers.
- **Phase 6 — Runner & CLI:** `runner.py`, `scheduler.py`, `cli.py` + full mock end-to-end (mirrors `run.test.mjs` scenarios).
- **Phase 7 — Live verification:** read-only `status` against the real setup → `smoke-test` → capped `live-test` → cutover decision (only with explicit approval).

### 6.4a Progress log

- **Phase 2 (pure core) — completed locally 2026-09-28.** `outbound/job_discovery/{settings,urls,queries,dedupe,signals}.py` ported; 122 native tests added; cross-language golden harness (Node reference vs Python port) reports **0 mismatches** across the full corpus; repo suite 234 tests green, ruff clean. Merged via PR #29 (squash `6eff19f`).

- **Phase 3 (state store) — completed locally 2026-09-28.** `outbound/job_discovery/state.py` (SQLite schema, run lock, projection sync, legacy migration) + 10 tests; cross-language check vs the reference on `node:sqlite`: **0 mismatches** across 15 scenario outputs incl. byte-level table dumps; repo suite 244 tests green, ruff clean. Merged via PR #30 (squash `a230079`).

- **Phase 4 (Sheets layer) — completed locally 2026-09-28.** `outbound/job_discovery/sheets.py` (Apps Script action protocol + gspread service-account client, tab schemas, default Control/Rules, row builders, client factory); `httpx==0.28.1` added; 17 tests incl. a Node-generated fixture (`tests/fixtures/job_discovery_sheets.json`); two-sided golden check: **0 mismatches** across 6 row-builder cases + 19 Apps Script scenarios; repo suite 261 tests green, ruff clean. Merged via PR #31 (squash `7001d27`).

- **Phase 5 (providers & browser) — completed locally 2026-09-28.** `outbound/job_discovery/{browser,providers,listing}.py` — Playwright browser wrapper on the direct Python API (lazy import), Google pagination provider (resume checkpoints, 2-thin-pages rule, challenge detection, page/time safety ceilings), search-provider factory (fixture/http/CSE/playwright transports), HTTP listing reader; reference page-side extraction snippets kept verbatim; `playwright==1.63.0` added; 25 tests; two-sided golden check: **0 mismatches** across 9 scenarios + pagination-state cases; repo suite 286 tests green, ruff clean. Merged via PR #32 (squash `55dd163`).

- **Phase 6 (runner & CLI) — completed locally 2026-09-28.** `outbound/job_discovery/{runner,scheduler,cli}.py` + `scripts/job_discovery.py` entry — full run orchestrator (locking + stale recovery, resumable checkpoints, field quotas, daily targets, retries, cursor rotation, miss lifecycle, incremental Sheets sync, reverify), daily scheduler, and the 8-command CLI (`setup-sheet | reset | run | reverify | smoke-test | live-test | schedule | status`); `settings.py`/`queries.py` gained env loading, Control mapping, and Sheet-driven configuration. 27 tests; two-sided end-to-end run comparison: **0 mismatches** (run summary + full final state + Sheet calls); repo suite 313 tests green, ruff clean. Merged via PR #33 (squash `b763d25`).

### 6.5 Verification strategy
- Test parity: ≥22 ported tests + golden cross-language checks (Node available locally; harness not in CI).
- Mock end-to-end runs before any live call; `automationEnabled=false` throughout development.
- Live checks are read-only first; any write path exercised only via `smoke-test`/`live-test` with limits, on your explicit go.

---

## 7. Open questions — resolved 2026-09-28

1. **Browser**: Python `playwright` directly. ✅
2. **State location**: repo `state/` conventions (e.g. `state/job_discovery/`). ✅
3. **Transports**: keep both sheet transports and all search providers. ✅
4. **JS copy retention**: keep `job_discovery/` as reference until parity proven. ✅
5. **Env reconciliation**: no key conflicts; port reads the repo `.env` with the same names. ✅
6. **Entry naming**: `scripts/job_discovery.py` (+ `mac/` launcher later). ✅

---

## 8. Phase 7 — Live verification runbook

Each step below executes only with explicit approval, in order. Nothing here touches the production automation; write-verification against the live Sheet is a cutover-level decision (§9).

**Prerequisites**
1. Playwright browsers for the venv (not yet installed):
   `.venv/bin/python -m playwright install chromium` (~150 MB into `~/Library/Caches/ms-playwright`).
2. Env keys for the port — append a block to the repo `.env` (names do not conflict with the existing system keys):
   ```
   SEARCH_PROVIDER=playwright-google
   PLAYWRIGHT_HEADED=true
   PLAYWRIGHT_PROFILE_DIR=/Users/tonyisbuiding/Documents/Automation Journey/daily-job-discovery/data/browser-profile
   STATE_FILE=state/job_discovery/state.sqlite
   SHEETS_TRANSPORT=apps-script
   GOOGLE_APPS_SCRIPT_URL=<copy from daily-job-discovery/.env>
   APPS_SCRIPT_TOKEN=<copy from daily-job-discovery/.env>
   LOCAL_CONTROL_FILE=job_discovery/config/runtime.json
   # CONTROL_SOURCE / CONFIGURATION_SOURCE stay unset (local-first) until decided
   ```
   Note: the profile dir is shared with the reference project; the old scheduler is idle (`Automation Enabled` FALSE) so it never launches a browser — but the two must never run browsers on the same profile at once.
3. Safety notes (learned from the reference implementation):
   - `syncStateToSheets` retains by **local** id sets — never sync to the live Sheet from a fresh/empty state, or Jobs/Companies rows are cleared by design.
   - `smoke-test` writes a `test` job (excluded from Jobs) and, when sheets are configured, syncs its run row to Runs (reference behavior). For the first live smoke run, use sheets-unconfigured mode.
   - The port's default state path is `state/job_discovery/state.sqlite`; keep it distinct from the reference project's `data/state.sqlite` until cutover.

**Steps (each gated on explicit go-ahead)**
1. **Local dry run — done 2026-09-28.** `status` + `smoke-test` from the repo root with sheets neutralized: both exit 0; smoke completed with 1 hydrated; zero network/Sheet calls.
2. **Read-only transport check vs the live Sheet** (when the env block is wired):
   `CONTROL_SOURCE=sheet python scripts/job_discovery.py status`
   — only `getControl` (and optionally `getConfiguration`) reads occur; no writes. Confirms the deployed Apps Script protocol end-to-end. The live sheet may run local-first (tabs absent) — either way a clean `{ok:true}` round-trip is the success criterion.
3. **Smoke-test (local-only)**: sheets env absent; synthetic provider/reader; one `test` record in local state.
4. **Capped live-test**: one query, `maxResults=1`, 2-use cap, sheets absent (local writes only) — the first real Google search + real listing read through the port, exercising the persistent profile.
5. **Evidence pack + cutover decision** (§9): compile receipts; decide the cutover sequence and timing.

## 9. Cutover checklist (draft — execute only after explicit sign-off)

- [ ] Finalize the env block; install browsers; keep `Automation Enabled` FALSE throughout.
- [ ] **Seed the port state by copying the reference SQLite** (schemas are identical; the reference migrated to SQLite already):
      checkpoint `daily-job-discovery/data/state.sqlite`, copy it to `state/job_discovery/state.sqlite`, and verify row counts via the port's `stats()`.
- [ ] Freeze the reference scheduler: `launchctl bootout gui/$UID/com.fulltime-job.daily-job-discovery` (it is currently running — PID observed 2026-09-28) and confirm no browser profile contention.
- [ ] Supervised first real run: `scripts/job_discovery.py run` with sheets configured; verify Jobs/Companies/Review/Runs deltas; then `reverify`.
- [ ] Decide scheduler takeover: port a `mac/` launcher + launchd unit for `scripts/job_discovery.py schedule`; retire the JS agent.
- [ ] Housekeeping: move `search-results.json` into `outbound/job_discovery/fixtures/`; archive `job_discovery/` (reference) per plan §6.1; update `docs/EXTERNAL-DEPENDENCIES.md`; final gates + PR.
