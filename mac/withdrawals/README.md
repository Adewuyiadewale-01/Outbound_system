# Connection Withdrawal Orchestration

## Files

```text
Run.command - production all-in-one: backfill, prepare, then run withdrawals
Prepare.command - backfill countdown fields, generate Withdrwal Sequence, and prepare local state
Test_Limit.command - prompt for a limited prepare count
Dry_Run.command - prompt for a limited browser dry-run without confirming withdrawals
Withdraw.command - run prepared withdrawals from local state
```

## Source Tabs

Source of truth:

```text
Outreach Log
```

Missing `Contact Linkedin` values are resolved from:

```text
Prospects.P1 LinkedIn / Prospects.P2 LinkedIn
```

based on `Person Engaged`.

Sequence/control tab:

```text
Withdrwal Sequence
```

Confirmed-withdrawal output tab:

```text
Withdrawn Leads
```

## Prepare Behavior

Prepare selects rows from `Outreach Log` where:

```text
Current Progress = Conn Request / Connection Request
Days Left <= 0
Contact Linkedin is valid or resolvable from Prospects
```

It caps each run at `50` targets.

It generates batch behavior into `Withdrwal Sequence`, then reads that sequence back into:

```text
state/withdrawal_sessions/<date>.json
state/withdrawal_journal/<date>.jsonl
```

## Sequence Columns

Slot table:

```text
Slot ID
Batch #
Withdrawal Log Timing
Delay Sec
Navigation type
```

Batch table:

```text
Batch #
Batch Size
Batch Diversion
Inter Batch Delay Sec
Diversion Sec
Enabled
```

## Humanization Rules

- Live withdrawal runs have a 2-hour cooldown.
  - `Prepare.command` can still be run during cooldown.
  - `Dry_Run.command` can still be run during cooldown.
  - `Withdraw.command` blocks until the cooldown expires.
  - `Run.command` will still backfill/prepare, then block at the live withdrawal step until the cooldown expires.
- The watcher prepares up to 50 due withdrawals, executes up to 30 in the 18:00 pass,
  then can resume the same prepared session for up to 20 remaining leads at/after
  19:30.
- Activity checks wait up to 180 seconds by default, but continue as soon as activity or a terminal state is detected.
- Missing Sent Invitations rows use the profile fallback intentionally; the run blocks only after 5 consecutive profile fallbacks.
- Batch count:
  - under 25 selected targets: 2-5 batches
  - 25 or more selected targets: 5-10 batches
- Batch size:
  - randomized
  - minimum 1
  - maximum 15
- Inter-batch delay:
  - 45-120 seconds
- Per-lead delay:
  - 4-12 seconds
- Batch diversion delay:
  - 20-60 seconds
- Batch diversions:
  - `engagement_trail`
  - `profile_drill`
  - `none`
  - `feed_scroll`
  - `company_page_browse`
- Navigation type:
  - selector-based is at least 65%
  - direct URL is at most 35%

## Notes

Activity checking is for retargeting/ranking only. It does not block confirmed withdrawals.

The live LinkedIn runtime:

1. open sent invitations direct URL
2. scroll `main#workspace`
3. match row-scoped profile URLs
4. visit profile/activity
5. return to sent invitations
6. open row-scoped withdrawal modal
7. confirm withdrawal
8. update `Outreach Log`
9. upsert `Withdrawn Leads`

Sheet writes happen only after the runtime receives a confirmed withdrawal result.

Cooldown state is stored at:

```text
state/withdrawal_run_guard.json
```
