# The linkedin_helper.py Carve — Phase A: Recon & Reading Portrait

**Date:** 2026-09-27
**Subject:** `helpers/linkedin_helper.py` (6,780 lines, 16 importers)
**Method:** full linear read (all 6,780 lines), AST region map, census expansion, repo-wide consumer verification. Baseline: 112/112 tests green on HEAD; CI green.
**Status:** Phase A complete. This document is the Phase B (slice plan) input.

---

## 1. Recon Receipts

| Receipt | Plan expected | Actual |
|---|---|---|
| `grep -c "^def \|^class "` | ~150+ | **88** defs/classes |
| Symbol map (defs/classes/constants) | — | **112** top-level symbols (88 defs/classes + 24 constants) |
| Census (direct imports) | 16 importers | **16 importers, 14 distinct names** |
| Shim surface incl. patch/attr access | — | **21 names** (14 imported + 7 patch/attr targets) |
| Baseline tests | 112+ green | **112 passed** (local venv + CI on HEAD) |

**The 14 imported names** (consumer counts): `LinkedInSession` ×11, `check_circuit_breakers` ×5, `HumanSimulator` ×2, `_navigate_with_readiness` ×2, `relative_days_from_time_text` ×2, and singles: `_open_profile_activity_from_profile`, `_wait_for_activity_destination`, `_wait_for_activity_feed_state`, `_wait_for_linkedin_ready`, `canonicalize_linkedin_profile_url`, `human_delay`, `increment_counter`, `inject_stealth`, `inspect_profile_action_state`.

**The 7 patch/attr names** (from tests): `verify_no_note_send_ui`, `_click_connect_button`, `_wait_for_connect_modal`, `_inspect_connect_modal`, `_dismiss_connect_modal`, `_wait_for_linkedin_ready`, `_wait_for_activity_feed_state` — the last two overlap with the import census.

**Importers resolved (16):** `scripts/` ×8 (check_prefinal_activity, withdraw_connections, prefinal_to_final, linkedin_followup_runner, test_outreach_lane_modals, probe_sent_invitations_hydration, probe_linkedin_profile_name_selector, probe_profile_fallback_popup), `outbound/engagement` ×2, `outbound/outreach` ×3, `tests/test_linkedin_no_send.py` ×1, `helpers/linkedin_outreach_session.py` (shim chain) ×1, plus the monster's own self-import fallback at line 9.

**Path dependency discovered:** `scripts/test_outreach_lane_modals.py:81` spawns `helpers/linkedin_helper.py` as a **subprocess CLI** (`verify-connection-modal --url`). The shim must remain a runnable CLI at that exact path — a re-export surface alone is not enough. Note: this script is NOT in the pytest suite (suite = `tests/` only), so the subprocess path needs a manual receipt at slice 28.

## 2. The Region Map (earned from the linear read)

