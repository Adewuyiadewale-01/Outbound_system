# External Dependencies & Live-System Audit

**Date:** 2026-09-28 · **Scope:** everything the Outbound system depends on that lives *outside* this repo, plus the live/staging topology.
**Method:** path/env/launchd sweeps across all repo surfaces + filesystem/launchd verification (all receipts inline; verified 2026-09-28 ~16:20 local).

---

## 0. HEADLINE — there are **two repos and two local clones**, and the live one is not this one

| | **Home clone (LIVE)** | **Desktop clone (STAGED — this one)** |
|---|---|---|
| Path | `~/codex-outreach-automation` | `~/Desktop/Outbound-system` |
| GitHub remote | `Adewuyiadewale-01/Outbound-system` *(hyphen)* | `Adewuyiadewale-01/Outbound_system` *(underscore)* |
| `main` HEAD | `1f782d8` — "drift: … (ported to new repo)", **2026-09-24** | `e686098` — latest restructure commit |
| Watcher | **runs from here** (launchd, every 5 min) | not wired |
| State dir | **2.5 GB, fresh today** (watcher log 16:21, dashboard_cache 16:16, leads 14:11, outreach 11:45) | 920 KB; only `post_engagement` runs (Sep 24–27) + `linkedin_state.json` |
| Dashboard .app bundles | "Orchestration Control Center.app" + backups | none (plain sources) |

**Reading:** this is the running strangler-fig migration — production continues to run the pre-restructure code in the home clone; this clone is the staged successor. Fleet-of-flows status appears **mixed**: `post_engagement` has real runs in *this* clone (Sep 24–27, small targets), while everything else (outreach, leads, activity, followups, withdrawals) still executes in the home clone.

**`git ls-remote` receipt (both are distinct live GitHub repos):**
```
Outbound-system.git  HEAD → 1f782d8f8929ceebda7aff11ce0cc83f016738d3
Outbound_system.git  HEAD → e686098a5424ccf18cabef47ff8f7c47247deef0
```

**Cutover-critical (when this clone becomes production):**
1. **Watcher repoint + label reconciliation** — see §3 (two different labels in play).
2. **State migration decision** — both codebases compute `state/` relative to repo root. Switching the watcher to this clone makes it read *this* (near-empty) state. Choose: copy home `state/` (2.5 GB), share the dir, or fresh-start per flow.
3. **Remove the old-repo fallback** in `orchestrator/dashboard/frontend/main.js` (`resolveProjectDir` falls back to `~/codex-outreach-automation`; on case-insensitive APFS it can silently bind to the old system). Same class of fallback exists in the old repo's copy.
4. **Repackage the dashboard app** — the live "Orchestration Control Center.app" bundles live only in the home clone; rebuild from `orchestrator/dashboard/` at cutover.
5. **Desktop symlink** `~/Desktop/Outreach Orchestration` → `~/codex-outreach-automation/ORCHESTRATION` (points at the old system; update or retire).
6. **End of the drift freezes** — the `drift:` protocol retires once cutover completes.

---

## 1. External projects & directories

| External item | Path | Detail | Touch points |
|---|---|---|---|
| **Daily Job Discovery** (separate git repo, Node ≥22 ESM) | `~/Documents/Automation Journey/daily-job-discovery` | Full workflow: `src/cli.mjs` (run/reverify/status/schedule/…), `config/runtime.json`, `data/state.sqlite.lock`, own `.env`, own tests | Dashboard: `main.js`, `local_server.py`, `dashboard.html`, `drawer.js`. Interface: env `DAILY_JOB_DISCOVERY_ROOT` → presence check (`src/cli.mjs`+`config/runtime.json`+`package.json`) → spawn `node src/cli.mjs …` → lock-file busy check → launchd toggle |
| **Codex automations** | `~/.codex/automations/` (11 entries) | The lead-pipeline schedules: ChatGPT research (12:00), lead-review prep (14:00/16:00), review processing (18:00/22:00), bridges (23:10/23:30/06:30), OBF prep/run (8:25/8:30), hourly acceptance. **All PAUSED**; every `cwds` = the old clone. Dashboard reads `process-approved-leads{,-23-00}/automation.toml` for codex-vs-manual mode | Dashboard: `main.js` (`PROCESS_APPROVED_*_AUTOMATION_PATH`), `local_server.py` |
| **Old repo (production)** | `~/codex-outreach-automation` | See §0 | Watcher launchd agent; state; packaged apps; Desktop symlink; drift protocol |
| **Followups audit dir** | `~/Desktop/audit` | `outbound/followups/runner.py`: `AUDIT_DIR = Path.home()/"Desktop"/"audit"` (exists; contains one leftover probe PDF) | Followups workflow writes audit artifacts there |
| **Sibling automation (not this system)** | — | launchd agents `com.tony.fixmypresence.scraper-cycle`, `com.fixmypresence.backup-pull` (separate project; no references from this repo) | None |

