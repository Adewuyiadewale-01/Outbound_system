# Activity Check Orchestration

Use these command files from this folder.

- `Prepare.command` - prepare local state only.
- `Activity.command` - run from prepared state only.
- `Run.command` - full all-in-one run.
- `Test_Limit.command` - prompt for a limited test count.

## Recommended Flow

1. Launch the Chrome outreach profile and make sure LinkedIn is logged in.
2. Confirm a researched batch has been published; publishing creates its immutable local batch under `state/prefinal_queue/`.
3. Run `Prepare.command`.
4. Review the prepared summary if needed.
5. Run `Activity.command`.

## Notes

- `Activity.command` requires a prepared local session for today's date.
- `Run.command` prepares and runs in one pass.
- `Test_Limit.command` is best for small live smoke tests before a full run.
- The checker reads its oldest unfinished local queue batch, not the live `Pre-final` tab. `Pre-final` may therefore safely show newer research while an older batch is being processed.
- For a one-time migration of legacy rows already in `Pre-final`, run the checker with `--enqueue-current-prefinal` after reviewing that tab.
- Activity results are saved locally first. `Final` remains untouched while the activity run is in progress or blocked.
- At the end, the runner makes one sorted, batched `Pre-final -> Final` write only when both resolved profiles and fully ready lead rows meet the 90% threshold.
- After that write succeeds, it waits five minutes and runs the idempotent `Final -> Prospects` bridge. Use `--no-prospects-bridge` to stop after Final, or `--prospects-bridge-delay-sec 0` for a controlled test.
- Local state is stored under `state/activity_sessions/` and `state/activity_journal/`.
- Stop gates block unsafe runs before writing bad blank activity results.

## CDP timeout recovery

When a low-level CDP/WebSocket command times out, the checker does not move on to another lead. It pauses the current target and:

1. Checks the CDP HTTP endpoint with a fresh request.
2. If Chrome is healthy, discards the stale WebSocket, reconnects, reruns preflight, and retries the same target once.
3. If Chrome is unhealthy, saves `paused_for_browser_recovery` without consuming another target. The sequential lane coordinator waits five minutes, moves to the other profile's assigned batch, then later restarts and retries the resting profile.
4. A normal completed-lane handoff waits ten minutes. A profile that remains unhealthy after its later recovery attempt pauses the session rather than causing repeated handoffs.

Recovery events are written to the activity journal as `cdp_recovery`. A later `--activity-only` run resumes from the same prepared session and skips only targets with saved results.

## Sequential two-profile mode

Two authorized browser profiles can process one prepared session in sequence.
This mode is disabled by default in `state/activity_workers.json`.

1. Run `bash scripts/launch-chrome-2.sh` and sign into the second account manually.
2. Verify that profile with `LINKEDIN_CDP_PORT=18801 python3 helpers/linkedin_helper.py preflight`.
3. Change `state/activity_workers.json` so its top-level `enabled` value and `account_2.enabled` are both `true`.
4. The watcher then prepares once. Fewer than 30 profile checks use `account_1` only; 30 or more checks are split by complete source row (P1 and P2 stay together) between both accounts. A normal lane handoff waits 10 minutes; an unhealthy-CDP handoff waits 5 minutes.
5. A single no-browser finalizer performs the Final/Prospects bridge only after both lanes succeed.

Use `python3 scripts/run_activity_lanes.py --date YYYY-MM-DD --plan-only` to inspect a prepared split without launching either browser.
