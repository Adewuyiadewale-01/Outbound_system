# The linkedin_helper.py Carve — Phase B: The Slice Plan (finalized)

**Date:** 2026-09-27
**Input:** `docs/CARVE-LINKEDIN-HELPER.md` (Phase A portrait — recon, census, region map)
**Status:** Phase B complete. This is the execution spec for Phase C.

---

## 0. Pre-execution corrections (found while finalizing)

1. **Name collision — `state/` is taken.** `outbound/shared/state.py` already exists (extracted from post_engagement during the engagement carve: locking, atomic JSON, daily ledger, per-day RNG). The monster's state/quota cluster therefore cannot become `outbound/shared/state/`. **Final home: a single module `outbound/shared/quota.py`** (STATE_* constants, load/save, day/week keys, counters, MAX_* limits). It is a different concern from `state.py` and must not merge with it (pure-move discipline; adoption is a separate decision).
2. **Slice order fix.** The Phase A skeleton had `danger/` (S4) before `browser/connection.py` (S5). Wrong: `detect_page(cdp: CDPConnection)` and friends annotate the class, and the new modules adopt `from __future__ import annotations` (repo convention — see `outbound/shared/*.py`). Order is now: connection → stealth → visibility → danger → readiness. (The gate also catches any lapse: ruff F821 = zero tolerance.)
3. **Second path-rewrite slice.** The `debug/` dump dir (monster line 1539) lives inside `profile/diagnostics.py`. So **S2 (quota) and S14 (profile/diagnostics) are both path-rewrite slices** with before/after receipts.
4. **The shim must insert `ROOT` itself.** `tests/test_linkedin_no_send.py` inserts only `ROOT/helpers` before `import linkedin_helper`; the subprocess CLI (`python3 helpers/linkedin_helper.py …`, run with `cwd=ROOT` by `test_outreach_lane_modals`) gets only the script dir on `sys.path`. Since the shim will import `outbound.*`, it must bootstrap `ROOT` + `HELPERS_DIR` itself (pattern proven by `helpers/linkedin_outreach_session.py`).
5. **Drift check (carve start) — clean.** Old repo `~/codex-outreach-automation` has exactly one `drift:` commit (`1f782d8`), already ported to this repo as `7e6fc40`. No pending drift. Freeze stands; re-check at every Phase C session start.

**No other collisions:** `browser/`, `danger/`, `human/`, `activity/`, `profile/`, `send/`, `acceptance/`, `feed/`, `actions/`, `session/`, `cli.py` do not collide with existing shared modules (`state.py`, `dates.py`, `diversion.py`, `sheetutils.py`).

---

## 1. Final module map

```
outbound/shared/
├── browser/{__init__,connection,stealth,visibility,readiness}.py
├── danger/{__init__,detection}.py
├── human/{__init__,delays,simulator}.py
├── quota.py                              ← collides-free home for state+counters+limits
├── activity/{__init__,tab_policy,url_utils,navigation,feed_state,readers}.py
├── profile/{__init__,mapper,diagnostics,notif_toggle}.py
├── send/{__init__,engine,modal,diagnostics}.py
├── acceptance/{__init__,normalization,sent_scraper,subtractive,connections,notifications}.py
├── feed/{__init__,post_types}.py
├── actions/{__init__,withdrawal,prospecting}.py   (graduation candidates: 1 consumer today)
├── session/{__init__,envelope,preflight,interleave,manager}.py
└── cli.py
```
Every package gets `__init__.py` (matches `outbound/shared/`, `outbound/engagement/`, `outbound/outreach/` convention). `outbound/` itself is a PEP 420 namespace — leave it.

---

## 2. The slice table

Columns: **S#** · **destination** · **symbols moved** · **internal deps (must already exist)** · **consumer edits** · **notes**.