| Lines | Concern | Symbols | Notes |
|---|---|---|---|
| 1–49 | Header + bootstrap | docstring, imports | Optional `outreach_helper` import (try/except → None) |
| 52–98 | Constants | CDP/STATE/DIAGNOSTIC paths, MAX_* limits, WARMUP_SCHEDULE, ACCEPTANCE_RATE_*, ACTIVITY_* policy | ⚠️ Paths anchored to `dirname(__file__)/../state`; `WARMUP_SCHEDULE` + `MAX_MESSAGES_PER_DAY` are dead |
| 101–190 | Delays & time parsing | human_delay, relative_days_from_time_text, classify_activity_windows, typing_delay | Pure functions — leaf utilities |
| 192–262 | State persistence & counters | _ensure_state_dir/diagnostic_dir, _safe_slug, load/save_state, get_today/week_key, increment/get/get_weekly_counter | ⚠️ Path-anchored; `increment_counter` writes via `save_state` |
| 266–515 | CDP transport | CDPConnection (245 lines) | Env-var host/port override; `execution_observer` hook consumed at ~3446 |
| 517–605 | Stealth | STEALTH_SCRIPTS (61-line JS island), inject_stealth | Only module-level JS constant |
| 606–894 | Human simulation | HumanSimulator (289 lines) | Depends on delays only |
| 902–955 | Element visibility | is_element_visible, get_visible_elements | `get_visible_elements` is dead |
| 963–1083 | Danger detection | PageType, detect_page (99 lines), check_circuit_breakers | The safety core |
| 1086–1101 | Profile selectors | PROFILE_TOPCARD/READY/ACTION_READY/MORE_CONNECT | Used by mapper + send path |
| 1109–1355 | Readiness & navigation | _wait_for_page_ready (171), _wait_for_linkedin_ready, _navigate_with_readiness, _safe_scroll_or_js | The readiness engine |
| 1363–1495 | Feed reading | detect_post_type, generate_scroll_stop_sequence, execute_feed_scroll | Depends on HumanSimulator |
| 1497–2087 | Profile mapper | _safe_debug_slug, _write_profile_mapper_dump (77), inspect_profile_action_state (186), _open_more_and_find_connect (122), _open_more_and_click_connect (147), browse_profile_briefly, view_profile | The profile-action-state machine |
| 2093–2924 | Activity core | _open_activity_tab (175), _open_profile_activity_from_profile, _try_open_..., _extract_visible_activity_entries (249), canonicalize + _activity_* URL helpers, _wait_for_activity_destination, _activity_invalid_result, _activity_scroll_snapshot, _wait_for_activity_feed_state, _scroll_activity_with_lazy_patience, _page_still_loading | Where all recent fixes live |
| 2927–3611 | Activity readers | read_activity_tab (216), read_activity_tabs_detail (462) | The two orchestrating readers |
| 3614–3706 | Notifications | toggle_profile_notifications, check_notifications | Toggle alive only via dead session method |
| 3709–3787 | Send diagnostics | _write_send_diagnostics | Supports send path |
| 3790–3872 | Session envelope | session_warm_up, session_cool_down | Depends on feed + notifications |
| 3874–4051 | Preflight | preflight_check (173) | ⚠️ Coupled to outreach_helper (acceptance-rate gate) |
| 4054–5047 | Send engine | send_connection_request (207), send_connection_only, verify_no_note_send_ui, modal cluster (7 functions) | The send cluster |
| 5049–5255 | Sent invitations | _normalize_* ×2, scrape_sent_invitations (127), _extract_sent_invitations_dom | Acceptance infra |
| 5258–5606 | Acceptance detection | verify_acceptance_via_profile, check_acceptances_subtractive (155), check_acceptances | Acceptance infra |
| 5609–5797 | Connections + prospecting | _extract_connections_dom, scan_prospect_box | |
| 5799–5942 | Withdrawal | withdraw_connection (144) | Self-contained action |
| 5945–6112 | Acceptance notifications | check_acceptance_notifications, _extract_acceptance_notifs_dom | |
| 6115–6302 | Legacy orchestrator | run_session (129), _execute_interleave | Alive via CLI only |
| 6304–6615 | Session class | LinkedInSession (312 lines, ~24 methods) | Thin delegation + rate-limit/danger gates |
| 6623–6780 | CLI | main() (154 lines, 17 subcommands) | Pinned by subprocess dep |

## 3. Census & Session-Method Verification

**Session methods actually called by consumers:**

| Method | Callers |
|---|---|
| connect / disconnect | 12 / 11 files |
| read_activity_detail | 4 (engagement/browser, check_prefinal_activity, prefinal_to_final, withdraw_connections) |
| inspect_profile_action_state | 3 (outreach/guards, outreach/lead_machine, test_outreach_lane_modals) |
| verify_no_note_send_ui | 3 (outreach/guards, test_outreach_lane_modals, tests/test_linkedin_no_send) |
| get_quotas | 2 (engagement/runner, outreach/runner) |
| send_connection_only | 2 (engagement/runner, outreach/lead_machine) |
| warm_up | 1 (outreach/runner) |
| read_activity | 1 (outreach/activity) |
| check_accepts_subtractive | 1 (outreach/acceptance) |
| engagement_trail / reaction_scan / prospect_box / read_feed | 1 (outbound/shared/diversion) |

**Methods with zero external callers but kept alive by the CLI:** `send_connection`, `like`, `check_accepts`, `check_acceptance_notifs`, `withdraw`, `set_notifications` (no CLI subcommand — truly dead), `batch_session` (no CLI subcommand — truly dead), `cool_down` (session method; alive via CLI cool-down).

**Conclusion: nothing is deletable via the shim.** The CLI subprocess dependency pins 17 subcommands and their backing methods. `scripts/withdraw_connections.py` imports functions directly (LinkedInSession, check_circuit_breakers, HumanSimulator, _navigate_with_readiness, inspect_profile_action_state, relative_days_from_time_text) — the function-level import surface, not just the class.

## 4. The Monkeypatch Surface (what keeps tests working)