---

## 2. User-level config & data

| Item | Path | Referenced by (selection) |
|---|---|---|
| **Sheets credentials** | `~/.openclaw/credentials/google-sheets.json` (+ `.lead-system.backup.json`) | `outbound/{activity_check,leads,outreach}/*`, `outbound/shared/sheets.py` (CLI default), `orchestrator/dashboard/server/local_server.py` (2 resolvers), `.env` key `GOOGLE_SHEETS_CREDENTIALS` |
| **Chrome CDP profiles** | `~/.openclaw/chrome-profile`, `~/.openclaw/chrome-profile-2` | `config/activity_workers.json`, `config/outreach_workers.json` (**hardcoded absolute `/Users/tonyisbuiding/...` paths**), `scripts/launch-chrome{,-2}.sh` |
| **Repo `.env`** (root, not committed) | keys: `GOOGLE_SHEETS_CREDENTIALS`, `LEAD_RESEARCH_*` (7), `OBF_SHEET_URL`, `ENRICHMENT_SHEET_URL`, `TASK_MANAGER_URL`, `COMPARISON_SHEET_URL`, `SHEET_URL`, `DOC_WEBHOOK_URL`, `DOC_WEBHOOK_SECRET` | loaded via `outbound/shared/env.py` (`load_repo_env`) + the old `runtime_environment` shim |
| **Other users' assets** | `~/.openclaw/{agents,bin,canvas,cron,…}` | OpenClaw runtime — not this system's concern |

---

## 3. launchd agents (inventory + one mismatch)

| Agent (installed) | Points at | Status |
|---|---|---|
| `com.tonyisbuiding.orchestration-watcher.plist` | `cd ~/codex-outreach-automation && python3 ORCHESTRATION/watcher/orchestration_watcher.py` (every 300 s; installed **Jul 9**) | **LOADED & live** (state fresh today; awake file says until 23:30) |
| `com.fulltime-job.daily-job-discovery.plist` | `/opt/homebrew/bin/node …/daily-job-discovery/src/cli.mjs schedule` | LOADED |
| `com.tony.clean-codex-outreach-chats.plist.disabled` | disabled | — |
| `com.fixmypresence.*` | separate project | loaded; unrelated |

**Mismatch (latent breakage):** both the old and this repo's dashboards manage watcher label **`com.outreachautomation.orchestration-watcher`** (they expect to install/uninstall/toggle it), but the *installed* live agent is labeled **`com.tonyisbuiding.orchestration-watcher`**. Consequences: the dashboard's watcher status/toggle never matches the live agent; a dashboard-driven install would create a *second* agent → two watchers competing. Reconcile labels as part of cutover.

**Watcher template + labels:** `orchestrator/watcher/com.outreachautomation.orchestration-watcher.plist.template` (repo) → installed to `~/Library/LaunchAgents/<label>.plist`; `__PROJECT_ROOT__` substituted at install time.

---

## 4. External binaries (hardcoded)