| S# | Destination | Symbols | Deps | Consumer edits | Notes |
|---|---|---|---|---|---|
| S1 | `human/delays.py` | human_delay, typing_delay | stdlib | — | **Add shim bootstrap** (sys.path + first armored re-export block). Warm-up slice: proves the dance |
| S2 | `quota.py` | STATE_DIR/FILE/DIAGNOSTIC_DIR, _ensure_*_dir, _safe_slug, load/save_state, get_today/week_key, increment/get/get_weekly_counter, MAX_* limits, WARMUP_SCHEDULE, ACCEPTANCE_RATE_* | stdlib | `outbound/engagement/runner.py:265` → `from outbound.shared.quota import increment_counter` | ⚠️ **PATH REWRITE**: `ROOT = Path(__file__).resolve().parents[2]`; before/after receipt (resolved paths identical) |
| S3 | `browser/connection.py` | CDPConnection, CDP_HOST/PORT | delays | — | `navigate()` calls human_delay (runtime) |
| S4 | `browser/stealth.py` | STEALTH_SCRIPTS, inject_stealth | connection | `engagement/browser.py:301` → inject_stealth from stealth module | 61-line JS island rides along |
| S5 | `browser/visibility.py` | is_element_visible, get_visible_elements | connection | — | dead `get_visible_elements` rides along |
| S6 | `danger/detection.py` | PageType, detect_page, check_circuit_breakers | connection | — | TS patch `.py` targets for check_circuit_breakers still resolve via shim until S19 |
| S7 | `browser/readiness.py` | _wait_for_page_ready, _wait_for_linkedin_ready, _navigate_with_readiness, _safe_scroll_or_js | connection, delays, danger | `engagement/browser.py:324` (split: keep feed-state name at S12) | ⚠️ **TEST MIGRATION**: patch targets for `_wait_for_linkedin_ready` move to the reading site |
| S8 | `human/simulator.py` | HumanSimulator | connection, delays | `engagement/browser.py:301` → HumanSimulator from simulator | |
| S9 | `activity/tab_policy.py` | ACTIVITY_* constants | stdlib | — | |
| S10 | `activity/url_utils.py` | canonicalize_linkedin_profile_url, _activity_profile_slug, _activity_destination_matches, _activity_url_for_tab, _current_activity_url_is_disallowed | tab_policy | — | canonicalize is a census name |
| S11 | `activity/navigation.py` | _open_activity_tab, _open_profile_activity_from_profile, _try_open_... | connection, delays, tab_policy | `engagement/browser.py:689` (partial) | |
| S12 | `activity/feed_state.py` | _activity_scroll_snapshot, _wait_for_activity_feed_state, _activity_invalid_result, _wait_for_activity_destination, _page_still_loading | connection, danger, url_utils | `browser.py:324` + `:689` → feed-state names | ⚠️ **TEST MIGRATION**: `_wait_for_activity_feed_state` patch targets |
| S13 | `activity/readers.py` | _extract_visible_activity_entries, read_activity_tab, read_activity_tabs_detail, classify_activity_windows, relative_days_from_time_text | navigation, feed_state, url_utils, readiness, human, delays | — | **Hardest slice** (462-line function moves whole). relative_days is a census name |
| S14 | `profile/diagnostics.py` | _safe_debug_slug, _write_profile_mapper_dump | stdlib | — | ⚠️ **PATH REWRITE**: debug dir → `parents[3]`; before/after receipt |
| S15 | `profile/mapper.py` | inspect_profile_action_state, _open_more_and_find_connect, _open_more_and_click_connect, browse_profile_briefly, view_profile, PROFILE_* selectors | connection, readiness, danger, human, profile/diagnostics | — | inspect_profile_action_state is a census name |
| S16 | `profile/notif_toggle.py` | toggle_profile_notifications, check_notifications | connection, human, danger | — | toggle alive only via dead session method; rides along |
| S17 | `send/diagnostics.py` | _write_send_diagnostics | quota (dirs), stdlib | — | |
| S18 | `send/modal.py` | _inspect/_dismiss/_wait_for_connect_modal, _click_connect_button, _click_add_note_button, _type_connection_note, _click_send_button | connection, human, delays | — | patch targets for these names still resolve via shim until S19 |
| S19 | `send/engine.py` | send_connection_request, send_connection_only, verify_no_note_send_ui | modal, mapper, quota, danger, readiness, human, send/diagnostics | — | ⚠️ **TEST MIGRATION (biggest)**: `test_linkedin_no_send` patches move from `linkedin_helper.*` to `outbound.shared.send.engine.*` / `modal.*` (the reading site moved) |
| S20 | `acceptance/normalization.py`, `acceptance/sent_scraper.py` | _normalize_linkedin_profile_url, _normalize_sent_invitation_name, scrape_sent_invitations, _extract_sent_invitations_dom | connection, readiness, human | — | |
| S21 | `acceptance/subtractive.py`, `connections.py`, `notifications.py` | verify_acceptance_via_profile, check_acceptances_subtractive, check_acceptances, _extract_connections_dom, check_acceptance_notifications, _extract_acceptance_notifs_dom | mapper, sent_scraper, readiness, danger, human | — | |
| S22 | `feed/post_types.py` | detect_post_type, generate_scroll_stop_sequence, execute_feed_scroll | connection, human, delays | — | |
| S23 | `actions/withdrawal.py`, `actions/prospecting.py` | withdraw_connection, scan_prospect_box | connection, danger, human, quota | — | Graduation candidates (withdrawals / prospecting carves later) |
| S24 | `session/envelope.py` | session_warm_up, session_cool_down | connection, human, danger, feed, profile/notif_toggle, quota | — | |
| S25 | `session/preflight.py` | preflight_check | connection, stealth, danger, quota | — | `outreach_helper` try/except import moves with it |
| S26 | `session/interleave.py` | run_session, _execute_interleave | envelope, send, actions, feed, human | — | |
| S27 | `session/manager.py` | LinkedInSession | everything | `engagement/browser.py:301`, `outreach/runner.py:333,1013`, `outreach/acceptance.py:254` → `from outbound.shared.session.manager import LinkedInSession` | Moves whole. Biggest consumer rewire |
| S28 | `cli.py` | main() | session/manager + all | — | Shim gains `if __name__ == "__main__": main()` guard. **Manual receipt**: `python3 helpers/linkedin_helper.py health` from repo root |
| S29 | shim finalization | — | — | — | Shim ≈150 lines, 0 defs/classes; census verification; receipt 4 (outbound/ clean) |