`tests/test_linkedin_no_send.py` patches on the module object: `_wait_for_linkedin_ready`, `_click_connect_button`, `_wait_for_connect_modal`, `_inspect_connect_modal`, `_dismiss_connect_modal`, `check_circuit_breakers`, `human_delay`. `tests/test_post_engagement_recovery.py` string-patches `linkedin_helper._wait_for_linkedin_ready` and `linkedin_helper._wait_for_activity_feed_state`.

These tests patch where the function is **imported**, but `verify_no_note_send_ui` (the function under test) reads them as module globals inside the monster. After the carve, the reading site moves to the new owner module. **Patches on the shim will not affect the real reading site.** Mitigation (engagement-carve Slice 7 pattern): tests migrate with the slice that moves the patched function — patch where the function is READ, invoke through the shim. Affected slices: send/engine (S19) and readiness/feed_state (S8/S12).

## 5. Couplings & Risks

1. **Path anchors (the big one).** `STATE_DIR`/`STATE_FILE`/`DIAGNOSTIC_DIR` (line 56–58) and the `debug` dir (line 1539) derive from `os.path.dirname(__file__)` + `../state`. A module at `outbound/shared/state/persistence.py` resolving `dirname(__file__)/../state` would land the state dir **inside the package**. The §8 Path Resolution Standard covers this: the moved constants must use `Path(__file__).resolve().parents[N]` with N = actual depth (parents[2] = repo root from `outbound/shared/state/`). This is the one place the pure-move rule bends: constants move + path arithmetic changes in the same commit, with a before/after resolved-path receipt (probe prints resolved paths both ways; state file location must be byte-identical).
2. **outreach_helper coupling.** Header try/except import of `count_outreach_log_connection_requests` + `count_pipeline_connected_leads`, used only by `preflight_check`'s acceptance-rate gate. helpers/ stays on sys.path for all consumers during this carve, so the import keeps working; the coupling moves with preflight to `session/preflight.py`, try/except preserved.
3. **`execution_observer` hook.** `report_navigation` (inside read_activity_tabs_detail) reads `getattr(cdp, "execution_observer", None)` — an attribute attached externally by engagement/browser.py. Not a blocker; document in the module docstring where it lands.
4. **JS islands are pervasive, not localized.** ~30 f-string/JS evaluate payloads live inside functions. They move with their functions; no named-constant extraction needed. `STEALTH_SCRIPTS` is the only module-level JS island.
5. **Duplicate assignment.** `tabs_checked` assigned twice back-to-back in `read_activity_tab` — harmless merge artifact; moves as-is per the pure-move rule.
6. **`read_activity_tabs_detail` (462 lines, dual navigation modes, closures)** is the hardest single function. It moves whole — never split mid-carve.
7. **`LinkedInSession` moves whole** (precedent: the state machine moved whole). 24 methods, thin delegations + uniform rate-limit/danger-gate pattern.
8. **Dead code inventory (ride along, flagged for post-carve cleanup):** `WARMUP_SCHEDULE`, `MAX_MESSAGES_PER_DAY`, `get_visible_elements`, session methods `set_notifications` + `batch_session`, and the vestigial `enable_notifications` parameter (threaded through 4 signatures + 2 call sites but `send_connection_request` never calls the toggle — see §10).

## 6. The Drafted Module Map

