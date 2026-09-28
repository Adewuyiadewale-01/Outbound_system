# Daily Job Discovery

Standalone daily job discovery and verification service. It builds your 180-search matrix, deduplicates **before** opening job listings, verifies junior/remote/Python signals from the ATS listing, and synchronises jobs, companies, reviews, and run logs to Google Sheets.

## What it does

- Builds 12 ATS platforms × 8 roles × 3 search strategies: five Engineering roles plus Product Designer, UI/UX Designer, and Web Designer.
- Runs only new, changed, or uncertain results through listing verification.
- Identifies jobs by ATS job ID where possible, then by canonical URL, then a conservative fallback fingerprint.
- Keeps every search source that found a job without creating duplicate rows.
- Paginates each Google query until two consecutive pages each yield fewer than three unique valid ATS listings.
- Completes the active query before observing the daily verification target, then resumes at the next query on the next run.
- Writes a separate `Companies` table linked from `Jobs` by `company_id`.
- Provides `run` for manual execution and `schedule` for a locally controlled daily run.
- Uses an indexed local SQLite database for raw search history, locks, page/query checkpoints, retry queues, deduplication, and job lifecycle. Google Sheets receives only finalized reporting tables.

## Prerequisites

1. Node.js 22+.
2. A Google Sheet. The default integration is a bound Apps Script Web App, so no service account is required.
3. A compliant search source:
- `playwright-google`: the default. A headed Playwright browser opens Google Search and the actual ATS listing pages.
- `http`: a provider adapter that accepts `POST { query, queryId }` and returns `{ results: [{ title, link, snippet, displayLink? }] }`.
   - `google-cse`: only for an existing Google Custom Search JSON API customer.
   - `fixture`: safe local smoke-test mode.

