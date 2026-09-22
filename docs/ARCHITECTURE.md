````markdown
# Architecture

> The contract for restructuring this repository. Every PR during the
> rebuild must follow these rules. Written before any file was moved.

## 1. The Layer Rule (most important)

Dependencies point DOWN only. Never up, never sideways.

```
scripts/            thin entry points (you run these)
   ↓ imports
outbound/           workflow logic (you import these)
   ↓ imports
outbound/shared/    common utilities (imported by everyone)
```

| Layer | May use | Never uses |
|---|---|---|
| `scripts/` | `outbound/` | other scripts' internals |
| `outbound/<workflow>` | `outbound/shared/` | other workflows' internals |
| `outbound/shared/` | stdlib + third-party only | any workflow |
| `orchestrator/` | `outbound/`, runs `scripts/` | frontend internals |
| `frontend/` | nobody (static assets) | everything |

This rule exists because the old `helpers/` folder allowed anything to
import anything — which is how a 5,600-line "helper" file happened.

## 2. Run vs. Import

- `scripts/` — things you **execute**. Thin: parse args, call `outbound/`,
  print result. Target ~50 lines each.
- `outbound/` — things you **import**. Never run directly.

Quick test: if a file has `if __name__ == "__main__":`, it belongs in `scripts/`.

## 3. Config vs. State

| Folder | Committed | Contents | The question |
|---|---|---|---|
| `config/` | ✅ | worker configs, rules | "Would a fresh clone need this to work?" |
| `state/` | ❌ gitignored | sessions, campaign state | "Did the system produce this while running?" |

Lesson earned: three config files once hid in `state/` and silently broke
fresh clones — including the lane bug that paused withdrawals in production.

## 4. Target Structure

```
outbound/                  THE Python package (all importable logic)
  shared/                    sheets, environment, browser sessions
  engagement/                post engagement workflow
  outreach/                  OBF session workflow (the "monster")
  activity/                  activity check workflow
  leads/                     review → research → archive pipeline
  withdrawals/               withdrawal workflow
orchestrator/              scheduling & monitoring backend
  watcher/                   from ORCHESTRATION/watcher/
  server/                    local_server.py
frontend/                  Electron app + served pages (from monitor_app/)
scripts/                   thin CLI entry points only
  migrations/                backfill_*, swap_*, migrate_*
mac/                       .command launchers + chrome scripts
apps_script/               from scripts/mobile_lead_app/
config/  state/  tests/  docs/

1.  — Package layout invariant. outbound/'s top level contains exactly two kinds of entry: workflow packages (engagement/, outreach/, ...) and shared/. No loose modules at the package root. New workflows arrive as subfolders; code graduates into shared/ only when two or more workflows consume it — one consumer means it stays in its workflow, because premature sharing is how junk drawers are born.
```

## 5. Migration Rules

1. **Strangler, not big-bang.** Legacy `helpers/` files stay until their
   last consumer migrates; deleted in that workflow's PR.
2. **Moves and edits never share a commit.** `git mv` verbatim; behavior
   changes get their own commits.
3. **Entry points keep their paths.** The watcher calls scripts by path —
   a moved entry point keeps its path or the watcher updates in the same PR.
4. **Tests migrate with their workflow.**
5. **Every PR is independently green.** ruff + full test suite + CI before merge.
6.  **Launcher ownership:** each workflow PR owns every launcher that invokes its workflow. Moving or splitting a workflow means updating its launchers in the same PR — invocation target, and any flags — with each launcher syntax-verified (bash -n) and behavior-verified before that workflow's PR merges.

## 6. Extraction Order

| # | Workflow | Source | Why this position |
|---|---|---|---|
| 0 | Prelude (mechanical) | root cleanup, workspace removal | unblocks clean context |
| 1 | Post engagement | `scripts/post_engagement.py` | 26 tests, self-contained — learn the pattern here |
| 2 | Outreach/OBF | `helpers/linkedin_outreach_session.py` | the monster; core of the system |
| 3 | Activity check | `scripts/check_prefinal_activity.py` | second-largest monolith |
| 4 | Leads pipeline | `lead_review_lifecycle.py` + friends | strong test suite |
| 5 | Withdrawals | `withdraw_connections.py` + friends | un-paused at cutover |
| 6 | Watcher | `ORCHESTRATION/*` | rename happens here; everything else settled |
| 7 | Apps Script | `mobile_lead_app/` | documentation pass, low risk |

## 7. Decision Log

| Date | Decision | Reason |
|---|---|---|
| 2026-09-21 | Frontend at top level | distinct concern from its server |
| 2026-09-21 | Both `package.json`s kept | root = Python test glue; monitor_app = the Electron app |
| 2026-09-21 | Root `dashboard.html` deleted in prelude | stale orphan — server only serves monitor_app's copy |
| 2026-09-21 | `workspace/` docs parked in old repo | fresh docs written per workflow as carved; old repo archives them |
| 2026-09-21 | `ORCHESTRATION` renamed in workflow #6 | keeps rename risk inside one PR |
````

## 8. Path Resolution Standard

All path resolution follows one pattern, anchored to the repo root:

Python: ROOT = Path(__file__).resolve().parents[N] where N is theactual depth from root — verified per file, never assumed.
Shell/.command: REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)" thencd "$REPO_ROOT" before any relative reference.
No absolute paths outside .env. No paths relative to the currentworking directory. Entry points may be invoked from anywhere.
Rationale: the codebase historically mixed four path idioms; three ofthem broke silently during the first restructure pass.

---