```
outbound/shared/
├── browser/
│   ├── connection.py     CDPConnection, CDP_HOST/PORT                    [266–515, 52–53]
│   ├── stealth.py        STEALTH_SCRIPTS, inject_stealth                 [517–605]
│   ├── visibility.py     is_element_visible, get_visible_elements       [902–955]
│   └── readiness.py      _wait_for_page_ready, _wait_for_linkedin_ready,
│                         _navigate_with_readiness, _safe_scroll_or_js    [1109–1355]
├── danger/
│   └── detection.py      PageType, detect_page, check_circuit_breakers   [963–1083]
├── human/
│   ├── delays.py         human_delay, typing_delay                       [101–117, 178–190]
│   └── simulator.py      HumanSimulator                                  [606–894]
├── state/
│   ├── persistence.py    STATE_* constants, _ensure_state_dir, _safe_slug,
│   │                     load_state, save_state                          [56–58, 192–219]
│   └── counters.py       get_today/week_key, counters, MAX_* limits,
│                         WARMUP_SCHEDULE, ACCEPTANCE_RATE_*              [61–73, 221–262]
├── activity/
│   ├── tab_policy.py     ACTIVITY_* constants                             [74–93]
│   ├── url_utils.py      canonicalize_linkedin_profile_url, _activity_* URL helpers [2581–2640]
│   ├── navigation.py     _open_activity_tab, _open_profile_activity_from_profile,
│   │                     _try_open_profile_activity_from_profile        [2093–2327]
│   ├── feed_state.py     _page_still_loading, _activity_scroll_snapshot,
│   │                     _wait_for_activity_feed_state, _activity_invalid_result,
│   │                     _wait_for_activity_destination                  [2642–2860]
│   └── readers.py        _extract_visible_activity_entries, read_activity_tab,
│                         read_activity_tabs_detail, classify_activity_windows,
│                         relative_days_from_time_text                    [118–175, 2330–2578, 2927–3611]
├── profile/
│   ├── mapper.py         inspect_profile_action_state, _open_more_and_find_connect,
│   │                     _open_more_and_click_connect, browse_profile_briefly,
│   │                     view_profile, PROFILE_* selectors               [1086–1101, 1581–2087]
│   ├── diagnostics.py    _safe_debug_slug, _write_profile_mapper_dump    [1497–1578]
│   └── notif_toggle.py   toggle_profile_notifications, check_notifications [3614–3706]
├── send/
│   ├── engine.py         send_connection_request, send_connection_only,
│   │                     verify_no_note_send_ui                          [4054–4336]
│   ├── modal.py          _inspect/_dismiss/_wait_for_connect_modal,
│   │                     _click_connect_button, _click_add_note_button,
│   │                     _type_connection_note, _click_send_button       [4339–4764]
│   └── diagnostics.py    _write_send_diagnostics                         [3709–3787]
├── acceptance/
│   ├── normalization.py  _normalize_linkedin_profile_url, _normalize_sent_invitation_name [5049–5060]
│   ├── sent_scraper.py   scrape_sent_invitations, _extract_sent_invitations_dom [5063–5255]
│   ├── subtractive.py    verify_acceptance_via_profile, check_acceptances_subtractive,
│   │                     check_acceptances                               [5258–5606]
│   ├── connections.py    _extract_connections_dom                        [5609–5721]
│   └── notifications.py  check_acceptance_notifications, _extract_acceptance_notifs_dom [5945–6112]
├── feed/
│   └── post_types.py     detect_post_type, generate_scroll_stop_sequence,
│                         execute_feed_scroll                             [1363–1495]
├── actions/              (graduation candidates — one consumer each today, workflow carves coming)
│   ├── withdrawal.py     withdraw_connection                             [5799–5942]
│   └── prospecting.py    scan_prospect_box                               [5724–5797]
├── session/
│   ├── envelope.py       session_warm_up, session_cool_down              [3790–3872]
│   ├── preflight.py      preflight_check                                 [3874–4051]
│   ├── interleave.py     run_session, _execute_interleave                [6115–6302]
│   └── manager.py        LinkedInSession                                 [6304–6615]
└── cli.py                 main(), argparse                                [6623–6780]
```

**Corrections vs. the hypothesized map:** `browser/navigation.py` merges into `readiness.py`; `_page_still_loading` goes to `activity/feed_state.py` (its only consumers are activity functions); withdrawal + prospecting get `actions/` instead of misc/; `session/` becomes its own package; `relative_days_from_time_text` + `classify_activity_windows` live in `activity/readers.py` (both exist to serve activity reading).

**The placement test every module must pass:** "Will ≥2 workflows import this after the remaining 5 carves?" Browser/danger/human/state: yes (every workflow). Activity: yes (engagement, outreach, activity-check). Profile: yes (outreach, engagement, activity-check, leads). Send: yes (outreach, engagement). Acceptance: yes (outreach, activity-check, withdrawals). Session: yes (every un-carved script uses LinkedInSession). Feed: yes (engagement, outreach via diversion). Actions: 1 consumer each today — flagged as graduation candidates, not blocked.

## 7. The Slice Plan Skeleton (Phase B input)

Order: leaves first, then dependency clusters, class last, CLI + shim finalization at the end.