Google’s Custom Search JSON API is closed to new customers and its current transition deadline is January 1, 2027, so new deployments should use an approved provider adapter rather than building browser-evasion logic. See Google’s [official overview](https://developers.google.com/custom-search/v1/overview).

## Setup

```bash
cp .env.example .env
# Deploy apps-script/Code.gs as a Web App bound to your Google Sheet.
# Add Script Property API_TOKEN, then set GOOGLE_APPS_SCRIPT_URL and APPS_SCRIPT_TOKEN in .env.
npm run setup-sheet
```

`setup-sheet` creates these tabs and loads the initial query inventory:

```text
Jobs | Companies | Runs | Review Queue
High | Medium | Low I | Low II | Needs Review | Company Boards
```

Set `automationEnabled` to `true` in `config/runtime.json` only after a successful manual test.

Local configuration is authoritative by default: runtime controls come from `config/runtime.json`, and the search matrix and signal defaults come from the local `config/` sources. This keeps scheduling and collection independent of Google availability. `CONTROL_SOURCE=sheet` and `CONFIGURATION_SOURCE=sheet` remain available as optional compatibility modes; when enabled, the corresponding Sheet tabs become authoritative.

### Apps Script deployment

Open the target Sheet, choose **Extensions → Apps Script**, and replace the project files with `apps-script/Code.gs` and `apps-script/appsscript.json`. In **Project Settings → Script properties**, add `API_TOKEN` with a long random value. Deploy it as a **Web app** that executes as you; use a deployment audience appropriate for the machine that will run the bot. Copy the deployment URL into `GOOGLE_APPS_SCRIPT_URL` and use the same token for `APPS_SCRIPT_TOKEN`.

`doGet` is a non-sensitive health check. The bot uses authenticated `doPost` requests for all setup, control reads, upserts, and run-log writes, so the token never appears in a URL.

The Apps Script creates formula-driven priority tabs—`High`, `Medium`, `Low I`, `Low II`, `Needs Review`, and `Company Boards`—from the canonical `Jobs` tab. They show only `job_id`, `company_id`, `company`, `title`, `location`, `application_url`, `junior_status`, `remote_status`, and `status`; the full audit record remains in `Jobs`. They refresh on each projection sync. The bound Sheet also has a **Job Discovery → Refresh priority views** menu for manual repair.

The current Apps Script uses a document lock and batched table rewrites. Company notes are preserved. In the default local-first mode, `npm run setup-sheet` removes the local-only configuration tabs (`Control`, `ATS Platforms`, `Roles & Vocabulary`, `Queries`, and `Rules`) instead of recreating them.

## Run

To deliberately clear all collected local state and the four canonical Sheet outputs before a fresh run:

```bash
npm run reset
```

This retains local configuration and the persistent browser profile, but clears jobs, companies, query progress, raw search history, run history, and the corresponding Sheet projections.

```bash
# Manual run: always permitted, even when the scheduled automation is disabled.
npm run run

# End-to-end Sheet test with one clearly labelled test record; no live search.
npm run smoke-test

# One live Google query + one live ATS verification + Sheet write.
# Each query can be used for this command at most twice; selection then rotates.
npm run live-test

# Long-running scheduler: checks local runtime configuration once per minute.
npm run schedule

# Install the scheduler as a restartable macOS LaunchAgent.
npm run install-scheduler

# Read-only operational status; does not run Google searches.
npm run status

# Tests
npm test
```

## Signal semantics

`junior_status` can be `verified`, `senior_verified`, `not_found`, `unsure`, or `conflicting`.

`remote_status` can be `verified`, `hybrid_verified`, `onsite_verified`, `not_found`, `unsure`, or `conflicting`; `python_status` can be `verified`, `not_found`, or `unsure`. Explicit hybrid or onsite wording from the job page is never reported as remote-only. Any conflict or uncertainty is placed in `Review Queue`; evidence is stored with the job record.

Run `npm run reverify` after changing signal rules to recalculate stored jobs locally and sync only changed final records to the Sheet. It never searches Google or reloads job pages.

Company careers landing pages are recorded without job-page hydration and marked `company_board` in the existing `status` column. For Ashby, `/{company}` is a careers board while `/{company}/{job-id}` is an individual posting.

The direct-junior search is treated as **query-qualified**, not final proof. A new result receives one lightweight listing confirmation. If it reappears unchanged with all signals confirmed, the bot records it as seen without reading the listing again.

Daily listing collection is split evenly by field: with the default 180-listing target, Engineering receives 90 hydrated-listing slots and Design receives 90. The query inventory alternates fields. When a field reaches its allocation, its remaining already-collected candidates are checkpointed locally for a later run without repeating the Google search.

Verified jobs are re-opened after the configured recheck interval even when the Google snippet is unchanged. Jobs missing from a successfully completed source query move through `possibly_closed` to `closed` after the configured number of misses.

## Local data and Sheet boundary

The local `data/state.sqlite` database is the durable operational source of truth. It contains normalized, indexed tables for jobs, companies, runs, query progress, live-test usage, and raw Google result history. An existing `data/state.json` file is imported automatically on first use and retained as a backup.

Raw SERP pages, partial query results, retry queues, hashes, and checkpoints never go to Google Sheets. The Sheet receives the finalized `Jobs`, `Companies`, `Review Queue`, and `Runs` projections. Records marked `test` are excluded from final Sheet synchronization.

## Operational safeguards

Pacing is configurable in `config/runtime.json` with separate randomized ranges for Google result pages, ATS listing reads, inter-query spacing, plus a longer pause after every three Google pages and every three completed queries. `PLAYWRIGHT_PROFILE_DIR` defaults to `./data/browser-profile`, retaining normal local browser state such as consent cookies between runs. If Google presents a verification page, the runner checkpoints the query and stops without retrying that challenge. `maxListingsPerRun` is a daily target, not a hard mid-query cutoff: the active query always finishes before the runner stops.

Every Google page and every listing attempt is checkpointed locally. A verification page, safety limit, search failure, or exhausted ATS retry leaves the current query pending and keeps the cursor in place. The next run resumes from saved page results without repeating completed Google pages. Successful queries advance the cursor; after the complete enabled-query inventory is exhausted, a new cycle begins.

Google pagination validates results against the configured ATS domain, recognizes the actual absence of a next page, and applies the two-consecutive-thin-page rule. The default page ceiling is disabled (`maxPagesPerQuery: 0`), so a paused query resumes from its saved next page; the per-search-session time limit remains as a checkpoint. These controls support respectful, stable use and do not attempt to bypass bot-detection measures.

The scheduler runs missed same-day times when it comes back online, prevents overlapping ticks, waits before retrying scheduler failures, and checkpoints cleanly on `SIGINT`/`SIGTERM`. The macOS LaunchAgent keeps it running across terminal closure and restarts it after a process failure; the local `automationEnabled` value decides whether a daily run may start. With local control selected, the scheduler makes no Google request while idle.