| Binary | Path | Referenced by |
|---|---|---|
| Google Chrome | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` | `scripts/launch-chrome.sh` |
| node | `/opt/homebrew/bin/node` (v25.8.1) | job-discovery plist; dashboard spawns `node` |
| zsh | `/bin/zsh` | all `.command` launchers; watcher plist (`-lc`) |
| python3 | `/usr/bin/python3` (used for JSON parsing) + project venv `.venv/bin/python` | `launch-chrome.sh`; launchers call bare `python3` |

---

## 5. Environment-variable catalog (code-verified)

`GOOGLE_SHEETS_CREDENTIALS` · `OBF_SHEET_URL` · `OBF_PROSPECTS_TAB` · `ENRICHMENT_SHEET_URL` · `LEAD_RESEARCH_SHEET_URL` · `LEAD_RESEARCH_SOURCE_SHEET_URL` · `LEAD_RESEARCH_DESTINATION_SHEET_URL` · `LEAD_RESEARCH_SOURCE_TAB` · `LEAD_RESEARCH_DESTINATION_TAB` · `LEAD_RESEARCH_PREFINAL_TAB` · `LEAD_RESEARCH_REVIEW_TAB` · `LEAD_RESEARCH_FINAL_TAB` · `LEAD_RESEARCH_NOTIFY_EMAIL` · `LINKEDIN_CDP_HOST` · `LINKEDIN_CDP_PORT` · `TASK_MANAGER_URL` · `MONITOR_HOST` · `MONITOR_PORT` · `SMTP_HOST` · `SMTP_PORT` · `SMTP_USER` · `SMTP_PASSWORD` · `SMTP_FROM` · `NOTIFY_TO` · `DAILY_JOB_DISCOVERY_ROOT` · `OUTREACH_AUTOMATION_ROOT` (+ `.env`-only: `COMPARISON_SHEET_URL`, `SHEET_URL`, `DOC_WEBHOOK_URL`, `DOC_WEBHOOK_SECRET`)

---

## 6. Recommendations

**Cutover-critical (blockers when this clone becomes production):**
1. Watcher agent: repoint to `orchestrator/watcher/orchestration_watcher.py` in this repo; reconcile the two labels to one; remove the manual `com.tonyisbuiding` agent.
2. State: decide copy-vs-share for `state/` (2.5 GB in home clone — includes watcher log history, leads runs, outreach sequences, activity sessions, dashboard caches).
3. ✅ **DONE (2026-09-28)** — old-repo fallback removed from `main.js`; the dashboard now warns and uses a diagnostic default instead of ever cross-binding.
4. **Codex automations** (`~/.codex/automations/`, 11 entries, all PAUSED): at cutover, recreate/repoint their `cwds` to this repo — they are the lead-pipeline schedule (ChatGPT research, review processing, bridges, OBF prep/run).
5. Rebuild the "Orchestration Control Center.app" from `orchestrator/dashboard/`; update the Desktop symlink.
6. Per-flow migration status: `post_engagement` already has runs here; confirm each remaining flow's switch date (the watcher currently drives them from the old clone).

**Hygiene (non-blocking):**
7. `config/*.json`: replace hardcoded `/Users/tonyisbuiding/.openclaw/...` profile dirs with `~`-expandable or env values. **Chrome-profile sharing confirmed as intended (2026-09-28)** — same LinkedIn accounts; new-repo state stays in this clone.
8. Credentials resolution is duplicated across ~6 modules with *different* fallback orders (`outreach/config.py`, `outreach/paths.py`, `leads/config.py`, `activity_check/config.py`, `leads/gate_impl.py`, `local_server.py`, `sheets.py` CLI default) — consolidate to one resolver + document.
9. Add/refresh `.env.example` for this clone (the home clone has one; verify parity).
10. Decide the fate of `~/Desktop/audit` (followups audit output) — migrate into repo `state/` or keep documented.
11. `.DS_Store`/`.playwright-cli` etc. — none in this clone; no action.

---

## 7. Verification receipts (selected)

```
stat ~/Library/LaunchAgents/com.tonyisbuiding.orchestration-watcher.plist  → Jul  9 12:04
launchctl list | grep -i "orchestration|job-discovery"  → both loaded
ls -lt ~/codex-outreach-automation/state/ | head  → watcher log + json @ 16:21 today
cat  ~/codex-outreach-automation/state/orchestration_watcher_awake.json → {"pid":47352,"until":"2026-09-28T23:30"}
git ls-remote (both repos)  → distinct HEADs (see §0)
grep WATCHER_LABEL old repo local_server.py → com.outreachautomation.orchestration-watcher (≠ installed label)
config/activity_workers.json + outreach_workers.json → /Users/tonyisbuiding/.openclaw/chrome-profile{,-2}
```

---

## 8. Resolution log

| Date | Item | Resolution |
|---|---|---|
| 2026-09-28 | Old-repo fallback in `main.js` | **Removed** — fails loudly instead of cross-binding |
| 2026-09-28 | npm package name | **Renamed** `codex-outreach-automation` → `outbound-system` |
| 2026-09-28 | Chrome profiles sharing | **Decision: keep shared** (same accounts); new-repo state remains in this clone |
| 2026-09-28 | Codex automations | Discovered & catalogued (11 paused schedules bound to the old clone) — cutover item |