| # | Slice | Contents |
|---|---|---|
| S1 | `human/delays.py` | human_delay, typing_delay (zero deps; warm-up slice for the dance) |
| S2 | `state/persistence.py` | STATE_* constants, _ensure_state_dir, _safe_slug, load/save_state — **path rewrite + receipt** |
| S3 | `state/counters.py` | keys, counters, MAX_* limits, WARMUP_SCHEDULE, ACCEPTANCE_RATE_* |
| S4 | `danger/detection.py` | PageType, detect_page, check_circuit_breakers |
| S5 | `browser/connection.py` | CDPConnection, CDP_HOST/PORT |
| S6 | `browser/stealth.py` | STEALTH_SCRIPTS, inject_stealth |
| S7 | `browser/visibility.py` | is_element_visible, get_visible_elements (dead one rides along) |
| S8 | `browser/readiness.py` | _wait_for_page_ready, _wait_for_linkedin_ready, _navigate_with_readiness, _safe_scroll_or_js — **test-patch migration: _wait_for_linkedin_ready** |
| S9 | `human/simulator.py` | HumanSimulator |
| S10 | `activity/tab_policy.py` + `activity/url_utils.py` | ACTIVITY_* constants + URL helpers (canonicalize is a census name) |
| S11 | `activity/navigation.py` | the _open_* trio |
| S12 | `activity/feed_state.py` | snapshot, feed-state waiters, invalid-result, destination waiter, _page_still_loading — **test-patch migration: _wait_for_activity_feed_state** |
| S13 | `activity/readers.py` | extractor + read_activity_tab + read_activity_tabs_detail + timing classification — **the hardest slice** |
| S14 | `profile/diagnostics.py` | mapper dump writers |
| S15 | `profile/mapper.py` | inspect + _open_more_* pair + browse + view_profile + selectors |
| S16 | `profile/notif_toggle.py` | toggle + check_notifications |
| S17 | `send/diagnostics.py` | _write_send_diagnostics |
| S18 | `send/modal.py` | the modal cluster |
| S19 | `send/engine.py` | send_connection_request, send_connection_only, verify_no_note_send_ui — **test migration: test_linkedin_no_send** |
| S20 | `acceptance/normalization.py` + `sent_scraper.py` | normalizers + scrape + DOM extractor |
| S21 | `acceptance/subtractive.py` + `connections.py` + `notifications.py` | the acceptance checkers |
| S22 | `feed/post_types.py` | detect_post_type + scroll sequence + execute_feed_scroll |
| S23 | `actions/` | withdraw_connection, scan_prospect_box |
| S24 | `session/envelope.py` | session_warm_up, session_cool_down |
| S25 | `session/preflight.py` | preflight_check (outreach_helper try/except moves with it) |
| S26 | `session/interleave.py` | run_session, _execute_interleave |
| S27 | `session/manager.py` | LinkedInSession — moves whole |
| S28 | `cli.py` + main() | CLI moves; **manual receipt: `python3 helpers/linkedin_helper.py health` via the shim** |
| S29 | shim finalization | ~150-line armored shim; census verification — all 21 names importable; zero stale references |

Estimated ~29 slices (plan estimated 18–22; the 112-symbol surface justifies the upper bound). S1–S9 are small and can pair per session; the dance stays per-slice, commits stay per-slice.

## 8. The Trap Checklist for This Carve

- **Test-patch migration is mandatory** (§4): slices S8, S12, S19 trigger it.
- **The subprocess CLI dep** is not covered by pytest — manual receipt at S28.
- **Path-depth rule** (§5.1): S2 is the path-rewrite slice; before/after resolved-path receipt required.
- **`outbound/shared/` already exists** (dates.py, diversion.py, sheetutils.py) — new subpackages land as siblings; no name collisions verified. ✅
- **Per-slice receipts stay frozen** from the outreach carve: fresh map → boundary checks (sed) → extract (sed append + count receipt) → module gate (ruff, zero F821s) → deletes (bottom-to-top) → reload editor → wire imports (armored `# noqa: F401`) → def-count receipt (source = 0) → npm test → commit → push.

## 9. Phase A Verdict

The hypothesized module map survives contact with the code, corrected in four places (readiness merge, _page_still_loading placement, actions/ package, session/ package). The census is complete and expanded beyond the plan (imports + patch targets + subprocess dep). Baseline is green. **Ready for Phase B: slice plan finalization.**

## 10. Addendum — Vestigial Wiring Confirmed

- **`enable_notifications` is a dead parameter:** threaded through `send_connection_request` → `send_connection_only` → `verify_no_note_send_ui` → `run_session` → `LinkedInSession.send_connection` → CLI `--notify` (4 signatures + 2 call sites), but `send_connection_request` never calls `toggle_profile_notifications`. Flagged for a post-carve behavior-cleanup PR (wire it or delete it).
- **The CLI pins 8 otherwise-dead session methods** (send_connection, like, check_accepts, check_acceptance_notifs, withdraw, engagement_trail, reaction_scan, prospect_box — lines 6748–6770): confirms nothing is deletable via the shim; the subprocess dependency pins the full surface.
