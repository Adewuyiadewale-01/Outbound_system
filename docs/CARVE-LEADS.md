# The Leads Pipeline Carve — Portrait & Staged Plan

**Date:** 2026-09-27 · **Cluster:** the leads pipeline (9 scripts, 6,345 lines) · **Drift check:** clean (old repo unchanged since `1f782d8`, ported)
**Method:** structural portrait — full symbol map + coupling/census across *all* invocation surfaces + targeted reads. Line-level reading happens per slice during execution (the frozen dance's fresh-map + boundary checks).

---

## 1. Recon receipts

| Script | Lines | Defs | Role |
|---|---|---|---|
| `scripts/lead_exec_research.py` | **4,078** | **138** | The hub: employee-db → research → destination rows → bridging; imported by 11 siblings + 2 tests + `outbound/activity_check` |
| `scripts/lead_review_lifecycle.py` | 1,109 | 23 | Review classification → archive promotion → Pre-final publish → Final/Prospects finalize |
| `scripts/lead_research_archive.py` | 346 | — | Durable research archive (index + hydrate) |
| `scripts/migrate_unreviewed_lead_batches.py` | 267 | — | One-off migration utility |
| `scripts/sync_lead_review_group.py` | 168 | — | Review-group sync (also invoked by the monitor app) |
| `scripts/assign_lead_review_lanes.py` | 126 | — | Lane assignment |
| `scripts/update_lead_review_use.py` | 94 | — | `Use` toggle |
| `scripts/update_lead_review_group_status.py` | 83 | — | Group status |
| `scripts/ensure_primary_lane_columns.py` | 74 | — | Column bootstrap |

Baseline: 112/112 tests green; CI green.

---

## 2. Consumer census (all surfaces — `.py`, `.js`, monitor app, tests)

**The hub's import surface (must survive as a compatibility entry):**

| Consumer | Imports |
|---|---|
| `scripts/lead_review_lifecycle.py` | large block from `lead_exec_research` (`:30`) |
| `scripts/assign_lead_review_lanes.py` | block (`:13`) |
| `scripts/sync_lead_review_group.py` | block (`:13`) |
| `scripts/update_lead_review_group_status.py` | block (`:12`) |
| `scripts/ensure_primary_lane_columns.py` | block (`:16`) |
| `scripts/migrate_unreviewed_lead_batches.py` | block (`:12`) |
| `scripts/cache_lead_review_dashboard.py` | block (`:13`) |
| `scripts/update_lead_review_use.py` | `DEFAULT_CREDS, DEFAULT_REVIEW_TAB, DEFAULT_SHEET_URL` |
| `scripts/update_lead_review_approval.py` | same defaults |
| `scripts/ensure_test_pipeline_tabs.py` | `DEFAULT_OBF_SHEET_URL, DEFAULT_SHEET_URL` |
| **`outbound/activity_check/finalize.py`** | `bridge_prefinal_to_prospects, ensure_dirs` (lazy) — **cross-carve, must re-point** |
| `tests/test_lead_exec_research_state.py` | block |
| `tests/test_lead_lane_assignment.py` | `assign_primary_lanes` |
| **Monitor app** | `local_server.py` + `main.js` reference `sync_lead_review_group`, `update_lead_review_use`, `update_lead_review_group_status` (script paths) |

**Other test consumers:** `test_lead_review_lifecycle`, `test_lead_research_archive`, `test_lead_review_dashboard_cache`, `test_sync_lead_review_group`, `test_update_lead_review_use`.

**Census lesson applied:** scanned `.py`/`.command`/`.html`/`.js`/`.sh`/`.mjs`/`.gs` — no `.command` launcher invokes the leads scripts directly; the monitor app does (by script path).

---

## 3. Couplings

| Dependency | Detail | Disposition |
|---|---|---|
| `sheets_helper` (helpers/) | **35 consumers** across outreach, activity_check, leads, withdrawals, monitor_app | **Graduate to `outbound/shared/sheets`** (§4.1: 2+ workflows) — Stage 3 |
| `lead_research_archive` (sibling script) | imported by the hub + migration + 2 tests | Stage 2 (join the leads package) |
| `prefinal_queue` (scripts/) | `enqueue_batch`, `record_prefinal_publish`, `rows_fingerprint` | stays; same `scripts/` bootstrap wrinkle as activity_check |
| `runtime_environment` | `load_repo_env` | helpers; out of scope |
| `activity_check` → hub | `bridge_prefinal_to_prospects`, `ensure_dirs` | **re-point at Stage 1** to `outbound.leads.*` |
| `smtplib`/`email` | the hub sends review-notification emails | preserve behaviour; note the external-action surface |

---

## 4. Region map — `lead_exec_research.py` (from the full symbol map)

| Clusters (concern, not contiguous) | Symbols |
|---|---|
| Config: paths, columns, tabs, patterns | `STATE_DIR`…`RUNS_DIR`, `SOURCE_COLUMNS/ALIASES`, `DESTINATION/OPTIONAL/FINAL/PROSPECTS_COLUMNS`, `DEFAULT_*`, `REVIEW_COLUMNS`, `EXEC_TITLE_PATTERNS`, `ROLE_KEYWORDS` |
| Text/identity utils | `clean_text`, `normalize_key/url`, `strip_accents`, `meaningful_name_parts`, `token_overlap_score`, `linkedin_url_name_score`, `is_linkedin_profile_url` |
| Sheet IO | `read_worksheet`, `canonicalize_source_rows`, `require_columns`, `source_value`, `load_destination_ids`, `load_destination_company_by_id`, `load_reviewed_ids` |
| Grouping + lanes | `group_source_rows`, `add_employee`, `chunks`, `assign_primary_lanes`, `looks_like_*_row` |
| Search | `SearchClient`, `parse_duckduckgo/bing/yahoo_results`, `unwrap_*_url`, `strip_tags`, `search_query_variants` |
| Exec extraction + scoring | `extract_exec_candidates`, `extract_name_role_pairs`, `clean_person_name/role`, `is_plausible_person_name`, `seniority_score`, `title_weight`, `employee_role_score`, `compact_company_name` |
| Reconcile + choose | `search_execs_for_company`, `fallback_execs_from_employees`, `match_employee`, `score_search_candidate`, `search_linkedin_for_exec`, `reconcile_exec`, `choose_people` |
| Destination rows | `build_destination_row(_from_computation)`, `select_computation_people`, `prefinal_row_readiness`, `filter_ready_prefinal_rows`, `computation_rows_for_write`, `verify_destination_rows`, `write_computation_rows`, `write_destination_rows` |
| Computation archive | `archive_computation_state`, `archive_unreviewed_computation`, `consume_reused_archive_entries` |
| Bridge (Pre-final → Prospects) | `parse_bridge_date`, `bridge_date_*`, `bridge_state_path`, `prefinal_rows_for_bridge`, `prefinal_bridge_fingerprint`, `pick_engaged_person`, `prospect_row_from_prefinal`, `existing_prospect_ids`, `first_prospect_write_row`, `write_prospect_rows`, `record_outreach_control_prospects_start_row`, **`bridge_prefinal_to_prospects`** |
| Review tab + rows | `ensure_review_tab`, `build_review_row`, `review_group_date_*`, `last_nonempty_review_row`, `existing_successful_review_group_row`, `find_successful_review_group_row_for_today`, `write/read_review_rows`, `remove_review_group_for_run`, `checkbox_truthy`, `update_review_statuses` |
| Gates + runs status | `approved_gate_status`, `claim/mark_approved_gate`, `computation_status_counts`, `next_approved_action`, `approved_workflow_status`, `export_pending_search_tasks` |
| Notifications | `send_review_email`, `review_email_subject/body`, `enqueue_review_notification`, `notify_review_with_fallback` |
| Run builders + files | `save/load_run`, `latest_*_file`, `*_fingerprint`, `preflight`, `source/destination_sheet_url`, `archive_index_path`, `annotate_overlap_scan`, `collect_overlap_top_up_waves`, `build_run`, `prepare_review`, `notify_review`, `filter_run_to_approved`, `approved_leads_from_review` |
| CLI | `main` (+ parsers) |

---

## 5. Staged plan

**Stage 1 — the hub (this is the §9.4 trigger: the repo's largest file).** Extract `scripts/lead_exec_research.py` → `outbound/leads/` (~16 modules: config, text, sheets, grouping, search, extract, reconcile, destination, computation, bridge, review, gates, notify, runs, workflow, cli) + thin entry shim at the same path preserving the **11-script + 2-test + activity_check** import surface. ~20 slices. Re-point `outbound/activity_check/finalize.py` to `outbound.leads.bridge` in the same stage.

**Stage 2 — the review/lifecycle cluster.** `lead_review_lifecycle.py` + `lead_research_archive.py` + the six small `lead_review*`/lane/migration scripts → `outbound/leads/review/*`, with their CLI entry points preserved at `scripts/` paths (the monitor app invokes them by path) and the hub entry as the shared import shim until each consumer is re-pointed.

**Stage 3 — `sheets_helper` graduation.** 1,451 lines, **35 consumers** → `outbound/shared/sheets` (2+ workflows), with a 35-file consumer re-point. This is a shared-infrastructure carve, not a leads one — it lands after the leads clusters so the consumer set has settled.

**Per-slice receipts (frozen):** extract verbatim → ruff gate (0 F821/I001) → def-count (source = 0) → wire re-exports (`# noqa: F401`) → full suite (112) → commit. Carve-specific extras: at Stage 1's entry finalization, verify the **11 sibling imports + 2 test imports + the activity_check bridge** resolve through the entry; at Stage 2, re-verify the monitor-app script paths.

---

## 6. Traps for this carve

1. **The biggest import surface yet.** 11 sibling scripts + 2 tests + a package all import the hub. Most import *blocks* of names, so the entry shim's census must enumerate every name every consumer touches — and Stage 1's execution must migrate or re-point them, not just re-export blindly. Budget for a name-by-name census like the `linkedin_helper` one.
2. **The hub is not one workflow slice — it's five sub-domains** (search/scrape → reconcile → destination rows → bridge → review/notify). Slicing must follow those seams, not line ranges.
3. **`smtplib`/`email`** — the hub sends review emails. Behaviour must be preserved exactly (it's an external-action surface; no accidental "improvements" during a pure move).
4. **`activity_check/finalize.py` cross-import** — re-point in Stage 1; the reverse dependency must be covered by the Stage-1 PR's receipts.
5. **Monitor-app path invocations** (`sync_lead_review_group`, `update_lead_review_use`, `update_lead_review_group_status`) — Stage 2 keeps those script paths intact (or updates `local_server.py`/`main.js` in the same PR, per §5.6 launcher ownership).
6. **`sheets_helper` cannot be left half-migrated** — Stage 3 moves 35 consumers atomically; sequence it *after* Stage 1/2 so the leads consumers are already in `outbound/`.
7. **Structural reading disclosure:** this portrait is map-level. Each slice's fresh-map + boundary checks provide the line-level rigor before extraction — the same discipline the previous carves applied, just deferred to the slice rather than pre-loaded into the plan.

---

## 7. Recommendation

Do **Stage 1** next (the hub — biggest file, biggest blast radius, unblocks Stage 3's consumer set). Stage 2 then retires the review scripts; Stage 3 graduates `sheets_helper` once the leads consumers live under `outbound/`.
