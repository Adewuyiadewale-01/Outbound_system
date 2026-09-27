# Pre-final Activity Checker

## Purpose
- Read P1/P2 LinkedIn activity for leads in the Leads employee DB `Pre-final` tab.
- Use `Operation Brute Force` -> `Activity Sequence` as the human pacing plan.
- Store local per-target activity results and bridge resolved rows into `Final`.
- Ignore P3 entirely.

## Command
```bash
python3 scripts/check_prefinal_activity.py
```

Double-click wrapper:
```bash
./mac/activity_check/Run.command
```

Prepare local state only:
```bash
./mac/activity_check/Prepare.command
```

Run activity from already-prepared local state only:
```bash
./mac/activity_check/Activity.command
```

Prompt for a limited target count:
```bash
./mac/activity_check/Test_Limit.command
```

Dry-run validation:
```bash
python3 scripts/check_prefinal_activity.py --dry-run --limit 1
```

Fixture validation without LinkedIn:
```bash
python3 scripts/check_prefinal_activity.py --dry-run --activity-fixture fixture.json
```

## Sources
- Leads employee DB -> `Pre-final`
- Leads employee DB -> `Final`
- Operation Brute Force -> `Activity Sequence`

## Activity Sequence
- Slot table columns:
  - `Slot ID`
  - `Batch #`
  - `Activity Log Timing`
  - `Delay Sec`
  - `Lead Diversion`
  - `Activity Diversion Sec`
  - `Navigation type`
- Batch table columns:
  - `Batch #`
  - `Batch Size`
  - `Enabled`

Runtime values:
- `Activity Log Timing = before_diversion`: bridge/report activity before diversion.
- `Activity Log Timing = after_diversion`: bridge/report activity after diversion.
- `Navigation type = Direct Url`: open each activity tab by direct `/recent-activity/.../` URL.
- `Navigation type = Selector-based`: open the profile, click `Show all posts`, then switch activity tabs through visible selectors/dropdowns.
- Batch gaps are generated locally between enabled batches in the `30-300` second range.
- `Navigation type` is randomized per slot when the column exists; if the column is missing, the runner defaults to `Direct Url`.
- Activity reading only uses `Posts`, `Reactions`, and `Comments`.
- For `Selector-based`, the reader clicks `Show all posts`, reads the default `Posts` view first, then checks `Reactions`, then checks `Comments`.
- For `Direct Url`, the reader starts with `Reactions`, then checks `Comments`, then checks `Posts`.
- The reader stops early only when a tab already qualifies the person as `Very active`; if the current evidence is only `Active` or weaker, it finishes probing the remaining allowed tabs.
- The reader must not intentionally open `Articles`, `Images`, `Videos`, or `Documents`.

## Rules
- Only P1 and P2 activity are checked; P3 is not read.
- Activity results are stored locally under:
  - `state/activity_sessions/<date>.json`
  - `state/activity_journal/<date>.jsonl`
- Before any LinkedIn activity read, the runner writes a prepared local session manifest containing:
  - all selected P1/P2 targets
  - each target's matched `Activity Sequence` runtime-plan item
  - the sequence/batch summary
- `--activity-only` requires an existing prepared local session and does not regenerate `Activity Sequence`.
- Completed targets in the local session state are skipped on resume.
- `Final` is upserted by lead ID and sorted after each resolved lead row.
- The runner does not write `Pre-final`, `Prospects`, or `Outreach Log`.

## Stop Gates
- Stop before target reads if Chrome/LinkedIn preflight cannot connect safely.
- Stop before writing a target result if LinkedIn reports login, captcha, restriction, email verification, robot check, checkpoint, or another danger state.
- Stop if the generated runtime plan does not serially match the selected targets.
- Stop after repeated hard activity-read failures such as timeouts, navigation failures, page-not-ready failures, or extraction failures. Default threshold: `3`.
- Do not convert danger/preflight blockers into blank activity results.

## Ranking
- `Very active = 2`, `Active = 1`, `Not active` or blank = `0`.
- P2 swaps into P1 only when P2 has a higher score.
- If neither P1 nor P2 has activity, `Category` is blank.
- With two LinkedIn profiles:
  - any `Very active` -> `Hyper`
  - otherwise any `Active` -> `High`
  - otherwise -> `Low`
- With one LinkedIn profile:
  - any `Very active` -> `Alpha-medium`
  - otherwise any `Active` -> `Medium`
  - otherwise -> `Low`
