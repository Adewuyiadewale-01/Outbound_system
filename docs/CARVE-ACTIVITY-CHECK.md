# The check_prefinal_activity.py Carve — Portrait & Slice Plan

**Date:** 2026-09-27 · **Target:** `scripts/check_prefinal_activity.py` (3,155 lines, 73 defs + 1 class)
**Method:** recon (symbol map, consumer census) + full linear read. Baseline: 112/112 tests green; CI green; drift check clean (old repo unchanged since `1f782d8`, already ported).

---

## 1. Recon receipts

| Receipt | Value |
|---|---|
| Lines | **3,155** |
| Top-level defs/classes | **73** (72 functions + `LiveActivityReader`) |
| Entry | `scripts/check_prefinal_activity.py` — CLI (34 `parse_args` flags, incl. `--prepare-only`, `--activity-only`, `--finalize-only`, `--worker-id`), plus a built-in `--self-test` harness |
| Tests | `tests/test_activity_retry_cap.py` (2 tests) + the in-file `run_self_tests()` (asserts the pure analysis functions) |

**Consumer census — *all* non-`.py` invocation surfaces, not just imports.** The Phase A method must scan `.command`, `.html`, `.js`, `.sh`, `.mjs`, `.gs` and any other surface that names the script or its path (the linkedin_helper carve's launcher miss generalises here):

| Consumer | Kind | Names / paths |
|---|---|---|
| `ORCHESTRATION/activity_check/{Activity,Run,Prepare,Test_Limit}.command` | subprocess by path | `python3 scripts/check_prefinal_activity.py [--activity-only\|--prepare-only\|--limit N]` |
| `scripts/run_activity_lanes.py` | subprocess by path | `ACTIVITY_SCRIPT = ROOT/"scripts"/"check_prefinal_activity.py"` |
| `ORCHESTRATION/watcher/orchestration_watcher.py` | subprocess by path + filename match | `[PYTHON, ROOT/"scripts"/"check_prefinal_activity.py", "--date", day]`; string checks on `"check_prefinal_activity.py"` |
| `ORCHESTRATION/monitor_app/dashboard.html` | invoke by path (3 trigger buttons) | `data-script="scripts/check_prefinal_activity.py"` (full run, `--prepare-only`, `--activity-only`) |
| `ORCHESTRATION/monitor_app/drawer.html` | invoke by path (3 trigger buttons) | same three `data-script="scripts/check_prefinal_activity.py"` buttons; dispatched at `drawer.js:2723` (`btn.getAttribute('data-script')`) |
| `ORCHESTRATION/monitor_app/drawer.js`, `main.js` | filename special-case | `scriptPath.endsWith('scripts/check_prefinal_activity.py')` |
| `tests/test_activity_retry_cap.py` | **import** | `import check_prefinal_activity as activity` → uses `activity.next_activity_retry_record` |

**The hard constraint (same shape as the `linkedin_helper` subprocess dep):** the file must remain at `scripts/check_prefinal_activity.py`, stay **runnable as a CLI**, and stay **importable as a module** exposing the names the test (and `--self-test`) use. The logic moves; the entry stays as a thin, importable, executable front door.

---

## 2. Couplings (what the current file imports)

| Import source | Names | Notes |
|---|---|---|
| `linkedin_helper` (shim) | `LinkedInSession`, `canonicalize_linkedin_profile_url`, `relative_days_from_time_text` | Post-carve shim — import direct from `outbound.shared.*` where clean |
| `linkedin_outreach_session` (shim) | `OBF_SHEET_URL`, `_batch_update_cells`, `_find_first_index`, `_find_last_index`, `_is_enabled`, `_parse_int`, `_random_positive_partition`, `_run_diversion`, `sequence_date_key`, `sheet_date` | These re-export from `outbound/shared/{sheetutils,dates,diversion}` and `outbound/outreach/planning` — the new modules can import the real homes (arrows down) |
| `prefinal_queue` (loose module in `scripts/`) | `QUEUE_ROW_COLUMNS`, `enqueue_batch`, `load_batch`, `next_activity_batch`, `update_batch_status` | ⚠️ requires `scripts/` on `sys.path` (see §5) |
| `runtime_environment` | `load_repo_env` | |
| `sheets_helper` (helpers/, awaiting its own carve) | `get_client`, `normalize_rows`, `open_sheet`, `require_columns` | Keep until the sheets carve |
| `gspread` | `utils.rowcol_to_a1`, worksheet IO | third-party |
| **`lead_exec_research`** | `bridge_prefinal_to_prospects`, `ensure_dirs` (lazy, inside `bridge_final_to_prospects`) | **Cross-workflow** — the leads pipeline; stays on `scripts.lead_exec_research` until that carve |

---

## 3. Region map (earned from the read)

> Rows are **concern clusters, not contiguous partitions** — regions deliberately interleave (e.g. retry 151–226 sits inside the text span 140–260; danger 292–360 inside the session span 253–435). Read it as "these functions belong together", not as disjoint line ranges.

| Lines | Concern | Symbols |
|---|---|---|
| 1–137 | Header, imports, config | tab/credential/state paths, `BASE_COLUMNS`…`FINAL_REQUIRED_COLUMNS`, `CATEGORY_PRIORITY`, `ACTIVITY_SCORE/VALUES`, `DIVERSION_OPTIONS`, `NAVIGATION_TYPE_OPTIONS`, danger/reason sets, `DEFAULT_MAX_TARGET_ATTEMPTS` |
| 140–260 | Text/URL parsing | `clean_text`, `_parse_positive_int`, `normalize_url`, `is_linkedin_profile_url`, `normalized_profile_key`, `canonical_linkedin_profile_url`, `normalize_activity_value`, `normalize_navigation_type`, `navigation_type_key`, `target_key` |
| 151–226 | Durable retry accounting | `next_activity_retry_record`, `persist_activity_retry_attempt`, `persist_terminal_activity_issue` |
| 253–435 | Session/journal state | `activity_session_path`, `activity_journal_path`, `append_jsonl`, `journal_event`, `emit_progress`, `load/save_session_state`, `prepare_session_state` |
| 292–360 | Danger classification | `is_blocking_danger`, `hard_read_failure_reason`, `blocked_result` |
| 435–490 | Sheet IO + person rows | `read_worksheet`, `person_from_row`, `set_person` |
| 490–534 | Target extraction | `extract_targets` |
| 534–757 | **Activity analysis (the brain)** | aggregate/non-aggregate, `count_entries_within`, `activity_detail_should_defer`, `detail_contains_text`, `activity_detail_empty_success_reason`, `activity_level`, `activity_evidence`, `activity_score`, `should_swap`, `category_for_row`, `rank_row_for_final`, `final_sort_key`, `is_cdp_transport_exception` |
| 757–955 | Reader | `LiveActivityReader` (recovery, selector fallback, retry loop) |
| 949–996 | Test readers | `build_fixture_reader`, `build_synthetic_activity_reader` |
| 996–1330 | Sequence planning | `resolve_activity_sequence_columns`, 6 × `generated_*` (deterministic per date), `unique_ints_in_order`, `distribute_slots_to_batches`, `ensure_activity_sequence` |
| 1330–1355 | Diversion | `apply_diversion` |
| 1355–1513 | Final upsert + bridging | `write_final_batch_upsert_and_sort`, `row_activity_from_state`, `row_targets`, `target_has_activity_decision`, `ready_final_rows`, `final_source_row_count`, `bridge_ready_rows_to_final` |
| 1513–1822 | Cross-script bridges + finalize | `bridge_final_to_prospects`, `queue_source_rows`, `no_queued_batch_result`, `resume_final_bridged_batch`, `finalize_prepared_session` |
| 1822–2800 | **`run()` — the 976-line orchestrator** | queue load, prepare/lanes/finalize modes, retry caps, bridging, queue-status writes |
| 2800–2998 | Self-test harness | `fake_activity`, `run_self_tests` |
| 2998–3155 | CLI | `parse_args` (34 flags), `main`, `__main__` guard |

---

## 4. Drafted module map — `outbound/activity_check/`

```
outbound/activity_check/
├── __init__.py
├── config.py      constants, columns, scoring maps, danger/reason sets, options
├── text.py        clean_text, _parse_positive_int, normalize_*, canonical_*, target_key
├── retry.py       next_activity_retry_record, persist_activity_retry_attempt, persist_terminal_activity_issue
├── state.py       session/journal paths, append_jsonl, journal_event, emit_progress, load/save/prepare_session_state
├── danger.py      is_blocking_danger, hard_read_failure_reason, is_cdp_transport_exception, blocked_result
├── sheets.py      read_worksheet, person_from_row, set_person
├── targets.py     extract_targets
├── analysis.py    aggregate/count/empty-success/activity_level/evidence/score/swap/category/rank/final_sort_key
├── reader.py      LiveActivityReader, build_fixture_reader, build_synthetic_activity_reader
├── sequence.py    resolve_activity_sequence_columns, generated_*, unique_ints_in_order, distribute_slots_to_batches, ensure_activity_sequence
├── diversion.py   apply_diversion
├── finalize.py    final upsert/sort, ready rows, bridge_* , queue_source_rows, no_queued_batch_result,
│                  resume_final_bridged_batch, finalize_prepared_session
├── runner.py      run()
└── cli.py         parse_args, main, run_self_tests, fake_activity
```

`scripts/check_prefinal_activity.py` → thin entry (ROOT bootstrap + census re-exports + `raise SystemExit(main())`).

**Placement test (§4.1 two-consumer rule):** this is a *workflow* package (like `engagement/`, `outreach/`) — its code is consumed by one workflow, so it does **not** go in `shared/`. Cross-workflow seams (`bridge_final_to_prospects` → leads) stay as explicit imports, to be re-pointed when the leads carve lands.

---

## 5. Traps for this carve

1. **Path + module-name preservation (the big one).** The entry must stay at `scripts/check_prefinal_activity.py`, remain CLI-runnable (4 launchers + watcher + lanes + monitor all invoke it by path) **and** import-stable (`test_activity_retry_cap` does `import check_prefinal_activity`; `--self-test` exercises the moved pure functions through it). Census = whatever those consumers reach through the name.
2. **`scripts/` on `sys.path`.** The file imports the loose module `prefinal_queue` and (lazily) `lead_exec_research` — both live in `scripts/`, not a package. The new package modules must resolve them: either the entry keeps `scripts/` on `sys.path` (simplest, matches today), or switch to `from scripts.prefinal_queue import …` (namespace-package import with ROOT on `sys.path`). **Decision: keep the entry's `scripts/` bootstrap** and have package modules import `prefinal_queue` expecting it — same behaviour as today; revisit when `scripts/` is itself cleaned up.
3. **Cross-workflow coupling** (`bridge_final_to_prospects` → `lead_exec_research`) — stays; flag for the leads carve.
4. **`sheets_helper` / `runtime_environment`** still in `helpers/` — out of scope; keep importing until their carves.
5. **`run()` is 978 lines** — moves whole, one slice; do not split mid-carve.
6. **`--self-test` is a free oracle** — it asserts `activity_level`, `activity_detail_empty_success_reason`, canonicalization, etc. Keep it working through the entry (it doubles as a slice-level receipt).

---

## 6. Slice plan (~15 slices)

Order: leaves first → clusters → `run()` → CLI → entry finalization.

| S# | Destination | Symbols | Deps | Notes |
|---|---|---|---|---|
| S1 | `text.py` | clean_text, _parse_positive_int, normalize_*, canonical_*, target_key | stdlib | Add entry bootstrap + shim anchor (like S1 of the last carve) |
| S2 | `config.py` | all constants/maps/sets/options | stdlib | |
| S3 | `retry.py` | retry records + persist_* | config, targets(keys), prefinal_queue | |
| S4 | `state.py` | session/journal paths, append_jsonl, journal_event, emit_progress, load/save/prepare_session_state | config, text | |
| S5 | `danger.py` | is_blocking_danger, hard_read_failure_reason, is_cdp_transport_exception, blocked_result | config, text | |
| S6 | `sheets.py` | read_worksheet, person_from_row, set_person | sheets_helper, text | |
| S7 | `targets.py` | extract_targets | text, config | |
| S8 | `analysis.py` | the whole scoring/classification cluster | text, config | The brain; `--self-test` covers it |
| S9 | `reader.py` | LiveActivityReader + fixture/synthetic readers | analysis, danger, linkedin_helper(session) | Class moves whole |
| S10 | `sequence.py` | resolve_* + generated_* + distribute_* + ensure_activity_sequence | config, outreach-shim helpers, sheetutils | |
| S11 | `diversion.py` | apply_diversion | shared.diversion, analysis | |
| S12 | `finalize.py` | upsert/sort + ready rows + bridge_* + queue_source_rows + no_queued_batch_result + resume_final_bridged_batch + finalize_prepared_session | sheets, analysis, targets, config, **lead_exec_research** | |
| S13 | `runner.py` | run() | everything | 976 lines, whole |
| S14 | `cli.py` | parse_args, main, run_self_tests, fake_activity | runner + all | |
| S15 | entry finalization | — | — | `scripts/check_prefinal_activity.py` → thin entry (bootstrap + census re-exports + `SystemExit(main())`); receipts |

**Per-slice receipts (frozen):** extract verbatim → ruff gate (0 F821/I001) → def-count (source = 0) → wire re-exports (`# noqa: F401`) → full suite (112) → commit. Plus this carve's extras: `--self-test` must pass at every slice boundary, and at S15 verify the path invocations from the four `ORCHESTRATION/activity_check/*.command` launchers, the watcher, `run_activity_lanes.py`, and the monitor app — **including `dashboard.html` and `drawer.html`** (3 trigger buttons each) and the `drawer.js`/`main.js` filename special-cases.

**Final receipts:** `scripts/check_prefinal_activity.py` ≤ ~120 lines; census names importable through it; `python3 scripts/check_prefinal_activity.py --self-test` OK; `python3 scripts/check_prefinal_activity.py --dry-run` reaches the no-op path; 112 tests green; zero code refs to the script from the new package (arrows down).
