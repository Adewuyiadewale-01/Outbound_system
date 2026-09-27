# Orchestration Watcher

Short-lived watcher for local automation checkpoints.

The watcher coordinates the `obf`, `followups`, `withdrawals`, and `activity` workflows.
It is meant to be woken by `launchd` every 5 minutes. Each tick runs at most one
due checkpoint and exits.

## Scheduling Policy

- OBF preparation and execution default to `08:25` and `08:30`. OBF autonomy
  is disabled by default and only runs when enabled in
  `state/obf_orchestration_config.json`.
- OBF will not backfill after its `10:30` operating-window cutoff, and execution
  will not start without a ready dated prepared state.
- Follow-ups begin at `11:40`, withdrawals at `18:00`, and activity checks at `21:00`.
- Follow-ups are strictly disabled on Sundays, including automatic continuations.
- A run is pinned to its dated local session. A continuation can safely run after
  midnight without accidentally loading a new day's session.
- A workflow may schedule exactly one workload continuation: initial run plus one
  continuation. Infrastructure failures are handled separately with up to three
  retries after 5, 15, and 30 minutes, always against the same dated session.
- Withdrawals prepare up to 50 due leads, run up to 30 at 18:00, then may run
  up to 20 remaining prepared leads at/after 19:30.
- A withdrawal continuation is permitted only after a clean first pass left
  prepared leads unprocessed.
- Browser/CDP, timeout, and interruption failures become `retry_waiting` or
  `resume_pending`. Authentication and configuration failures become
  `needs_attention` immediately; exhausted retries do the same.
- Long workflows receive a runtime budget derived from their prepared workload
  (bounded between two and four hours) and publish a runtime heartbeat.
- The watcher keeps the Mac awake from 08:15 through 23:30 once the first tick in
  that window runs. Every live command is also wrapped in `caffeinate`.
- Required CDP accounts are preflighted per workflow. Dual-lane workflows verify
  both configured workers before execution.

## Files

- `orchestration_watcher.py` - watcher tick implementation.
- `Watcher.command` - manual one-tick launcher.
- `Watcher_Status.command` - manual status check.
- `com.outreachautomation.orchestration-watcher.plist.template` - portable launchd template.

## State

- Watcher state: `state/orchestration_watcher.json`
- OBF scheduler settings: `state/obf_orchestration_config.json`
- OBF prepared state: `state/outreach_sequences/<date>-prepared.json`
- OBF execution journal: `state/outreach_journal/<date>.jsonl`
- Watcher lock: `state/orchestration_watcher.lock`
- Follow-up day state: `state/followup_sessions/<date>.json`
- Withdrawal day state: `state/withdrawal_sessions/<date>.json`

## Install With launchd

```bash
PROJECT_ROOT="$(pwd)"
PLIST=~/Library/LaunchAgents/com.outreachautomation.orchestration-watcher.plist
mkdir -p ~/Library/LaunchAgents
sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" orchestrator/watcher/com.outreachautomation.orchestration-watcher.plist.template > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
```

## Disable

```bash
launchctl unload ~/Library/LaunchAgents/com.outreachautomation.orchestration-watcher.plist
```
