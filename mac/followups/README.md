# Follow-up Orchestration

Manual launchers for LinkedIn follow-up automation.

## Files

- `Prepare.command` - prepare local state only.
- `Run.command` - send due follow-ups in one sequence-driven pass.
- `Day_Pass.command` - alias for `Run.command`.
- `Dry_Run.command` - inspect due leads without sending or writing Pipeline.
- `Test_Limit.command` - limited live run after safety checks.
- `Monitor.command` and `Evening_Send.command` are retired aliases; they now stop with a note instead of running the old flow.

## Source Of Truth

- Due leads come from `Pipeline`.
- Follow-up message bodies come from `Follow-up Templates`.
- Pacing comes from `Followup Sequence`.
- Audit PDFs are stored in a local, non-versioned audit directory.
- PDF filenames should match the Pipeline company name, for example `AMC Ventures Holding BV.pdf`.
- The runner creates disabled placeholder rows for `FU-1` through `FU-9` if the template tab is empty.

## Safety Rules

- The runner opens each LinkedIn thread before sending.
- It sends only when the latest detected message in the thread is from `Anthony Adewuyi`.
- It also verifies that the latest outgoing LinkedIn message matches the expected previous sequence message:
  - for `FU-1`, the expected previous message comes from `Messaging Drafts` at stage `First Message`;
  - for `FU-2` and beyond, the expected previous message comes from `state/followup_history.json`.
- If a template contains `[PDF]`, the runner treats it as an attachment token, removes it from typed text, and requires a matching audit PDF.
- PDF attachments use LinkedIn Messaging's file input matching `input[type="file"][accept*=".pdf"]`.
- If the lead sent the latest message, it marks `Pipeline -> Outcome` as `Replied` and does not send.
- If latest-message detection is uncertain, it marks `Outcome` as `Unsure` and does not send.
- If the expected previous message is missing or does not match LinkedIn, it marks `Outcome` as `Unsure` and does not send.
- In dry-run mode, it does not send messages and does not write Pipeline updates.

## Timing Logic

- `Run.command` sends due leads in one sequence-driven pass, with randomized per-lead delay, pre-send pause, after-send delay, and batch pause.
- The hard per-run cap is `20` confirmed sends.
- If the watcher sees remaining due work after a capped run, it schedules the next follow-up run `2` hours after the previous run completes.
- Presence/read-receipt data may be recorded as evidence, but it no longer gates whether a due follow-up is sent.

## Local State

- Prepared sessions: `state/followup_sessions/<date>.json`
- Journal: `state/followup_journal/<date>.jsonl`
- Confirmed sent follow-up history: `state/followup_history.json`
