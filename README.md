<img width="100" height="20" alt="image" src="https://github.com/user-attachments/assets/04165a86-955a-4545-8901-bc38dd950cb9" />

# Outreach Automation

This repository contains a local-first automation toolkit for moving qualified leads through research, review, outreach, follow-up, and reporting workflows. It coordinates Google Sheets-backed queues with controlled browser automation and a local Electron dashboard.

## Before you run it

1. Create and activate a Python 3.10+ virtual environment.
2. Install runtime dependencies: `python3 -m pip install -r requirements.txt`.
3. Install the root JavaScript dependencies: `npm ci`.
4. Copy `.env.example` to `.env`, then set the required sheet URLs and local credential path. Core Python workflows load the root `.env` automatically; explicit process environment values take precedence.
5. Keep service-account JSON, browser profiles, generated state, lead exports, and workflow outputs outside version control. The root `.gitignore` protects these paths for a new repository.

## Daily Job Discovery dashboard

The Operations Control Center includes a **Job Discovery** page for the companion daily-job-discovery service. It reads the service's local SQLite-backed status, recent run record, scheduler state, and runtime controls; it can start manual discovery and re-verification runs, enable/disable daily execution, and start or stop its local scheduler. By default it locates the service at `~/Documents/Automation Journey/daily-job-discovery`; set `DAILY_JOB_DISCOVERY_ROOT` in `.env` to use a different directory.

## Post Engagement workflow

The **Post Engagement** dashboard page accepts one or more recent LinkedIn source-post URLs, extracts the accessible reactor pool, audits recent profile activity, and maintains a resumable daily campaign under `state/post_engagement/`. **Run audit** is read-only; **Launch workflow** is the explicit live-action path for Likes, follows, and connection requests. The selected Design or Automation CDP browser must already be running and signed in. The workflow always creates its own tab and does not replace an OBF tab.

The command-line equivalent is `python3 scripts/post_engagement.py status`. Add a source with `add-source URL`; run an audit with `run`; add `--execute` only for a live campaign.

## Verification

Run the Python suite with:

```bash
npm test
```

Run static checks after installing development dependencies with:

```bash
python3 -m pip install -r requirements-dev.txt
ruff check .
```

## Configuration and safe publishing

The code expects operational URLs and secrets through environment variables or Google Apps Script properties; no active account, sheet, browser profile, or webhook credential belongs in a public repository. See [docs/PUBLISHING.md](docs/PUBLISHING.md) for the pre-push checklist and [scripts/mobile_lead_app/SCRIPT_PROPERTIES.example.md](scripts/mobile_lead_app/SCRIPT_PROPERTIES.example.md) for the Apps Script configuration.

The local dashboard is designed to bind to loopback only. Do not expose it through a public interface without adding authentication and replacing the Electron renderer's legacy Node integration model.