**29 slices.** S1–S9 ≈ session 3; S10–S16 ≈ session 4; S17–S23 ≈ session 5; S24–S29 ≈ session 6.

---

## 3. The per-slice dance (invariant receipts)

Frozen from two carves. **Receipts are invariant; the mechanics adapt** — this carve executes via direct file edits (not the paste/sed protocol), but every receipt stays:

```
fresh map → boundary checks → extract → module gate (ruff: zero F821/I001)
→ delete from source (bottom-to-top) → re-read (no stale buffers)
→ wire imports (shim blocks armored # noqa: F401) → def-count receipt (source = 0)
→ full suite (112) → commit → push
```

Per-slice commit message: `refactor: carve <module> out of linkedin_helper (S<N>)`.
Branch: `carve/linkedin-helper`, created at S1, one commit per slice, pushed each time (CI green per push). PR opened at S29 (matching the prior carves' workflow-level PRs #5/#8), merged with the standard loop.

**The def-count receipt** is per-slice: `grep -c` of the slice's moved symbol names in `helpers/linkedin_helper.py` must reach 0.

---

## 4. The shim specification (final form)

```python
#!/usr/bin/env python3
"""helpers/linkedin_helper.py — compatibility shim.

All logic lives in outbound.shared.* ; this file preserves the historical
import surface for its consumers and stays runnable as a CLI.
"""
import sys
from pathlib import Path

_HELPERS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _HELPERS_DIR.parent
for _p in (str(_ROOT_DIR), str(_HELPERS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.shared.browser.connection import (  # noqa: F401
    CDPConnection,
)
from outbound.shared.activity.feed_state import (  # noqa: F401
    _page_still_loading, _wait_for_activity_destination, _wait_for_activity_feed_state,
)
from outbound.shared.browser.readiness import (  # noqa: F401
    _navigate_with_readiness, _wait_for_linkedin_ready,
)
from outbound.shared.browser.stealth import inject_stealth  # noqa: F401
from outbound.shared.human.delays import human_delay  # noqa: F401
from outbound.shared.human.simulator import HumanSimulator  # noqa: F401
from outbound.shared.danger.detection import check_circuit_breakers  # noqa: F401
from outbound.shared.activity.readers import (  # noqa: F401
    relative_days_from_time_text,
)
from outbound.shared.activity.url_utils import (  # noqa: F401
    canonicalize_linkedin_profile_url,
)
from outbound.shared.activity.navigation import (  # noqa: F401
    _open_profile_activity_from_profile,
)
from outbound.shared.profile.mapper import inspect_profile_action_state  # noqa: F401
from outbound.shared.send.engine import verify_no_note_send_ui  # noqa: F401
from outbound.shared.send.modal import (  # noqa: F401
    _click_connect_button, _dismiss_connect_modal, _inspect_connect_modal,
    _wait_for_connect_modal,
)
from outbound.shared.session.manager import LinkedInSession  # noqa: F401
from outbound.shared.acceptance.sent_scraper import scrape_sent_invitations  # noqa: F401
from outbound.shared.acceptance.normalization import _normalize_linkedin_profile_url  # noqa: F401
from outbound.shared.quota import increment_counter  # noqa: F401

if __name__ == "__main__":
    from outbound.shared.cli import main
    main()
```

The census is the **union of all consumers** (see §3 addendum): 14 names imported by `*.py`, 5 further patch/attr targets, and 2 names imported only by the `mac/*.command` launchers (`scrape_sent_invitations`, `_normalize_linkedin_profile_url`) — **21 distinct names**. The authoritative list is the shim file itself (it re-exports the whole moved surface, a superset of the census). `main` is not re-exported (only the `__main__` guard imports it). `# noqa: F401` armor on every block.

---

## 5. Consumer rewiring matrix (per slice)

Only **4 files / 7 sites** in `outbound/` directly import the monster; all rewire to direct imports by S27 (satisfying receipt 4):

| File:line | Names | Rewired at |
|---|---|---|
| `engagement/browser.py:301` | HumanSimulator, LinkedInSession, inject_stealth | S4, S8, S27 |
| `engagement/browser.py:324` | _wait_for_activity_feed_state, _wait_for_linkedin_ready | S7, S12 |
| `engagement/browser.py:689` | _open_profile_activity_from_profile, _wait_for_activity_destination, _wait_for_activity_feed_state | S11, S12 |
| `engagement/runner.py:265` | increment_counter | S2 |
| `outreach/runner.py:333`, `:1013` | LinkedInSession | S27 |
| `outreach/acceptance.py:254` | LinkedInSession | S27 |

**Kept on the shim intentionally** (receipt 6 — the historical surface): all `scripts/*` consumers and `helpers/linkedin_outreach_session.py`. They are not edited by this carve.

**Lazy-import caution:** the imports in `engagement/browser.py` are deliberately *inside functions* for patchability. Rewiring keeps them inside functions; only the module path changes.

---

## 6. Test migration map

Rule (fix-protocol): run the full suite at every slice; a broken patch target is the signal to migrate it **to the reading site**.

| Slice | Test(s) that must migrate | Change |
|---|---|---|
| S7 | `test_post_engagement_recovery`, `test_linkedin_no_send` | `patch("linkedin_helper._wait_for_linkedin_ready")` → `patch("outbound.shared.browser.readiness._wait_for_linkedin_ready")` |
| S12 | `test_post_engagement_recovery` | `...linkedin_helper._wait_for_activity_feed_state` → `...outbound.shared.activity.feed_state._wait_for_activity_feed_state` |
| S19 | `test_linkedin_no_send` | `patch.object(linkedin_helper, X)` → `patch.object(<owner module>, X)` for `_click_connect_button`, `_wait_for_connect_modal`, `_inspect_connect_modal`, `_dismiss_connect_modal`, `check_circuit_breakers`, `human_delay`; invocation may stay through the shim (re-exported) |

Mitigating insight: between S1 and S18 the tests keep working untouched — the function under test (`verify_no_note_send_ui`) is still in the monster and reads the monster's globals, whose names are shim re-exports (patchable). The migrations land exactly when a reading site moves.

---

## 7. Path-rewrite receipts (S2, S14)

| Slice | Old | New | Receipt |
|---|---|---|---|
| S2 | `os.path.join(dirname(__file__), "..", "state")` from helpers/ | `Path(__file__).resolve().parents[2] / "state"` from outbound/shared/quota.py | probe prints resolved STATE_DIR/DIAGNOSTIC_DIR before & after — must be identical (`<root>/state`, `<root>/state/linkedin_debug`) |
| S14 | `os.path.abspath(os.path.join(dirname(__file__), "..", "debug"))` from helpers/ | `Path(__file__).resolve().parents[3] / "debug"` from outbound/shared/profile/diagnostics.py | probe prints resolved debug dir before & after — identical (`<root>/debug`) |

Exemplar already in repo: `outbound/shared/state.py` uses `ROOT = Path(__file__).resolve().parents[2]` (spec §8 Path Resolution Standard).

---

## 8. Session batching (Phase C)

| Session | Slices | Exit criterion |
|---|---|---|
| 3 | S1–S9 | bootstrap proven; path rewrite receipt; first green push |
| 4 | S10–S16 | activity + profile clusters out; suite green |
| 5 | S17–S23 | send + acceptance + feed + actions out |
| 6 | S24–S29 | session manager moves; shim finalized; census verified; PR merged |

Each session **starts with the drift check** (§5.7): `git -C ~/codex-outreach-automation log --oneline -5` for new `drift:` commits; port any to the owning module before the first slice.

---

## 9. Success receipts (final verification)

```
1. grep -c "^def \|^class " helpers/linkedin_helper.py        → 0
2. wc -l helpers/linkedin_helper.py                           → ~150
3. ls outbound/shared/                                        → the module tree
4. grep -rn "linkedin_helper" outbound/ --include="*.py"      → nothing (arrows down only)
5. 112+ tests green on every slice boundary
6. every census name importable through the shim
   + manual: python3 helpers/linkedin_helper.py health && python3 helpers/linkedin_helper.py quotas
```

---

## 10. Risk register

| Risk | Mitigation |
|---|---|
| `from __future__ import annotations` + existing quoted annotations → ruff UP037 | Slice ruff gate catches; unquote (or keep quotes only where required) |
| isort ordering on new import blocks (ruff I001) | Gate catches; `known-first-party` already lists `outbound` |
| Editing a 6,780-line file by script | def-count receipt per slice; bottom-to-top deletes; re-read after edits |
| `# noqa: F401` armor forgotten → ruff fails on shim blocks | Every block armored from S1 (pattern set early) |
| Test patch targets silently stale | Suite at every slice; migrate at the reading site (S7/S12/S19 predicted) |
| Multi-session drift from the old system | Session-start drift check (§8) |
| Scope creep into `outreach_helper`/`sheets_helper`/`runtime_environment` | Explicitly out of scope; the monster's `outreach_helper` try/except moves with `preflight_check` only |

**Open decision for review:** whether S20–S21 (acceptance) should instead land under a future `outbound/acceptance/` workflow package rather than `outbound/shared/acceptance/`. The two-consumer rule says shared (outreach + activity-check + withdrawals all consume acceptance checking); workflows don't exist yet for the latter two. Recommendation: shared now, re-evaluate at the activity-check/withdrawals carves.

---

## Appendix — Execution Notes (2026-09-27, completed)

**Result:** `helpers/linkedin_helper.py` 6,780 → 172 lines (compatibility shim). Logic lives under
`outbound/shared/{browser,danger,human,activity,profile,send,acceptance,feed,actions,session}/`,
`quota.py`, and `cli.py`.

**Deviations from the slice table (all recorded, none structural):**

1. **Two symbols the table missed** — added mid-carve:
   - `_scroll_activity_with_lazy_patience` (moved with S13, `activity/readers.py`).
   - `like_post`, `follow_engagement_trail`, `scan_reaction_list` (new slice **S23c**, `actions/engagement.py`).
2. **Test-patch migration** also required at **S18**, not only S19: `test_connect_click_waits_for_the_real_modal_before_failing`
   invokes `_click_connect_button` directly, so its `_wait_for_connect_modal` patch had to move to
   `outbound.shared.send.modal`. S19 then migrated the `verify_no_note_send_ui` patches to `send/engine`.
3. **Shim self-bootstrap** added at S1 (as planned): inserts `ROOT` + `HELPERS_DIR` so the subprocess CLI and the
   tests' `sys.path` handling both resolve `outbound.*` and `outreach_helper`.
4. **`session/preflight.py`** carries its own `helpers/` bootstrap so the optional `outreach_helper` import survives
   direct (non-shim) imports — preserving the acceptance-rate gate's behaviour.
5. **S7 ordering** uses `TYPE_CHECKING` for the `"HumanSimulator"` forward reference (simulator lands at S8).
6. **Draft PR opened early** (instead of at S29) so CI runs on every slice push — the workflow only triggers on PRs to `main`.

**Final receipts:**

```
grep -c "^def \|^class " helpers/linkedin_helper.py   → 0
wc -l helpers/linkedin_helper.py                      → 172
census names importable through the shim              → 19/19
code references to linkedin_helper under outbound/    → 0
full suite                                            → 112 passed
CLI through the shim (python3 helpers/linkedin_helper.py quotas) → OK
```

**Note on receipt 4:** the *code* reference count is zero; the only remaining `linkedin_helper` occurrences under
`outbound/` are provenance lines in module docstrings ("Extracted verbatim from helpers/linkedin_helper.py…").
Reword them if a literal-zero grep is required.


## Appendix B — Phase C review responses (audit 2026-09-27)

An external review of the shim spec (this doc's §4) surfaced defects. Verified against the repo and resolved:

| # | Claim | Verdict | Resolution |
|---|---|---|---|
| 1 | Census missed the `mac/*.command` launchers (`scrape_sent_invitations`, `_normalize_linkedin_profile_url`); S29 gate would pass while breaking them | **Doc defect — NOT functional.** Verified: the shipped shim re-exports *all* moved symbols (a superset of any census), and all 7 names the launchers use import cleanly | §3 census addendum + §4 sample corrected; launcher verification recorded |
| 2 | §4 sample imported `_wait_for_activity_destination`/`_wait_for_activity_feed_state` from `browser.readiness` (they live in `activity/feed_state.py`) | **Doc defect — NOT functional.** Shipped shim imports both from `activity.feed_state` | §4 sample paths corrected |
| 3 | `_page_still_loading` routed to `activity/url_utils` (CDP-taking fn among pure URL helpers); should be `activity/feed_state` | **Valid cohesion point.** Code relocated to `activity/feed_state.py`; imports in `readers.py`/shim retargeted. Note: `url_utils` already contained a CDP-taking function (`_current_activity_url_is_disallowed`), so the "pure module" charter was always approximate — caller locality is the real justification | Done (`refactor: relocate _page_still_loading…`) |
| 4 | "17 subcommands" in the Phase A portrait | Valid (it is 18) | Corrected in `docs/CARVE-LINKEDIN-HELPER.md` |
| 5 | S7 migration row over-specifies `test_linkedin_no_send` for `_wait_for_linkedin_ready` | Acknowledged (harmless; the suite-driven rule self-corrected — the actual migration landed at S18) | Appendix A already records the real sequence |
| 6 | Drift claim ("exactly one drift") unverifiable externally (old repo private) | Acknowledged — local check stands | — |

**Census addendum (union of consumers):** 14 `*.py` imports + 5 distinct patch/attr targets + 2 launcher-only names = **21 distinct names**. This census gap came from scoping the Phase A scan to `--include="*.py"`; the launcher scan is now part of the census method.
