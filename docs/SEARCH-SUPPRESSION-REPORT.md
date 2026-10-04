# Job discovery search suppression — investigation report

**Status:** root cause identified with high confidence; remediation options open for review
**Scope:** the `daily-job-discovery` Google-search pipeline (legacy Node service today; the same search strategy is planned for the Python port in `outbound/job_discovery/`)
**Report date:** 2026-10-04
**Evidence:** [2026-09-29 platform matrix](evidence/2026-09-29-platform-matrix.log) · [2026-09-29 controls](evidence/2026-09-29-controls.log)

## TL;DR

The discovery service has recorded **zero search results since Sep 10**. This is **not** a keyword problem, not a platform problem, not a parsing/filtering bug, and not the network: **Google is quietly suppressing results for the service's automated browser session.** For scoped, operator-heavy queries it serves genuine-looking "did not match any documents" pages or generic fallback results instead of real matches — with no captcha and no error the service can detect, so every run "completes cleanly" at zero.

Manual verification (Oct 4, owner's own Chrome, same machine and network) returned normal, rich results for the **exact same queries**, including the strict operator forms — ruling out the index, the query design, and the IP.

## How the service searches (context)

- 288 queries = 12 ATS platforms × 8 roles × 3 seniority filters. Query text is **identical across platforms except the `site:` scope** (`src/query-builder.mjs`).
- Searches run in a persistent, headed Google Chrome profile (`data/browser-profile`) driven through `scripts/playwright-cli.sh` (`@playwright/cli`), with conservative pacing: 15–30 s between result pages, 3–5 min cooldown every 3 pages, 1.5–3 min between queries, 10–15 min cooldown every 3 queries.
- Only explicit challenge pages (captcha / `/sorry/` / "unusual traffic") raise `SearchBlockedError` (`src/playwright-provider.mjs:132`); anything else parses as "0 results" and the run continues.

## Timeline

| Date | Event |
| --- | --- |
| Aug 20 | Healthy day: **694 results** recorded (all from Ashby queries) across the day's runs. |
| Aug 23 | Last service commit (`0601819`, design track + field quotas). Query templates unchanged since. |
| Sep 10 | Manual run: 10 attempted / 9 completed / **0 results**; stopped `query_pending_retry` after a browser-command failure. |
| Sep 28 (eve) | Google verification page observed during live probing; requests deferred. |
| Sep 29 | Full manual run: 31 attempted / 30 completed / **0 results** / 1 `pending_retry`; stopped `query_pending_retry`. Same day: the platform matrix + controls below identify suppression. |
| Oct 3 | Dashboard fixes committed on this branch (see "Related fixes"). |
| Oct 4 | Owner's manual Chrome check: the **same queries return normal results** → session-level suppression confirmed. |

Note: no runs happened between Aug 20 and Sep 10, so the exact onset of the suppression is unknown; the first observable zero-run is Sep 10.

## Experiment 1 — one high-yield keyword, all 12 platforms (automation session)

Query: `site:<platform> (intitle:"software engineer" OR intitle:"software developer") remote -"no remote"` — the exact template of the historically best query (297 hits on Aug 20). Capped at 2 pages each, production pacing, run Sep 29 through the service's own browser session.

| # | Platform | Kept | Stop | SERP observed |
|---|---|---|---|---|
| 1 | Ashby | 0 | exhausted | "Your search … did not match any documents" |
| 2 | Greenhouse | 0 | exhausted | same |
| 3 | Lever | 0 | exhausted | same |
| 4 | Workable | 0 | exhausted | same |
| 5 | SmartRecruiters | 0 | exhausted | same |
| 6 | Teamtailor | 0 | exhausted | same |
| 7 | Recruitee | 0 | exhausted | same |
| 8 | Pinpoint | 0 | exhausted | same |
| 9 | Breezy HR | 0 | exhausted | same |
| 10 | Comeet | 0 | exhausted | same |
| 11 | Personio | 0 | exhausted | same |
| 12 | Workday | 0 | exhausted | same |

No challenge pages, no errors — every page was a genuine-looking Google results page claiming zero matches.

## Experiment 2 — controls (same automation session)

| Query | Outcome |
| --- | --- |
| `site:jobs.ashbyhq.com` (bare) | **Real Ashby job pages listed** (Inferact, Baseten, Sesame, …) → the domain is indexed; "no jobs exist" is false. |
| `site:jobs.ashbyhq.com software engineer` | **Generic, out-of-scope web results** (encyclopedia/university pages) — the scoped matches were not served. |
| `site:jobs.ashbyhq.com intitle:"software engineer"` | Generic out-of-scope results again. |
| `python tutorial` (plain sanity) | Perfectly normal results → the browser can search. |

## Experiment 3 — manual human session (Oct 4)

The owner searched every second query per platform in personal Chrome: **all surfaced results, including the strict operator forms**, on the same machine and network. The IP and Google's index are therefore exonerated; the differentiator is the automated session.

## Conclusion

1. Google is applying **quiet, selective suppression** to the automation session's scoped/operator searches: empty "no match" pages or de-scoped fallback results, with no detectable challenge. This is why runs "complete cleanly" at zero and why the existing block detector never fires.
2. The query design (identical across platforms, verified in `src/query-builder.mjs`), the result host filter (subdomain-safe, `src/playwright-provider.mjs:134-135`), the platform list, and the network are exonerated.
3. The suppression is dynamic rather than a hard ban — the same session still receives real listings for a bare `site:` query, and the healthy Aug 20 behavior shows the pipeline works when the session is trusted.
4. Likely driver: accumulated session/profile distrust from weeks of metronome-identical automated queries (Aug 20 profile freshness → healthy; later → escalating soft signals: verification page, consent-check failure, empty SERPs). The exact trigger is unproven.
5. Separate observation: the recorded history only ever contains **Ashby**-platform results — the non-Ashby platforms' true yield has never been measured under healthy conditions. The Oct 4 manual check suggests they do yield.

## Related defects found and fixed on this branch

1. **Dashboard launcher repo-root bug** (`675accb`): since the #20/#21 restructures, `orchestrator/dashboard/server/local_server.py` resolved `PROJECT_DIR` one level too high (`~/Desktop`), so standalone launches crashed with `ModuleNotFoundError: No module named 'post_engagement'`. Fixed (`parents[2]`) with a regression test.
2. **Repo `.env` leaking into the legacy node service** (`943c243`): the dashboard passed its full environment to `node src/cli.mjs` children, so port settings in the repo `.env` (`LOCAL_CONTROL_FILE`, `STATE_FILE`, …) overrode the legacy service's own config — producing ENOENT errors on the dashboard's Job Discovery page and a stray empty state database inside the legacy folder. Fixed with `JOB_DISCOVERY_LEGACY_ENV_BLOCKLIST` + `job_discovery_node_env()` on both spawn paths, with tests.

Both are covered by the suite (**337 passing**, ruff clean).

## Open defects (found during investigation, not yet fixed)

1. **Parser misses non-`h3` result layouts.** Results are extracted only from `<a>` elements containing `<h3>`; Google's compact "site listing" layout (visible on the bare `site:` control) renders results the parser does not see. Consider alternate title selectors.
2. **Consent handling does not reload.** `rejectGoogleCookiesIfPresent()` clicks "Reject all" but never re-navigates, so a consent interstitial can still yield a zero-result page even after dismissal.
3. **No suppression telemetry.** Only post-filter result counts are stored. Record per-page raw anchor counts + SERP title so "quiet zero" days are detectable in history instead of silently producing empty runs.

## Recommendations (for review)

1. **Gate the Python port's cutover on a fresh-profile single-query test** — one Ashby + one non-Ashby query through `outbound/job_discovery` on a throwaway profile. If it yields, the port's access approach is already healthier; if not, the session strategy needs rework before switching over (see `docs/PORT-JOB-DISCOVERY.md` §9).
2. **Session strategy options**, roughly by effort: profile rotation/rebuild cadence → human warm-up browsing on the profile → webdriver/fingerprint masking (the port's stealth layer) → attaching to a real, logged-in Chrome profile (most trusted; couples the bot to personal browsing) → non-Google acquisition channels for some platforms (several ATSs expose usable public endpoints).
3. **Add quiet-zero telemetry** (raw anchor count + SERP title per page) so future suppression is visible in history.
4. **Soften block detection:** a "did not match any documents" page on a historically productive query deserves at least a warning flag in the run record.

## Appendix — reproduction

- **Matrix:** a temporary script drove `PlaywrightGoogleSearchProvider.search()` once per platform with `maxPages: 2`, production pacing options, logging `kept / paginationStop / SERP title + first 300 chars of body text`. Scratch scripts were removed after the run; the approach is fully described above and the raw logs are committed under `docs/evidence/`.
- **Controls:** same provider, single page per query, plus `location.href` / `document.title` / body-text capture after each search.
- Both used the service's own session (`--session daily-job-discovery`, persistent profile) so the results reflect exactly what the service sees.
- Key code: `src/query-builder.mjs` (query construction) · `src/playwright-provider.mjs:61` (consent check) · `:132` (challenge detection) · `:134-135` (host filter) · `src/run.mjs:288` (SearchBlockedError handling).
- Service runs history: Aug 20 — 694 results (3 runs); Sep 10 — 10 attempted / 0 results; Sep 29 — 31 attempted / 0 results.
