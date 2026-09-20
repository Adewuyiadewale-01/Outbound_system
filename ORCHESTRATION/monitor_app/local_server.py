#!/usr/bin/env python3
"""Serve the local dashboard with a small Lead Prep API for browser testing."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parents[1]
STATE_DIR = PROJECT_DIR / "state"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
HELPERS_DIR = PROJECT_DIR / "helpers"
JOB_DISCOVERY_DIR = Path(
    os.environ.get(
        "DAILY_JOB_DISCOVERY_ROOT",
        str(Path.home() / "Documents" / "Automation Journey" / "daily-job-discovery"),
    )
).expanduser()
JOB_DISCOVERY_CONFIG_PATH = JOB_DISCOVERY_DIR / "config" / "runtime.json"
JOB_DISCOVERY_LOCK_PATH = JOB_DISCOVERY_DIR / "data" / "state.sqlite.lock"
JOB_DISCOVERY_SCHEDULER_LABEL = "com.fulltime-job.daily-job-discovery"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(HELPERS_DIR))

from post_engagement import (
    add_source as add_post_engagement_source,
)
from post_engagement import (
    archive_and_start_fresh as archive_post_engagement_campaign,
)
from post_engagement import (
    dashboard as read_post_engagement_dashboard,
)
from post_engagement import (
    pause_campaign as pause_post_engagement_campaign,
)
from post_engagement import (
    resume_campaign as resume_post_engagement_campaign,
)
from post_engagement import (
    save_config as save_post_engagement_config,
)
from runtime_environment import load_repo_env

load_repo_env()

# Runtime environment loading also reads the root .env, so resolve this optional
# integration path again after it has been applied.
JOB_DISCOVERY_DIR = Path(
    os.environ.get("DAILY_JOB_DISCOVERY_ROOT", str(JOB_DISCOVERY_DIR))
).expanduser()
JOB_DISCOVERY_CONFIG_PATH = JOB_DISCOVERY_DIR / "config" / "runtime.json"
JOB_DISCOVERY_LOCK_PATH = JOB_DISCOVERY_DIR / "data" / "state.sqlite.lock"

LEAD_PREP_CONFIG_PATH = STATE_DIR / "lead_prep_orchestration_config.json"
LEAD_PREP_ARCHIVE_PATH = STATE_DIR / "lead_exec_research" / "research_archive" / "index.json"
LEAD_PREP_RUNS_DIR = STATE_DIR / "lead_exec_research" / "runs"
LEAD_PREP_COMPUTATIONS_DIR = STATE_DIR / "lead_exec_research" / "computations"
LEAD_REVIEW_CACHE_PATH = STATE_DIR / "lead_exec_research" / "dashboard_cache.json"
LEAD_RESEARCH_PROGRESS_PATH = STATE_DIR / "lead_exec_research" / "manual_research_progress.json"
OBF_SOURCE_CONFIG_PATH = STATE_DIR / "obf_source.json"
OBF_CONFIG_PATH = STATE_DIR / "obf_orchestration_config.json"
OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")
OBF_SOURCE_DEFAULTS = {"prospects_tab": "Prospects", "test_mode": False}
OBF_CONFIG_DEFAULTS = {
    "enabled": False,
    "prep_time": "08:25",
    "exec_time": "08:30",
    "max_sends": 30,
    "daily_volume": 30,
    "prospects_start_row": None,
    "timezone": "Africa/Lagos",
}
OBF_SOURCE_SNAPSHOT_PATH = STATE_DIR / "dashboard_cache" / "obf_source_snapshot.json"
OBF_SOURCE_REFRESH_SECONDS = 30 * 60
OBF_RUN_STATE_PATH = STATE_DIR / "dashboard_cache" / "obf_dashboard_run.json"
OBF_RUN_LOG_PATH = STATE_DIR / "dashboard_cache" / "obf_dashboard_run.log"
OBF_PREPARED_DIR = STATE_DIR / "outreach_sequences"
OBF_JOURNAL_DIR = STATE_DIR / "outreach_journal"
FOLLOWUP_CONFIG_PATH = STATE_DIR / "followup_orchestration_config.json"
WITHDRAWAL_SESSIONS_DIR = STATE_DIR / "withdrawal_sessions"
WITHDRAWAL_JOURNAL_DIR = STATE_DIR / "withdrawal_journal"
WITHDRAWAL_RUN_GUARD_PATH = STATE_DIR / "withdrawal_run_guard.json"
WITHDRAWAL_DASHBOARD_RUN_STATE_PATH = (
    STATE_DIR / "dashboard_cache" / "withdrawal_dashboard_run.json"
)
WITHDRAWAL_DASHBOARD_RUN_LOG_PATH = STATE_DIR / "dashboard_cache" / "withdrawal_dashboard_run.log"
ACTIVITY_SESSIONS_DIR = STATE_DIR / "activity_sessions"
ACTIVITY_HISTORY_CACHE: dict[str, Any] = {"signature": (), "rows": []}
WATCHER_STATE_PATH = STATE_DIR / "orchestration_watcher.json"
WATCHER_SCRIPT_PATH = PROJECT_DIR / "ORCHESTRATION" / "watcher" / "orchestration_watcher.py"
WATCHER_LABEL = "com.outreachautomation.orchestration-watcher"
WATCHER_PLIST_SOURCE = PROJECT_DIR / "ORCHESTRATION" / "watcher" / f"{WATCHER_LABEL}.plist.template"
WATCHER_PLIST_TARGET = Path.home() / "Library" / "LaunchAgents" / f"{WATCHER_LABEL}.plist"
PROCESS_APPROVED_FIRST_AUTOMATION_PATH = (
    Path.home() / ".codex" / "automations" / "process-approved-leads" / "automation.toml"
)
PROCESS_APPROVED_FALLBACK_AUTOMATION_PATH = (
    Path.home() / ".codex" / "automations" / "process-approved-leads-23-00" / "automation.toml"
)
LOCAL_TZ = ZoneInfo("Africa/Lagos")
OBF_RUN_LOCK = threading.Lock()
OBF_ACTIVE_PROCESS: subprocess.Popen | None = None
WITHDRAWAL_RUN_LOCK = threading.Lock()
WITHDRAWAL_ACTIVE_PROCESS: subprocess.Popen | None = None
POST_ENGAGEMENT_ACTIVE_PROCESS: subprocess.Popen | None = None

LEAD_PREP_CONFIG_DEFAULTS = {
    "autonomous_prep_enabled": False,
    "prep_time": "14:00",
    "first_review_deadline": "18:00",
    "fallback_review_deadline": "22:00",
    "base_volume": 60,
    "processing_batch_size": 30,
    "approval_gate_enabled": False,
    "overlap_scan_mode": "auto",
    "fresh_volume_top_up_mode": "auto",
    "dashboard_date_override": "",
}


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def job_discovery_available() -> bool:
    return (
        (JOB_DISCOVERY_DIR / "src" / "cli.mjs").is_file()
        and JOB_DISCOVERY_CONFIG_PATH.is_file()
        and (JOB_DISCOVERY_DIR / "package.json").is_file()
    )


def job_discovery_scheduler_enabled() -> bool:
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{JOB_DISCOVERY_SCHEDULER_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def run_job_discovery_node(*args: str, timeout: int = 30) -> dict[str, Any]:
    if not job_discovery_available():
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Daily Job Discovery was not found at {JOB_DISCOVERY_DIR}.",
        }
    try:
        completed = subprocess.run(
            ["node", *args],
            cwd=JOB_DISCOVERY_DIR,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "code": completed.returncode,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "stdout": "", "stderr": str(error), "code": -1}


def read_job_discovery_dashboard() -> dict[str, Any]:
    config = read_json(JOB_DISCOVERY_CONFIG_PATH, {})
    lock = read_json(JOB_DISCOVERY_LOCK_PATH, None)
    if not job_discovery_available():
        return {
            "available": False,
            "root": str(JOB_DISCOVERY_DIR),
            "error": f"Daily Job Discovery was not found at {JOB_DISCOVERY_DIR}. Set DAILY_JOB_DISCOVERY_ROOT to use a different location.",
            "config": config,
            "scheduler_enabled": False,
            "is_running": False,
            "status": {},
        }
    result = run_job_discovery_node("src/cli.mjs", "status")
    try:
        status = json.loads(result["stdout"] or "{}")
    except json.JSONDecodeError:
        status = {}
    error = (
        ""
        if result["ok"]
        else (result["stderr"] or result["stdout"] or "Status command failed.").strip()
    )
    return {
        "available": True,
        "root": str(JOB_DISCOVERY_DIR),
        "config": config,
        "scheduler_enabled": job_discovery_scheduler_enabled(),
        "is_running": bool(status.get("activeRunId") or (lock or {}).get("runId")),
        "active_action": "scheduled run" if status.get("activeRunId") else "",
        "lock": lock,
        "status": status,
        "error": error,
        "last_updated": local_now().isoformat(),
    }


def save_job_discovery_settings(requested: dict[str, Any]) -> dict[str, Any]:
    if not job_discovery_available():
        return {"ok": False, "error": f"Daily Job Discovery was not found at {JOB_DISCOVERY_DIR}."}
    current = read_json(JOB_DISCOVERY_CONFIG_PATH, {})
    daily_run_time = str(requested.get("dailyRunTime") or current.get("dailyRunTime") or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_run_time):
        return {"ok": False, "error": "Daily run time must use 24-hour HH:MM format."}

    def bounded(value: Any, fallback: Any, maximum: int) -> int:
        try:
            return max(1, min(maximum, int(value)))
        except (TypeError, ValueError):
            return max(1, min(maximum, int(fallback or 1)))

    next_config = {
        **current,
        "automationEnabled": bool(requested.get("automationEnabled")),
        "dailyRunTime": daily_run_time,
        "maxQueriesPerRun": bounded(
            requested.get("maxQueriesPerRun"), current.get("maxQueriesPerRun", 180), 500
        ),
        "maxListingsPerRun": bounded(
            requested.get("maxListingsPerRun"), current.get("maxListingsPerRun", 180), 1000
        ),
    }
    write_json(JOB_DISCOVERY_CONFIG_PATH, next_config)
    return {"ok": True, "config": next_config}


def toggle_job_discovery_scheduler(requested: dict[str, Any]) -> dict[str, Any]:
    if not job_discovery_available():
        return {"ok": False, "error": f"Daily Job Discovery was not found at {JOB_DISCOVERY_DIR}."}
    script = (
        "scripts/install-launchd.mjs"
        if bool(requested.get("enabled"))
        else "scripts/uninstall-launchd.mjs"
    )
    result = run_job_discovery_node(script)
    if not result["ok"]:
        return {
            "ok": False,
            "error": (
                result["stderr"] or result["stdout"] or "Unable to change scheduler service."
            ).strip(),
        }
    return {"ok": True, "enabled": job_discovery_scheduler_enabled()}


def start_job_discovery_action(action: str) -> dict[str, Any]:
    if action not in {"run", "reverify"}:
        raise ValueError("Unsupported Daily Job Discovery action.")
    if not job_discovery_available():
        return {"ok": False, "error": f"Daily Job Discovery was not found at {JOB_DISCOVERY_DIR}."}
    if read_json(JOB_DISCOVERY_LOCK_PATH, None):
        return {"ok": False, "error": "A Daily Job Discovery run is already active."}
    subprocess.Popen(
        ["node", "src/cli.mjs", action],
        cwd=JOB_DISCOVERY_DIR,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "action": action}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_lead_prep_config() -> dict[str, Any]:
    stored = read_json(LEAD_PREP_CONFIG_PATH, {})
    if not stored.get("overlap_scan_mode") and stored.get("reconciliation_mode"):
        stored["overlap_scan_mode"] = stored["reconciliation_mode"]
    if stored.get("review_deadline") and not stored.get("fallback_review_deadline"):
        stored["fallback_review_deadline"] = stored["review_deadline"]
    return {**LEAD_PREP_CONFIG_DEFAULTS, **stored}


def read_obf_source_config() -> dict[str, Any]:
    config = {**OBF_SOURCE_DEFAULTS, **read_json(OBF_SOURCE_CONFIG_PATH, {})}
    tab = str(config.get("prospects_tab") or "").strip()
    if not tab:
        raise ValueError("OBF source must specify a prospects tab.")
    config["prospects_tab"] = tab
    config["test_mode"] = bool(config.get("test_mode"))
    return config


def read_obf_config() -> dict[str, Any]:
    return {**OBF_CONFIG_DEFAULTS, **read_json(OBF_CONFIG_PATH, {})}


def obf_credentials_path() -> str:
    configured = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "").strip()
    if configured:
        return configured
    native = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
    if native.exists():
        return str(native)
    return str(PROJECT_DIR / ".openclaw" / "credentials" / "google-sheets.json")


def refresh_obf_source_snapshot() -> dict[str, Any]:
    """The only OBF path that reads Sheets; dashboard reads its saved snapshot."""
    source = read_obf_source_config()
    from sheets_helper import read_tab

    data = read_tab(obf_credentials_path(), OBF_SHEET_URL, source["prospects_tab"])
    from linkedin_outreach_session import _read_outreach_control

    approval, control_row, target, remaining = _read_outreach_control(
        obf_credentials_path(), OBF_SHEET_URL, local_now().date().isoformat()
    )
    snapshot = {
        "source": source,
        "refreshed_at": local_now().isoformat(),
        "data": data,
        "outreach_control": {
            "approval": approval,
            "target_total": target,
            "target_remaining": remaining,
            "row": control_row,
        },
    }
    write_json(OBF_SOURCE_SNAPSHOT_PATH, snapshot)
    return snapshot


def refresh_obf_source_loop() -> None:
    while True:
        try:
            refresh_obf_source_snapshot()
        except Exception:
            pass
        time.sleep(OBF_SOURCE_REFRESH_SECONDS)


def read_json_lines(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def obf_row_status(events: list) -> tuple[str, str]:
    statuses = [str(event.get("status") or "").lower() for event in events]
    types = [str(event.get("event_type") or "") for event in events]
    if "pending_sync" in statuses:
        return "Sync Pending", "warning"
    if "connection_request_confirmed" in types or "sent" in statuses:
        return "Conn Sent", "success"
    if "prospect_requires_email" in types:
        return "Requires Email", "warning"
    if any(event.get("event_type") == "prospect_reconciliation" for event in events):
        return "Reconciled", "success"
    if any(
        event.get("error") or any(token in status for token in ("fail", "blocked", "error"))
        for event, status in zip(events, statuses)
    ):
        return "Failed", "danger"
    if "skipped_timeout" in statuses:
        return "Timed Out", "warning"
    if events:
        return "In Progress", "active"
    return "Prepared", "neutral"


def summarize_obf_day(day: str, prepared: dict[str, Any], events: list) -> dict[str, Any]:
    grouped: dict[str, list] = {}
    for event in events:
        key = str(event.get("prospect_id") or "")
        if key:
            grouped.setdefault(key, []).append(event)
    rows = []
    runtime_plan = prepared.get("runtime_plan") or []
    for index, prospect in enumerate(prepared.get("queue") or []):
        prospect_events = grouped.get(str(prospect.get("id") or ""), [])
        runtime = runtime_plan[index] if index < len(runtime_plan) else {}
        progress, tone = obf_row_status(prospect_events)
        latest = prospect_events[-1] if prospect_events else {}
        retries = sum(
            1
            for event in prospect_events
            if any(
                token
                in f"{event.get('event_type', '')} {event.get('status', '')} {event.get('stage', '')}".lower()
                for token in ("retry", "recover", "timeout")
            )
        )
        rows.append(
            {
                "index": index + 1,
                "id": prospect.get("id") or "",
                "company": prospect.get("company") or "Unknown company",
                "contact_name": prospect.get("contact_name") or "Unknown contact",
                "contact_title": prospect.get("contact_title") or "",
                "contact_linkedin": prospect.get("contact_linkedin") or "",
                "prospect_row": prospect.get("_row_number") or "",
                "primary_lane": prospect.get("primary_lane") or "Unassigned",
                "worker_id": prospect.get("worker_id") or "Unassigned",
                "slot_id": runtime.get("slot_id") or index + 1,
                "activity_timing": runtime.get("activity_log_timing") or "",
                "delay_sec": runtime.get("delay_sec"),
                "diversion": runtime.get("lead_diversion") or "none",
                "progress": progress,
                "tone": tone,
                "retry_count": retries,
                "latest_stage": latest.get("stage") or latest.get("event_type") or "",
                "last_update": latest.get("recorded_at") or "",
                "detail": latest.get("error")
                or latest.get("reason")
                or latest.get("live_state")
                or "",
            }
        )
    sent = sum(row["progress"] == "Conn Sent" for row in rows)
    reconciled = sum(row["progress"] == "Reconciled" for row in rows)
    requires_email = sum(row["progress"] == "Requires Email" for row in rows)
    failed = sum(row["progress"] == "Failed" for row in rows)
    pending_sync = sum(row["progress"] == "Sync Pending" for row in rows)
    timed_out = sum(row["progress"] == "Timed Out" for row in rows)
    return {
        "date": day,
        "rows": rows,
        "planned": int(
            prepared.get("planned_count") or prepared.get("target_remaining") or len(rows)
        ),
        "target": int(prepared.get("target_total") or 0),
        "sent": sent,
        "reconciled": reconciled,
        "requires_email": requires_email,
        "failed": failed,
        "pending_sync": pending_sync,
        "timed_out": timed_out,
        "resolved": sent + reconciled + requires_email + failed + pending_sync + timed_out,
        "retries": sum(row["retry_count"] for row in rows),
        "start_time": events[0].get("recorded_at") if events else None,
        "last_event_time": events[-1].get("recorded_at") if events else None,
    }


def obf_issue_rows(events: list) -> list:
    issues = []
    for event in events:
        status = str(event.get("status") or "").lower()
        if not (
            event.get("error")
            or status in {"pending_sync", "skipped_timeout"}
            or any(token in status for token in ("fail", "blocked", "retry"))
        ):
            continue
        issues.append(
            {
                "time": event.get("recorded_at") or "",
                "severity": "danger"
                if event.get("error") or any(token in status for token in ("fail", "blocked"))
                else "warning",
                "title": event.get("company") or event.get("event_type") or "Workflow issue",
                "detail": event.get("error") or event.get("reason") or event.get("stage") or status,
                "prospect_id": event.get("prospect_id") or "",
            }
        )
    return list(reversed(issues[-20:]))


def obf_history() -> list:
    """Summarize retained frozen states and journals for the recent-runs panel."""
    records = []
    for path in sorted(OBF_PREPARED_DIR.glob("*-prepared.json"), reverse=True)[:14]:
        day = path.name[:10]
        prepared = read_json(path, {})
        events = read_json_lines(OBF_JOURNAL_DIR / f"{day}.jsonl")
        summary = summarize_obf_day(day, prepared, events)
        issue_labels: dict[str, int] = {}
        for event in events:
            status = str(event.get("status") or "").lower()
            if not (
                event.get("error")
                or event.get("reason")
                or status in {"pending_sync", "skipped_timeout"}
                or any(token in status for token in ("fail", "blocked", "retry"))
            ):
                continue
            # Prefer the explicit error/reason, but preserve the timeout state
            # instead of reducing it to the generic activity-sync stage.
            detail = (
                event.get("error")
                or event.get("reason")
                or status
                or event.get("stage")
                or "workflow issue"
            )
            label = str(detail).replace("_", " ").strip().title()
            issue_labels[label] = issue_labels.get(label, 0) + 1
        issue_summary = (
            " · ".join(
                f"{label} ({count})"
                for label, count in sorted(
                    issue_labels.items(), key=lambda item: (-item[1], item[0])
                )[:3]
            )
            or "—"
        )
        planned = summary["planned"]
        records.append(
            {
                "date": day,
                "prepared": planned,
                "sent": summary["sent"],
                "reconciled": summary["reconciled"],
                "issues": summary["failed"] + summary["pending_sync"] + summary["timed_out"],
                "issue_summary": issue_summary,
                "completion": round((summary["resolved"] / planned) * 100) if planned else 0,
                "started_at": summary["start_time"],
                "completed_at": summary["last_event_time"],
            }
        )
    return records


def source_availability(data: dict[str, Any]) -> tuple[list, dict[str, int]]:
    """Describe the live source without mixing it into the frozen daily queue."""
    rows = []
    for index, row in enumerate(data.get("rows") or [], start=1):
        engaged = str(row.get("Engaged Person") or "Person 1").strip()
        prefix = "P2" if engaged == "Person 2" else "P1"
        linkedin = str(row.get(f"{prefix} LinkedIn") or "").strip()
        status = str(row.get("Outreach Status") or "").strip()
        lane = str(row.get("Primary Lane") or "").strip()
        fresh = not status
        available = fresh and bool(linkedin)
        assigned = lane.lower() in {"design", "automation"}
        rows.append(
            {
                "index": index,
                "id": str(row.get("ID") or "").strip(),
                "company": str(row.get("Company") or "").strip(),
                "contact_name": str(row.get(f"{prefix} Name") or "").strip(),
                "contact_title": str(row.get(f"{prefix} Title") or "").strip(),
                "primary_lane": lane,
                "outreach_status": status,
                "fresh": fresh,
                "available": available,
                "lane_assigned": assigned,
                "source_state": "Available"
                if available and assigned
                else (
                    "Needs lane assignment" if available else (status or "Missing LinkedIn profile")
                ),
            }
        )
    summary = {
        "total": len(rows),
        "fresh": sum(row["fresh"] for row in rows),
        "available": sum(row["available"] for row in rows),
        "needs_lane_assignment": sum(row["available"] and not row["lane_assigned"] for row in rows),
    }
    return rows, summary


def obf_run_state() -> dict[str, Any]:
    with OBF_RUN_LOCK:
        global OBF_ACTIVE_PROCESS
        if OBF_ACTIVE_PROCESS and OBF_ACTIVE_PROCESS.poll() is not None:
            OBF_ACTIVE_PROCESS = None
        return read_json(OBF_RUN_STATE_PATH, {})


def read_obf_dashboard() -> dict[str, Any]:
    """Read frozen OBF state and its local execution journal; never read Sheets."""
    source = read_obf_source_config()
    day = local_now().date().isoformat()
    prepared_path = OBF_PREPARED_DIR / f"{day}-prepared.json"
    journal_path = OBF_JOURNAL_DIR / f"{day}.jsonl"
    prepared = read_json(prepared_path, {})
    events = read_json_lines(journal_path)
    run_state = obf_run_state()
    is_running = run_state.get("status") == "running"
    active_action = str(run_state.get("action") or "") if is_running else ""
    snapshot = read_json(OBF_SOURCE_SNAPSHOT_PATH, {})
    snapshot_source = snapshot.get("source") or {}
    snapshot_matches = (
        snapshot_source.get("prospects_tab") == source["prospects_tab"]
        and bool(snapshot_source.get("test_mode")) == source["test_mode"]
    )
    data = snapshot.get("data") if snapshot_matches else {"rows": []}
    refreshed_at = snapshot.get("refreshed_at") if snapshot_matches else None
    source_rows, source_summary = source_availability(data)
    cached_control = snapshot.get("outreach_control") or {}
    if prepared.get("ready"):
        summary = summarize_obf_day(day, prepared, events)
        phase = (
            "completed"
            if summary["planned"] and summary["resolved"] >= summary["planned"]
            else ("executing" if events else "prepared")
        )
        if is_running:
            phase = "preparing" if active_action == "prepare" else "executing"
        return {
            "today": day,
            "preview": False,
            "source_mode": "test" if source["test_mode"] else "live",
            "config": read_obf_config(),
            "phase": phase,
            "is_running": is_running,
            "active_action": active_action,
            "prepared_exists": True,
            "prepared_at": prepared.get("prepared_at"),
            "approval": prepared.get("approval_state"),
            "control": prepared.get("outreach_control") or {},
            "state_path": str(prepared_path),
            "journal_path": str(journal_path),
            "summary": summary,
            "issues": obf_issue_rows(events),
            "checkpoints": {},
            "history": obf_history(),
            "source_rows": source_rows,
            "source_summary": source_summary,
            "last_updated": summary.get("last_event_time")
            or prepared.get("prepared_at")
            or refreshed_at,
        }

    # Before Prep, show only the locally-cached source snapshot.
    payload = {
        "today": day,
        "preview": False,
        "source_mode": "test" if source["test_mode"] else "live",
        "config": read_obf_config(),
        "phase": "preparing"
        if is_running and active_action == "prepare"
        else ("source_ready" if source_rows else "waiting"),
        "is_running": is_running,
        "active_action": active_action,
        "prepared_exists": False,
        "prepared_at": None,
        "approval": cached_control.get("approval"),
        "control": {
            "effective_target": cached_control.get("target_total", 0),
            "current_progress": int((cached_control.get("row") or {}).get("Current Progress") or 0),
            "prospects_start_row": (cached_control.get("approval") or {}).get(
                "prospects_start_row"
            ),
            "status": (cached_control.get("approval") or {}).get("status")
            or "Waiting for preparation",
        },
        "state_path": (
            f"{source['prospects_tab']} · local snapshot refreshed {refreshed_at}"
            if refreshed_at
            else f"{source['prospects_tab']} · no local snapshot yet"
        ),
        "journal_path": "No execution journal",
        "summary": {
            "rows": [],
            "planned": 0,
            "target": 0,
            "sent": 0,
            "reconciled": 0,
            "requires_email": 0,
            "failed": 0,
            "pending_sync": 0,
            "timed_out": 0,
            "resolved": 0,
            "retries": 0,
            "start_time": None,
            "last_event_time": None,
        },
        "source_rows": source_rows,
        "source_summary": source_summary,
        "issues": [],
        "checkpoints": {},
        "history": obf_history(),
        "last_updated": refreshed_at,
    }
    return payload


def start_obf_action(action: str, requested: dict[str, Any]) -> dict[str, Any]:
    """Start the same production command used by the desktop dashboard."""
    global OBF_ACTIVE_PROCESS
    if action not in {"prepare", "execute"}:
        raise ValueError("Unsupported OBF action.")
    with OBF_RUN_LOCK:
        if OBF_ACTIVE_PROCESS and OBF_ACTIVE_PROCESS.poll() is None:
            return {"ok": False, "error": "An OBF action is already running."}
        source = read_obf_source_config()
        day = local_now().date().isoformat()
        prepared_path = OBF_PREPARED_DIR / f"{day}-prepared.json"
        if action == "prepare":
            command = [
                sys.executable,
                str(HELPERS_DIR / "linkedin_outreach_session.py"),
                "prepare-8_30-session",
                "--date",
                day,
                "--prospects-tab",
                source["prospects_tab"],
            ]
        else:
            if source["test_mode"]:
                return {
                    "ok": False,
                    "error": "Switch back to the live Prospects source before running production Execute.",
                }
            if not read_json(prepared_path, {}).get("ready"):
                return {"ok": False, "error": "Run Prep first to create today’s frozen queue."}
            max_sends = max(
                1, min(30, int(requested.get("max_sends") or read_obf_config()["max_sends"]))
            )
            command = [
                sys.executable,
                str(SCRIPTS_DIR / "run_outreach_lanes.py"),
                "--date",
                day,
                "--max-sends",
                str(max_sends),
            ]
        write_json(
            OBF_RUN_STATE_PATH,
            {
                "status": "running",
                "action": action,
                "started_at": local_now().isoformat(),
                "command": command,
            },
        )
        OBF_RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_handle = OBF_RUN_LOG_PATH.open("a", encoding="utf-8")
        log_handle.write(f"\n[{local_now().isoformat()}] {' '.join(command)}\n")
        log_handle.flush()
        OBF_ACTIVE_PROCESS = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        def wait_for_completion(process: subprocess.Popen, log_file: Any) -> None:
            code = process.wait()
            log_file.close()
            with OBF_RUN_LOCK:
                write_json(
                    OBF_RUN_STATE_PATH,
                    {
                        "status": "complete" if code == 0 else "failed",
                        "action": action,
                        "started_at": read_json(OBF_RUN_STATE_PATH, {}).get("started_at"),
                        "finished_at": local_now().isoformat(),
                        "exit_code": code,
                    },
                )

        threading.Thread(
            target=wait_for_completion, args=(OBF_ACTIVE_PROCESS, log_handle), daemon=True
        ).start()
    return {"ok": True, "action": action, "date": day}


def start_withdrawal_action(action: str, requested: dict[str, Any]) -> dict[str, Any]:
    """Launch a withdrawal action from the local dashboard without using Electron IPC."""
    if action not in {"prepare", "execute", "full"}:
        raise ValueError("Unsupported withdrawal action.")
    with WITHDRAWAL_RUN_LOCK:
        active = read_json(WITHDRAWAL_DASHBOARD_RUN_STATE_PATH, {})
        if active.get("status") == "running":
            return {"ok": False, "error": "A withdrawal action is already running."}

        day = local_now().date().isoformat()
        session = WITHDRAWAL_SESSIONS_DIR / f"{day}.json"
        if action == "execute" and not read_json(session, {}).get("runtime_plan"):
            return {"ok": False, "error": "Prepare today’s withdrawal queue before executing it."}

        backfill = [sys.executable, str(SCRIPTS_DIR / "backfill_withdrawal_countdown.py")]
        requested_limit = int(requested.get("limit") or 0)
        if requested_limit < 0 or requested_limit > 50:
            return {"ok": False, "error": "Withdrawal preparation limit must be between 1 and 50."}
        prepare = [
            sys.executable,
            str(SCRIPTS_DIR / "prepare_connection_withdrawals.py"),
            "--date",
            day,
            "--session",
            str(session),
        ]
        if requested_limit:
            prepare.extend(["--limit", str(requested_limit)])
        execute = [
            sys.executable,
            str(SCRIPTS_DIR / "run_withdrawal_lanes.py"),
            "--date",
            day,
            "--session",
            str(session),
        ]
        commands = (
            [backfill, prepare]
            if action == "prepare"
            else [execute]
            if action == "execute"
            else [backfill, prepare, execute]
        )
        started_at = local_now().isoformat()
        write_json(
            WITHDRAWAL_DASHBOARD_RUN_STATE_PATH,
            {"status": "running", "action": action, "started_at": started_at, "command": commands},
        )
        WITHDRAWAL_DASHBOARD_RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        def run_chain() -> None:
            global WITHDRAWAL_ACTIVE_PROCESS
            exit_code = 0
            with WITHDRAWAL_DASHBOARD_RUN_LOG_PATH.open("a", encoding="utf-8") as log_handle:
                for command in commands:
                    log_handle.write(f"\n[{local_now().isoformat()}] {' '.join(command)}\n")
                    log_handle.flush()
                    process = subprocess.Popen(
                        command,
                        cwd=PROJECT_DIR,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    with WITHDRAWAL_RUN_LOCK:
                        WITHDRAWAL_ACTIVE_PROCESS = process
                    exit_code = process.wait()
                    if exit_code != 0:
                        break
            with WITHDRAWAL_RUN_LOCK:
                WITHDRAWAL_ACTIVE_PROCESS = None
                write_json(
                    WITHDRAWAL_DASHBOARD_RUN_STATE_PATH,
                    {
                        "status": "complete" if exit_code == 0 else "failed",
                        "action": action,
                        "started_at": started_at,
                        "finished_at": local_now().isoformat(),
                        "exit_code": exit_code,
                    },
                )

        threading.Thread(target=run_chain, daemon=True).start()
    return {"ok": True, "action": action, "date": day}


def stop_withdrawal_action() -> dict[str, Any]:
    global WITHDRAWAL_ACTIVE_PROCESS
    with WITHDRAWAL_RUN_LOCK:
        process = WITHDRAWAL_ACTIVE_PROCESS
        if not process or process.poll() is not None:
            return {"ok": False, "error": "No dashboard-started withdrawal action is running."}
        os.killpg(process.pid, signal.SIGTERM)
    return {"ok": True}


def save_obf_settings(requested: dict[str, Any]) -> dict[str, Any]:
    """Persist local schedule settings and sync daily controls to Outreach Control."""
    if obf_run_state().get("status") == "running":
        return {
            "ok": False,
            "error": "Wait for the active OBF action to finish before changing settings.",
        }
    current = read_obf_config()
    try:
        next_config = {
            **current,
            "enabled": bool(requested.get("enabled")),
            "daily_volume": max(
                1, min(30, int(requested.get("daily_volume") or current["daily_volume"]))
            ),
            "prospects_start_row": int(requested["prospects_start_row"])
            if requested.get("prospects_start_row")
            else None,
            "prep_time": str(requested.get("prep_time") or current["prep_time"]),
            "exec_time": str(requested.get("exec_time") or current["exec_time"]),
            "max_sends": max(1, min(30, int(requested.get("max_sends") or current["max_sends"]))),
        }
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "Daily volume, start row, and run ceiling must be valid numbers.",
        }
    if not re.fullmatch(r"\d{2}:\d{2}", next_config["prep_time"]) or not re.fullmatch(
        r"\d{2}:\d{2}", next_config["exec_time"]
    ):
        return {"ok": False, "error": "Prep and execution times must use HH:MM."}
    if next_config["prospects_start_row"] is not None and next_config["prospects_start_row"] < 2:
        return {"ok": False, "error": "Prospects start row must be 2 or greater."}
    if requested.get("update_sheet", True):
        command = [
            sys.executable,
            str(HELPERS_DIR / "linkedin_outreach_session.py"),
            "configure-outreach-control",
            "--date",
            local_now().date().isoformat(),
            "--daily-volume",
            str(next_config["daily_volume"]),
        ]
        if next_config["prospects_start_row"] is not None:
            command.extend(["--prospects-start-row", str(next_config["prospects_start_row"])])
        command.extend(["--approved", "true" if requested.get("approved") else "false"])
        completed = subprocess.run(command, cwd=PROJECT_DIR, text=True, capture_output=True)
        if completed.returncode != 0:
            return {
                "ok": False,
                "error": completed.stderr.strip()
                or completed.stdout.strip()
                or "Could not update Outreach Control.",
            }
    write_json(OBF_CONFIG_PATH, next_config)
    refresh_obf_source_snapshot()
    return {
        "ok": True,
        "config": next_config,
        "requires_reprep": bool(
            (OBF_PREPARED_DIR / f"{local_now().date().isoformat()}-prepared.json").exists()
        ),
    }


def latest_json_file(directory: Path) -> Path | None:
    candidates = sorted(directory.glob("*.json"), reverse=True) if directory.exists() else []
    return candidates[0] if candidates else None


def latest_matching_computation(review_leads: list) -> dict[str, Any]:
    lead_ids = {str(lead.get("run_id") or "") for lead in review_leads}
    if not lead_ids or not LEAD_PREP_COMPUTATIONS_DIR.exists():
        return {"file": "", "data": {}}
    for path in sorted(LEAD_PREP_COMPUTATIONS_DIR.glob("*.json"), reverse=True):
        data = read_json(path, {})
        if any(str(lead.get("lead_id") or "") in lead_ids for lead in data.get("leads", [])):
            return {"file": str(path), "data": data}
    return {"file": "", "data": {}}


def manual_computation_path(review: dict[str, Any]) -> Path:
    day = re.sub(r"[^0-9-]", "", str(review.get("date") or "undated"))
    group_row = int(review.get("group_row") or 0)
    return LEAD_PREP_COMPUTATIONS_DIR / f"manual_{day}_{group_row}_computation.json"


def valid_linkedin_profile(value: Any) -> bool:
    return bool(
        re.match(
            r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^?#\s]+(?:[?#].*)?$",
            str(value or "").strip(),
            re.I,
        )
    )


def manual_lead_ready(lead: dict[str, Any]) -> bool:
    executives = list(lead.get("executives") or [])
    if not executives:
        return False
    first = executives[0]
    return bool(
        str(first.get("name") or "").strip()
        and str(first.get("title") or "").strip()
        and valid_linkedin_profile(first.get("linkedin_url"))
    )


def normalize_manual_executives(raw: Any) -> list:
    executives = []
    for item in list(raw or [])[:3]:
        person = {
            "name": str(item.get("name") or "").strip()[:200],
            "title": str(item.get("title") or "").strip()[:300],
            "linkedin_url": str(item.get("linkedin_url") or "").strip()[:500],
            "email": str(item.get("email") or "").strip()[:300],
            "research_source": "manual_dashboard",
            "linkedin_source": "manual_dashboard",
            "needs_linkedin_search": False,
            "reconciliation_notes": [],
        }
        if person["linkedin_url"] and not valid_linkedin_profile(person["linkedin_url"]):
            raise ValueError("LinkedIn URLs must be profile links using linkedin.com/in/.")
        executives.append(person)
    while executives and not any(
        executives[-1][field] for field in ("name", "title", "linkedin_url", "email")
    ):
        executives.pop()
    return executives


def create_manual_computation(dashboard: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    review = dashboard.get("review", {})
    processing = dashboard.get("processing", {})
    path = manual_computation_path(review)
    existing = read_json(path, {}) if path.exists() else {}
    if existing:
        return path, existing
    source_run = read_json(Path(str(dashboard.get("run", {}).get("file") or "")), {})
    source_by_id = {str(item.get("id") or ""): item for item in source_run.get("leads", [])}
    leads = []
    for lead in processing.get("leads", []):
        source = source_by_id.get(str(lead.get("run_id") or ""), {})
        company = source.get("company", {})
        leads.append(
            {
                "lead_id": lead.get("run_id", ""),
                "company": lead.get("company", ""),
                "website": lead.get("website", ""),
                "company_linkedin": company.get("linkedin", ""),
                "emp_count": company.get("employee_count", lead.get("employee_count", "")),
                "source_tab": source.get("source_tab", ""),
                "use": lead.get("use", ""),
                "primary_lane": lead.get("primary_lane", ""),
                "prep_wave": lead.get("prep_wave", "Base"),
                "source_rows": source.get("source_rows", {}),
                "employees_from_sheet": source.get("employees_from_sheet", []),
                "executives": list(lead.get("executives") or [])[:3],
                "search_tasks": [],
                "search_results": [],
                "destination_row": {},
                "status": ("manual_ready" if manual_lead_ready(lead) else "manual_draft"),
                "notes": [],
            }
        )
    if not leads:
        raise ValueError("No reviewed leads are available for manual research.")
    now = local_now().isoformat()
    computation = {
        "computation_id": path.stem,
        "created_at": now,
        "updated_at": now,
        "mode": "manual",
        "status": (
            "manual_research_ready"
            if all(manual_lead_ready(lead) for lead in leads)
            else "manual_research_in_progress"
        ),
        "source_run_file": dashboard.get("run", {}).get("file", ""),
        "review_date": review.get("date", ""),
        "review_group_row": review.get("group_row"),
        "lead_count": len(leads),
        "leads": leads,
        "search_tasks": [],
        "search_tasks_remaining": 0,
        "writes": [],
        "errors": [],
    }
    write_json(path, computation)
    return path, computation


def processing_final_report(
    computation: dict[str, Any], computation_file: str = ""
) -> dict[str, Any]:
    if not computation:
        return {
            "available": False,
            "terminal": False,
            "outcome": "waiting",
            "outcome_label": "Waiting for computation",
            "tone": "neutral",
            "mode": "unknown",
            "mode_label": "Not started",
            "summary": "No persisted Process Approved Leads computation matches this review group yet.",
            "counts": {},
            "write": {},
            "issues": [],
            "last_updated": None,
            "computation_file": computation_file,
        }

    leads = list(computation.get("leads") or [])
    tasks = list(computation.get("search_tasks") or [])
    batches = list(computation.get("search_result_batches") or [])
    writes = list(computation.get("writes") or [])
    latest_batch = batches[-1] if batches else {}
    # A verification dry-run can be recorded after a real publication. Report
    # the latest actual Sheet write, falling back to the dry-run only when it
    # is the sole attempt.
    latest_write = next(
        (write for write in reversed(writes) if not write.get("dry_run")),
        writes[-1] if writes else {},
    )
    skipped = list(latest_write.get("skipped_unresolved") or [])
    batch_results = list(latest_batch.get("results") or [])
    errors = list(computation.get("errors") or [])
    research_completed = sum(1 for lead in leads if list(lead.get("executives") or []))
    person_coverage = [
        sum(
            1
            for lead in leads
            if len(list(lead.get("executives") or [])) > index
            and str((lead.get("executives") or [])[index].get("name") or "").strip()
        )
        for index in range(3)
    ]
    rows_ready = 0
    for lead in leads:
        executives = list(lead.get("executives") or [])
        first = executives[0] if executives else {}
        if (
            str(lead.get("lead_id") or "").strip()
            and str(lead.get("company") or "").strip()
            and str(lead.get("website") or "").strip()
            and str(first.get("name") or "").strip()
            and valid_linkedin_profile(first.get("linkedin_url"))
        ):
            rows_ready += 1
    pending_tasks = sum(1 for task in tasks if str(task.get("status") or "pending") == "pending")
    selected_tasks = sum(1 for task in tasks if str(task.get("status") or "") == "selected")
    search_errors = sum(1 for result in batch_results if str(result.get("status") or "") == "error")
    needs_review = sum(
        1 for result in batch_results if str(result.get("status") or "") == "needs_review"
    )
    retry_count = sum(
        max(
            int(task.get("retry_count") or 0),
            max(0, int(task.get("attempts") or 1) - 1),
        )
        for task in tasks
    )
    conflict_count = sum(1 for lead in leads if str(lead.get("status") or "") == "archive_conflict")
    rows_written = int(latest_write.get("rows_written") or 0)
    skipped_count = int(latest_write.get("skipped_unresolved_count") or len(skipped))
    raw_status = str(computation.get("status") or "unknown")
    lowered_status = raw_status.lower()
    error_count = len(errors) + search_errors
    if "fail" in lowered_status or "error" in lowered_status:
        outcome, label, tone, terminal = "failed", "Failed", "danger", True
    elif rows_written or lowered_status in {"written", "write_partial"}:
        if skipped_count or rows_written < len(leads):
            outcome, label, tone, terminal = (
                "partial",
                "Completed with unresolved rows",
                "warning",
                True,
            )
        else:
            outcome, label, tone, terminal = (
                "completed",
                "Completed and written",
                "success",
                True,
            )
    elif "archive" in lowered_status and (
        "unreviewed" in lowered_status or lowered_status == "archived"
    ):
        outcome, label, tone, terminal = "archived", "Archived", "neutral", True
    elif error_count:
        outcome, label, tone, terminal = (
            "attention",
            "Attention required",
            "warning",
            False,
        )
    else:
        outcome, label, tone, terminal = (
            "in_progress",
            "Persisted state available",
            "active",
            False,
        )
    mode = "manual" if str(computation.get("mode") or "") == "manual" else "codex"
    issues = []
    for item in skipped:
        reasons = ", ".join(str(reason).replace("_", " ") for reason in item.get("reasons", []))
        issues.append(
            {
                "type": "unresolved",
                "tone": "warning",
                "lead_id": str(item.get("lead_id") or ""),
                "company": str(item.get("company") or ""),
                "message": reasons or "Row was skipped as unresolved.",
            }
        )
    for result in batch_results:
        if str(result.get("status") or "") not in {"error", "needs_review"}:
            continue
        issues.append(
            {
                "type": "search",
                "tone": ("danger" if str(result.get("status") or "") == "error" else "warning"),
                "lead_id": str(result.get("lead_id") or ""),
                "company": str(result.get("company") or ""),
                "message": str(
                    result.get("error") or result.get("message") or "LinkedIn search needs review."
                ),
            }
        )
    for lead in leads:
        if str(lead.get("status") or "") != "archive_conflict":
            continue
        issues.append(
            {
                "type": "archive_conflict",
                "tone": "warning",
                "lead_id": str(lead.get("lead_id") or ""),
                "company": str(lead.get("company") or ""),
                "message": "Archive identity conflict requires reconciliation.",
            }
        )
    for error in errors:
        if isinstance(error, dict):
            message = (
                error.get("error")
                or error.get("message")
                or error.get("stderr")
                or json.dumps(error, ensure_ascii=False)
            )
        else:
            message = str(error)
        issues.append(
            {
                "type": "error",
                "tone": "danger",
                "lead_id": "",
                "company": "",
                "message": str(message),
            }
        )
    destination = str(latest_write.get("destination_tab") or "")
    published = dict(latest_write.get("queue_batch", {}).get("prefinal_publish") or {})
    write_timestamp = published.get("published_at") or latest_write.get("created_at") or None
    if outcome == "completed":
        summary = (
            f"{rows_written} of {len(leads)} leads were written to {destination or 'Pre-final'}."
        )
    elif outcome == "partial":
        summary = (
            f"{rows_written} of {len(leads)} leads were written; "
            f"{skipped_count} unresolved row{'s' if skipped_count != 1 else ''} were skipped."
        )
    elif outcome == "archived":
        summary = (
            f"{len(leads)} leads were retained in the research archive instead of being published."
        )
    elif outcome == "failed":
        summary = "The persisted computation ended in a failure state. Review the reported issues before retrying."
    else:
        summary = (
            f"{research_completed} of {len(leads)} leads have persisted research; "
            f"{pending_tasks} search task{'s' if pending_tasks != 1 else ''} remain."
        )
    return {
        "available": True,
        "terminal": terminal,
        "outcome": outcome,
        "outcome_label": label,
        "tone": tone,
        "raw_status": raw_status,
        "mode": mode,
        "mode_label": ("Manual dashboard" if mode == "manual" else "Codex orchestration"),
        "summary": summary,
        "counts": {
            "lead_count": len(leads),
            "research_completed": research_completed,
            "research_pending": max(0, len(leads) - research_completed),
            "p1_count": person_coverage[0],
            "p2_count": person_coverage[1],
            "p3_count": person_coverage[2],
            "rows_ready": max(rows_ready, rows_written),
            "rows_written": rows_written,
            "rows_skipped": skipped_count,
            "search_total": len(tasks),
            "search_completed": max(
                selected_tasks,
                int(latest_batch.get("completed_tasks") or 0),
            ),
            "search_pending": int(
                computation.get("search_tasks_remaining")
                if "search_tasks_remaining" in computation
                else pending_tasks
            ),
            "search_needs_review": needs_review,
            "search_errors": search_errors,
            "archive_conflicts": conflict_count,
            "retries": retry_count,
            "captcha_events": int(latest_batch.get("captcha_events") or 0),
            "errors": error_count,
        },
        "write": {
            "attempted": bool(writes),
            "destination": destination,
            "created_at": write_timestamp,
            "verified": published.get("verified"),
            "status": str(published.get("status") or ""),
            "queue_fingerprint": str(latest_write.get("queue_batch", {}).get("fingerprint") or ""),
        },
        "issues": issues,
        "last_updated": (
            write_timestamp
            or latest_batch.get("created_at")
            or computation.get("updated_at")
            or computation.get("created_at")
        ),
        "computation_file": computation_file,
    }


def latest_matching_review_run(review_leads: list) -> dict[str, Any]:
    expected_ids = {str(lead.get("run_id") or "") for lead in review_leads if lead.get("run_id")}
    if not expected_ids or not LEAD_PREP_RUNS_DIR.exists():
        return {"file": "", "data": {}}
    best = {"file": "", "data": {}, "overlap": 0}
    for path in sorted(LEAD_PREP_RUNS_DIR.glob("*.json"), reverse=True):
        data = read_json(path, {})
        run_ids = {str(lead.get("id") or "") for lead in data.get("leads", []) if lead.get("id")}
        overlap = len(expected_ids & run_ids)
        if run_ids == expected_ids:
            return {"file": str(path), "data": data}
        if overlap > best["overlap"]:
            best = {"file": str(path), "data": data, "overlap": overlap}
    return {"file": best["file"], "data": best["data"]}


def run_matches_day(run: dict[str, Any], today: str) -> bool:
    if str(run.get("created_at") or "").startswith(today):
        return True
    review_date = (
        run.get("review", {}).get("write", {}).get("date")
        or run.get("review_write", {}).get("date")
        or ""
    )
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(review_date), pattern).date().isoformat() == today
        except ValueError:
            continue
    return False


def computation_destination_row(lead: dict[str, Any]) -> dict[str, Any]:
    row = dict(lead.get("destination_row") or {})
    people = list(lead.get("executives") or [])[:3]
    for index, person in enumerate(people, start=1):
        row.setdefault(f"P{index} Name", person.get("name", ""))
        row.setdefault(f"P{index} Title", person.get("title", ""))
        row.setdefault(f"P{index} LinkedIn", person.get("linkedin_url", ""))
        row.setdefault(f"P{index} Email", person.get("email", ""))
    return row


def read_activity_dashboard(today: str) -> dict[str, Any]:
    path = ACTIVITY_SESSIONS_DIR / f"{today}.json"
    session = read_json(path, {}) if path.exists() else {}
    prepared = session.get("prepared_targets") or []
    recorded = session.get("targets") or {}
    recorded_by_key = (
        recorded
        if isinstance(recorded, dict)
        else {str(item.get("key") or ""): item for item in recorded}
    )
    targets = []
    for item in prepared:
        live = recorded_by_key.get(str(item.get("key") or ""), {})
        targets.append(
            {
                **item,
                "activity_value": live.get("activity_value", ""),
                "status": live.get("status") or "prepared",
                "recorded_at": live.get("recorded_at"),
            }
        )
    recorded_count = sum(1 for target in targets if target.get("status") == "recorded")
    active_signals = sum(
        1
        for target in targets
        if str(target.get("activity_value") or "").strip().lower() in {"active", "very active"}
    )
    bridged_rows = int(session.get("bridged_rows_count") or 0)
    checkpoints = (
        read_json(WATCHER_STATE_PATH, {})
        .get("workflows", {})
        .get("activity", {})
        .get("days", {})
        .get(today, {})
        .get("checkpoints", {})
    )
    active_checkpoints = [
        item
        for item in checkpoints.values()
        if item.get("status") in {"running", "waiting"} and item.get("started_at")
    ]
    latest_checkpoint = max(
        active_checkpoints or [item for item in checkpoints.values() if item.get("started_at")],
        key=lambda item: str(item.get("started_at") or ""),
        default={},
    )
    run_started_at = str(latest_checkpoint.get("started_at") or "")
    current_records = [
        target
        for target in targets
        if run_started_at and str(target.get("recorded_at") or "") >= run_started_at
    ]
    prior_count = (
        sum(
            1
            for target in targets
            if target.get("recorded_at") and str(target.get("recorded_at")) < run_started_at
        )
        if run_started_at
        else 0
    )
    current_total = max(0, len(targets) - prior_count) if run_started_at else len(targets)
    current_completed = len(current_records)
    quarantine_issues = read_activity_quarantine_issues()
    return {
        "exists": bool(session),
        "file": str(path) if path.exists() else "",
        "status": session.get("status", "not_prepared"),
        "target_count": len(targets),
        "lead_ids": sorted(
            {str(item.get("lead_id") or "") for item in targets if item.get("lead_id")}
        ),
        "targets": targets,
        "prepared_at": session.get("prepared_at"),
        "updated_at": session.get("updated_at") or session.get("completed_at"),
        "failures": session.get("failures", []),
        "stats": {
            "profiles_prepared": len(targets),
            "profiles_recorded": recorded_count,
            "profiles_pending": max(0, len(targets) - recorded_count),
            "unique_leads": len(
                {str(item.get("lead_id") or "") for item in targets if item.get("lead_id")}
            ),
            "active_signals": active_signals,
            "bridged_rows": bridged_rows,
            "failures": len(session.get("failures") or []),
        },
        "current_run": {
            "started_at": run_started_at or None,
            "status": latest_checkpoint.get("status") or "idle",
            "profiles_total": current_total,
            "profiles_completed": current_completed,
            "profiles_pending": max(0, current_total - current_completed),
            "prior_records": prior_count,
            "active_signals": sum(
                1
                for target in current_records
                if str(target.get("activity_value") or "").strip().lower()
                in {"active", "very active"}
            ),
        },
        "quarantine_issues": quarantine_issues,
        "history": read_activity_history(),
    }


def read_activity_quarantine_issues() -> list:
    issues = []
    for queue_path in sorted((STATE_DIR / "prefinal_queue").glob("*.json")):
        queue = read_json(queue_path, {})
        if queue.get("status") in {"prospects_bridged", "final_bridged"}:
            continue
        activity_issues = queue.get("activity_issues") or {}
        if not isinstance(activity_issues, dict):
            continue
        rows_by_id = {
            str(row.get("ID") or "").strip(): row
            for row in queue.get("rows", [])
            if str(row.get("ID") or "").strip()
        }
        for key, issue in sorted(activity_issues.items()):
            lead_id, _, prefix = str(key).partition(":")
            row = rows_by_id.get(lead_id, {})
            issues.append(
                {
                    "key": key,
                    "queue_fingerprint": queue.get("fingerprint", queue_path.stem),
                    "lead_id": lead_id,
                    "company": row.get("Company", ""),
                    "person": prefix,
                    "name": row.get(f"{prefix} Name", ""),
                    "title": row.get(f"{prefix} Title", ""),
                    "profile_url": issue.get("profile_url", ""),
                    "reason": issue.get("terminal_reason", issue.get("reason", "")),
                    "recorded_at": issue.get("recorded_at", ""),
                }
            )
    return issues


def read_activity_history() -> list:
    files = sorted(ACTIVITY_SESSIONS_DIR.glob("*.json"), reverse=True)
    signature = tuple((str(path), path.stat().st_mtime_ns) for path in files)
    if signature == ACTIVITY_HISTORY_CACHE["signature"]:
        return ACTIVITY_HISTORY_CACHE["rows"]
    rows = []
    for path in files:
        session = read_json(path, {})
        prepared = list(session.get("prepared_targets") or [])
        recorded = session.get("targets") or {}
        recorded_rows = list(recorded.values()) if isinstance(recorded, dict) else list(recorded)
        recorded_count = sum(1 for item in recorded_rows if item.get("status") == "recorded")
        active_signals = sum(
            1
            for item in recorded_rows
            if str(item.get("activity_value") or "").strip().lower() in {"active", "very active"}
        )
        rows.append(
            {
                "date": str(session.get("date") or path.stem),
                "status": str(session.get("status") or "not_prepared"),
                "profiles_prepared": len(prepared),
                "profiles_recorded": recorded_count,
                "profiles_pending": max(0, len(prepared) - recorded_count),
                "unique_leads": len(
                    {str(item.get("lead_id") or "") for item in prepared if item.get("lead_id")}
                ),
                "active_signals": active_signals,
                "bridged_rows": int(session.get("bridged_rows_count") or 0),
                "failures": len(session.get("failures") or []),
                "updated_at": session.get("updated_at")
                or session.get("completed_at")
                or session.get("prepared_at"),
            }
        )
    ACTIVITY_HISTORY_CACHE["signature"] = signature
    ACTIVITY_HISTORY_CACHE["rows"] = rows
    return rows


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def read_automation_status(path: Path) -> str:
    try:
        match = re.search(r'^status\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else "UNKNOWN"
    except OSError:
        return "UNKNOWN"


def read_lead_prep_dashboard() -> dict[str, Any]:
    config = read_lead_prep_config()
    archive = read_json(LEAD_PREP_ARCHIVE_PATH, {"entries": {}, "updated_at": ""})
    raw_entries = archive.get("entries", {})
    entries = list(raw_entries.values()) if isinstance(raw_entries, dict) else list(raw_entries)
    available = sum(1 for entry in entries if entry.get("status", "available") == "available")
    consumed = sum(1 for entry in entries if entry.get("status") == "consumed")

    run_path = latest_json_file(LEAD_PREP_RUNS_DIR)
    run = read_json(run_path, {}) if run_path else {}
    overlap_scan = run.get("overlap_scan") or run.get("archive_reconciliation") or {}
    review_cache = read_json(LEAD_REVIEW_CACHE_PATH, {})
    progress_state = read_json(LEAD_RESEARCH_PROGRESS_PATH, {"days": {}})
    now = local_now()
    today = str(config.get("dashboard_date_override") or now.date().isoformat())
    cached_today = review_cache.get("today", {})
    if cached_today.get("date") != today:
        cached_today = {
            "date": today,
            "review_complete": False,
            "prepared_count": 0,
            "approved_count": 0,
            "case_study_worthy_count": 0,
            "archive_match_count": 0,
            "conflict_count": 0,
            "fresh_count": 0,
            "leads": [],
        }

    progress_for_today = progress_state.get("days", {}).get(today, {})
    prepared_leads = []
    for lead in cached_today.get("leads", []):
        prepared_leads.append(
            {
                **lead,
                "manual_research": progress_for_today.get(
                    lead.get("run_id", ""),
                    {"status": "not_started", "notes": "", "updated_at": None},
                ),
            }
        )
    matching_run = latest_matching_review_run(prepared_leads)
    if matching_run["file"]:
        run_path = Path(matching_run["file"])
        run = matching_run["data"]
        overlap_scan = run.get("overlap_scan") or run.get("archive_reconciliation") or {}
    manual_research_completed = sum(
        1 for lead in prepared_leads if lead.get("manual_research", {}).get("status") == "completed"
    )
    approved_review_leads = [lead for lead in prepared_leads if lead.get("approved")]
    matching_computation = latest_matching_computation(prepared_leads)
    computation_by_lead_id = {
        str(lead.get("lead_id") or ""): lead
        for lead in matching_computation["data"].get("leads", [])
    }
    activity = read_activity_dashboard(today)
    activity_lead_ids = set(activity["lead_ids"])
    approval_gate_enabled = bool(config.get("approval_gate_enabled"))
    if computation_by_lead_id:
        processing_source = [
            lead
            for lead in prepared_leads
            if str(lead.get("run_id") or "") in computation_by_lead_id
        ]
        selection_mode = "computation_state"
    elif not approval_gate_enabled:
        processing_source = prepared_leads
        selection_mode = "approval_disabled"
    elif cached_today.get("review_complete"):
        processing_source = approved_review_leads
        selection_mode = "approved_only"
    else:
        processing_source = []
        selection_mode = "waiting_for_review_handoff"
    processing_leads = []
    for lead in processing_source:
        if str(lead.get("run_id") or "") in activity_lead_ids:
            continue
        computation_lead = computation_by_lead_id.get(str(lead.get("run_id") or ""), {})
        processing_leads.append(
            {
                **lead,
                "processing_status": computation_lead.get("status") or "awaiting_processing",
                "executive_count": len(computation_lead.get("executives", [])),
                "executives": computation_lead.get("executives", []),
                "search_tasks_total": len(computation_lead.get("search_tasks", [])),
                "destination_row": computation_destination_row(computation_lead),
                "processing_notes": computation_lead.get("notes", []),
            }
        )
    computation_mode = str(matching_computation["data"].get("mode") or "")
    manual_ready_count = sum(
        1 for lead in matching_computation["data"].get("leads", []) if manual_lead_ready(lead)
    )
    bridged = any(
        int(write.get("rows_written") or 0) > 0
        for write in matching_computation["data"].get("writes", [])
    )
    final_report = processing_final_report(
        matching_computation["data"], matching_computation["file"]
    )
    first_automation_status = read_automation_status(PROCESS_APPROVED_FIRST_AUTOMATION_PATH)
    fallback_automation_status = read_automation_status(PROCESS_APPROVED_FALLBACK_AUTOMATION_PATH)

    first_deadline = datetime.fromisoformat(
        f"{today}T{config.get('first_review_deadline', '18:00')}:00"
    ).replace(tzinfo=LOCAL_TZ)
    fallback_deadline = datetime.fromisoformat(
        f"{today}T{config.get('fallback_review_deadline', '22:00')}:00"
    ).replace(tzinfo=LOCAL_TZ)
    review_window = (
        "first" if now < first_deadline else "fallback" if now < fallback_deadline else "closed"
    )
    review_status = (
        "review_complete"
        if cached_today.get("review_complete")
        else "waiting_for_group"
        if not prepared_leads
        else "review_due_before_first_run"
        if review_window == "first"
        else "first_run_missed"
        if review_window == "fallback"
        else "fallback_due_or_passed"
    )

    watcher = read_json(WATCHER_STATE_PATH, {})
    checkpoints = (
        watcher.get("workflows", {})
        .get("lead_prep", {})
        .get("days", {})
        .get(today, {})
        .get("checkpoints", {})
    )
    prepare_checkpoint = checkpoints.get("lead_prep_prepare", {})
    latest_run_is_today = run_matches_day(run, today)
    fresh_target = int(overlap_scan.get("fresh_target") or config.get("base_volume", 60))
    fresh_count = int(
        overlap_scan["fresh_count"]
        if "fresh_count" in overlap_scan
        else cached_today.get("fresh_count", 0)
    )
    archive_match_count = int(
        overlap_scan["archive_match_count"]
        if "archive_match_count" in overlap_scan
        else cached_today.get("archive_match_count", 0)
    )
    conflict_count = int(
        overlap_scan["conflict_count"]
        if "conflict_count" in overlap_scan
        else cached_today.get("conflict_count", 0)
    )
    target_met = bool(
        overlap_scan["target_met"] if "target_met" in overlap_scan else fresh_count >= fresh_target
    )
    summary = review_cache.get(
        "summary",
        {
            "total_leads": 0,
            "total_case_study_worthy": 0,
            "total_approved": 0,
            "date_groups": 0,
            "reviewed_date_groups": 0,
        },
    )

    return {
        "config": config,
        "archive": {
            "enabled": available > 0,
            "total": len(entries),
            "available": available,
            "consumed": consumed,
            "updated_at": archive.get("updated_at") or None,
            "path": str(LEAD_PREP_ARCHIVE_PATH),
        },
        "run": {
            "exists": bool(run.get("run_id")),
            "is_today": latest_run_is_today,
            "run_id": run.get("run_id", ""),
            "created_at": run.get("created_at"),
            "status": run.get("status", "not_prepared"),
            "file": str(run_path) if run_path else "",
            "selected_count": int(
                run.get("source", {}).get("selected_count") or len(run.get("leads", [])) or 0
            ),
            "fresh_target": fresh_target,
            "fresh_count": fresh_count,
            "archive_match_count": archive_match_count,
            "conflict_count": conflict_count,
            "top_up_count": int(overlap_scan.get("top_up_count") or 0),
            "top_up_suppressed_count": int(overlap_scan.get("top_up_suppressed_count") or 0),
            "top_ups_enabled": bool(overlap_scan.get("top_ups_enabled", True)),
            "target_met": target_met,
            "settled_for_day": bool(overlap_scan.get("settled_for_day", target_met)),
            "source_exhausted": bool(overlap_scan.get("source_exhausted")),
            "enabled": bool(overlap_scan.get("enabled")),
            "detection_only": bool(overlap_scan.get("detection_only", True)),
            "waves": overlap_scan.get("waves", []),
            "errors": run.get("errors", []),
        },
        "review": {
            **cached_today,
            "leads": prepared_leads,
            "manual_research_completed": manual_research_completed,
            "status": review_status,
            "window": review_window,
            "first_deadline_at": first_deadline.isoformat(),
            "fallback_deadline_at": fallback_deadline.isoformat(),
            "next_deadline_at": (
                first_deadline if now < first_deadline else fallback_deadline
            ).isoformat(),
            "cache": {
                "path": str(LEAD_REVIEW_CACHE_PATH),
                "cached_at": review_cache.get("cached_at"),
                "cache_date": review_cache.get("cache_date"),
                "checkpoint": checkpoints.get("lead_review_daily_cache", {}),
            },
            "processing_automation": {
                "first_status": first_automation_status,
                "fallback_status": fallback_automation_status,
                "paused": first_automation_status != "ACTIVE"
                and fallback_automation_status != "ACTIVE",
            },
        },
        "historical": {
            **summary,
            "recent_days": review_cache.get("history", [])[:14],
        },
        "processing": {
            "selection_mode": selection_mode,
            "eligible_count": len(processing_leads),
            "computation_file": matching_computation["file"],
            "computation_status": matching_computation["data"].get("status", ""),
            "computation_mode": computation_mode,
            "manual_editable": not bridged,
            "manual_ready_count": manual_ready_count,
            "bridge_ready": bool(processing_leads)
            and manual_ready_count == len(processing_leads)
            and computation_mode == "manual"
            and not bridged,
            "bridged": bridged,
            "report": final_report,
            "leads": processing_leads,
        },
        "activity": activity,
        "watcher": {
            "status": prepare_checkpoint.get("status")
            or ("waiting" if config.get("autonomous_prep_enabled") else "disabled"),
            "due": config.get("prep_time"),
            "completed_at": prepare_checkpoint.get("completed_at"),
            "error": "",
        },
        "is_running": prepare_checkpoint.get("status") == "running",
        "active_action": (
            "prepare-review" if prepare_checkpoint.get("status") == "running" else ""
        ),
        "last_updated": run.get("created_at")
        or archive.get("updated_at")
        or watcher.get("last_tick_at"),
    }


def run_project_script(script_name: str, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        cwd=PROJECT_DIR,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def read_withdrawal_state(day: str) -> dict[str, Any]:
    """Read the frozen withdrawal queue and journal only; never query Sheets."""
    session_path = WITHDRAWAL_SESSIONS_DIR / f"{day}.json"
    journal_path = WITHDRAWAL_JOURNAL_DIR / f"{day}.jsonl"
    session = read_json(session_path, {})
    queue = list((session.get("runtime_plan") or {}).get("queue") or [])
    events = read_json_lines(journal_path)
    session_names = {str(session_path)}
    try:
        session_names.add(str(session_path.resolve()))
    except OSError:
        pass
    target_events: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") != "withdrawal_target_result":
            continue
        if str(event.get("session") or "") not in session_names:
            continue
        prospect_id = str(event.get("prospect_id") or "").strip()
        if prospect_id:
            target_events[prospect_id] = event

    rows = []
    for index, target in enumerate(queue, start=1):
        prospect_id = str(target.get("prospect_id") or "").strip()
        event = target_events.get(prospect_id, {})
        if event.get("confirmed"):
            status = "Withdrawn"
        elif event:
            status = "Failed" if not event.get("ok", False) else "Not withdrawn"
        else:
            status = "Prepared"
        rows.append(
            {
                "index": index,
                "id": prospect_id,
                "company": str(target.get("company") or ""),
                "contact_name": str(target.get("contact_name") or ""),
                "sent_at": str(target.get("sent_at") or ""),
                "days_left": target.get("days_left"),
                "batch": target.get("batch_number"),
                "navigation": str(target.get("navigation_type") or "").replace("_", " "),
                "status": status,
                "last_update": event.get("recorded_at")
                or event.get("withdrawn_at")
                or session.get("prepared_at"),
            }
        )
    dashboard_run = read_json(WITHDRAWAL_DASHBOARD_RUN_STATE_PATH, {})
    running = dashboard_run.get("status") == "running" or any(
        "withdraw_connections.py" in line
        for line in subprocess.check_output(["ps", "-axo", "command="], text=True).splitlines()
    )
    guard = read_json(WITHDRAWAL_RUN_GUARD_PATH, {})
    completed = sum(row["status"] == "Withdrawn" for row in rows)
    failed = sum(row["status"] in {"Failed", "Not withdrawn"} for row in rows)
    return {
        "exists": bool(session),
        "session_path": str(session_path),
        "prepared_at": session.get("prepared_at"),
        "rows": rows,
        "list": rows,
        "prepared": len(rows),
        "completed": completed,
        "failed": failed,
        "pending": len(rows) - completed - failed,
        "is_running": running,
        "start_time": dashboard_run.get("started_at") or session.get("prepared_at"),
        "last_event_time": events[-1].get("recorded_at") if events else session.get("prepared_at"),
        "cooldown_active": bool(guard.get("last_live_run_started_at")) and running,
        "cooldown_until": None,
    }


def dashboard_stats(requested: dict[str, Any]) -> dict[str, Any]:
    configured = str(read_lead_prep_config().get("dashboard_date_override") or "")
    requested_day = str(requested.get("date") or "")
    today = requested_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested_day) else configured
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        today = local_now().date().isoformat()
    activity = read_activity_dashboard(today)
    watcher = read_json(WATCHER_STATE_PATH, {})
    watcher_completion = {"tracked_days": 0, "completed_days": 0, "completion_rate": 0}
    terminal_statuses = {
        "completed",
        "failed_terminal",
        "cutoff_skipped",
        "needs_attention",
        "paused_manual_stop",
    }
    # This endpoint powers the live overview. Keep historical alerts in watcher
    # state/history, but only surface alerts belonging to the selected day here.
    watcher_completion["alerts"] = [
        item
        for item in watcher.get("alerts", [])
        if not item.get("acknowledged")
        and (item.get("day") == today or str(item.get("created_at") or "").startswith(today))
    ]
    for queue_path in sorted((STATE_DIR / "prefinal_queue").glob("*.json")):
        queue = read_json(queue_path, {})
        issue_count = len(queue.get("activity_issues") or {})
        queue_day = str(
            queue.get("updated_at") or queue.get("prepared_at") or queue.get("created_at") or ""
        )[:10]
        if (
            queue_day == today
            and issue_count
            and queue.get("status") not in {"prospects_bridged", "final_bridged"}
        ):
            watcher_completion["alerts"].append(
                {
                    "workflow": "activity",
                    "reason": f"{issue_count}_quarantined_profile_issues",
                    "checkpoint": queue.get("fingerprint", ""),
                }
            )
    watcher_days: dict[str, list[dict[str, Any]]] = {}
    for workflow_name, workflow in watcher.get("workflows", {}).items():
        for day, day_data in workflow.get("days", {}).items():
            checkpoints = day_data.get("checkpoints", {})
            checkpoint_states = list(checkpoints.values())
            if checkpoint_states:
                watcher_days.setdefault(day, []).extend(checkpoint_states)
            if day == today:
                for checkpoint_name, checkpoint in checkpoints.items():
                    if checkpoint.get("status") in {"retry_waiting", "resume_pending"}:
                        watcher_completion["alerts"].append(
                            {
                                "workflow": workflow_name,
                                "reason": checkpoint.get("status"),
                                "checkpoint": checkpoint_name,
                                "next_retry_at": checkpoint.get("next_retry_at"),
                                "severity": "info",
                            }
                        )
                    if checkpoint.get("status") in {
                        "failed_terminal",
                        "needs_attention",
                        "interrupted_terminal",
                    }:
                        commands = (checkpoint.get("result") or {}).get("commands") or []
                        output = "\n".join(
                            f"{command.get('stdout_tail') or ''}\n{command.get('stderr_tail') or ''}"
                            for command in commands
                        )
                        blocked = re.search(r"Blocked:\s*([^\n]+)", output, re.IGNORECASE)
                        detail = (
                            blocked.group(1).strip()
                            if blocked
                            else str(
                                checkpoint.get("reason")
                                or checkpoint.get("status")
                                or "checkpoint failed"
                            ).replace("_", " ")
                        )
                        detail = re.sub(r"""["',}\s]+$""", "", detail)
                        existing = next(
                            (
                                alert
                                for alert in watcher_completion["alerts"]
                                if alert.get("workflow") == workflow_name
                                and alert.get("checkpoint") == checkpoint_name
                            ),
                            None,
                        )
                        if existing:
                            existing["detail"] = detail
                            existing["severity"] = "danger"
                        else:
                            watcher_completion["alerts"].append(
                                {
                                    "workflow": workflow_name,
                                    "reason": checkpoint.get("reason") or checkpoint.get("status"),
                                    "checkpoint": checkpoint_name,
                                    "detail": detail,
                                    "severity": "danger",
                                    "day": day,
                                }
                            )
    for checkpoint_states in watcher_days.values():
        if all(item.get("status") in terminal_statuses for item in checkpoint_states):
            watcher_completion["tracked_days"] += 1
            if all(item.get("status") == "completed" for item in checkpoint_states):
                watcher_completion["completed_days"] += 1
    finalized_checkpoints = [
        item
        for states in watcher_days.values()
        for item in states
        if item.get("status") in terminal_statuses
    ]
    watcher_completion["finalized_checkpoints"] = len(finalized_checkpoints)
    watcher_completion["completed_checkpoints"] = sum(
        item.get("status") == "completed" for item in finalized_checkpoints
    )
    if watcher_completion["finalized_checkpoints"]:
        watcher_completion["completion_rate"] = round(
            watcher_completion["completed_checkpoints"]
            / watcher_completion["finalized_checkpoints"]
            * 100
        )
    watcher_completion["history"] = [
        {
            "date": day,
            "completed": sum(item.get("status") == "completed" for item in states),
            "scheduled": len(states),
            "rate": round(
                sum(item.get("status") == "completed" for item in states) / len(states) * 100
            ),
            "outcome": "Completed"
            if states and all(item.get("status") == "completed" for item in states)
            else (
                "Finalized with exceptions"
                if all(item.get("status") in terminal_statuses for item in states)
                else "In progress"
            ),
        }
        for day, states in sorted(watcher_days.items(), reverse=True)
    ]
    deduped_alerts = []
    seen_alerts = set()
    for alert in watcher_completion["alerts"]:
        key = (alert.get("workflow"), alert.get("checkpoint"), alert.get("reason"))
        if key in seen_alerts:
            continue
        seen_alerts.add(key)
        deduped_alerts.append(alert)
    watcher_completion["alerts"] = deduped_alerts[:5]
    checkpoints = (
        watcher.get("workflows", {})
        .get("activity", {})
        .get("days", {})
        .get(today, {})
        .get("checkpoints", {})
    )
    is_running = any(item.get("status") in {"running", "waiting"} for item in checkpoints.values())
    stats = activity["stats"]
    current_run = activity.get("current_run") or {}
    withdrawal = read_withdrawal_state(today)
    followups_enabled = bool(read_json(FOLLOWUP_CONFIG_PATH, {}).get("enabled"))
    return {
        "today": today,
        "activity_prep_done": activity["exists"],
        "activity_run_done": stats["profiles_recorded"] > 0,
        "followup_prep_done": False,
        "followup_daytime_done": False,
        "followups_enabled": followups_enabled,
        "withdrawal_prep_done": withdrawal["exists"],
        "withdrawal_execute_done": withdrawal["completed"] > 0,
        "watcher": watcher_completion,
        "activity": {
            "prepared": current_run.get("profiles_total", stats["profiles_prepared"]),
            "completed": current_run.get("profiles_completed", stats["profiles_recorded"]),
            "failed": stats["failures"],
            "pending": current_run.get("profiles_pending", stats["profiles_pending"]),
            "is_running": is_running,
            "start_time": next(
                (item.get("started_at") for item in checkpoints.values() if item.get("started_at")),
                None,
            ),
            "last_event_time": activity.get("updated_at"),
            "next_action_due": None,
            "list": activity["targets"],
            "quarantine_issues": activity.get("quarantine_issues", []),
        },
        "followups": (
            lambda session: {
                "enabled": followups_enabled,
                "prepared": len(session.get("targets") or [])
                or int(session.get("first_message_draft_count") or 0),
                "completed": 0,
                "failed": 0,
                "pending": len(session.get("targets") or [])
                or int(session.get("first_message_draft_count") or 0),
                "is_running": False,
                "list": [],
                "due_today": len(session.get("targets") or [])
                or int(session.get("first_message_draft_count") or 0),
                "first_message_drafts": int(session.get("first_message_draft_count") or 0),
                "second_message_due": sum(
                    1
                    for target in (session.get("targets") or [])
                    if str(target.get("next_action") or "").upper() in {"FU-2", "FU-3", "FU-4"}
                ),
                "pipeline_assessed": len(session.get("targets") or [])
                + len(session.get("skipped") or []),
            }
        )(read_json(STATE_DIR / "followup_sessions" / f"{today}.json", {})),
        "withdrawals": withdrawal,
    }


def start_activity_watcher(requested: dict[str, Any]) -> dict[str, Any]:
    mode = str(requested.get("mode") or "full")
    if mode not in {"full", "prepare-only", "activity-only"}:
        raise ValueError("Unsupported Activity Check mode.")
    status = dashboard_stats(requested)
    if status["activity"]["is_running"]:
        return {"ok": False, "error": "Activity Check is already running through the watcher."}
    day = status["today"]
    subprocess.Popen(
        [
            sys.executable,
            str(WATCHER_SCRIPT_PATH),
            "--force-activity-date",
            day,
            "--force-activity-mode",
            mode,
        ],
        cwd=PROJECT_DIR,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "date": day, "mode": mode}


def launch_chrome_accounts(requested: dict[str, Any]) -> dict[str, Any]:
    target = str(requested.get("target") or "cdp1")
    scripts = {
        "cdp1": [SCRIPTS_DIR / "launch-chrome.sh"],
        "cdp2": [SCRIPTS_DIR / "launch-chrome-2.sh"],
        "both": [SCRIPTS_DIR / "launch-chrome.sh", SCRIPTS_DIR / "launch-chrome-2.sh"],
    }.get(target)
    if not scripts or any(not script.is_file() for script in scripts):
        raise ValueError("Requested Chrome launch script is unavailable.")
    for script in scripts:
        subprocess.Popen(
            ["bash", str(script)],
            cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return {"ok": True, "target": target}


def watcher_enabled() -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{WATCHER_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def set_watcher_enabled(requested: Any) -> dict[str, Any]:
    enabled = bool(requested if isinstance(requested, bool) else requested.get("enabled"))
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{WATCHER_LABEL}"
    if enabled:
        if not WATCHER_PLIST_SOURCE.is_file():
            raise ValueError("Watcher launch-agent template is unavailable.")
        WATCHER_PLIST_TARGET.parent.mkdir(parents=True, exist_ok=True)
        template = WATCHER_PLIST_SOURCE.read_text(encoding="utf-8")
        WATCHER_PLIST_TARGET.write_text(
            template.replace("__PROJECT_ROOT__", str(PROJECT_DIR)),
            encoding="utf-8",
        )
        subprocess.run(
            ["launchctl", "bootout", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(WATCHER_PLIST_TARGET)],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ["launchctl", "bootout", service],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and not watcher_enabled():
            return {"ok": True, "enabled": False}
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "launchctl failed").strip())
    return {"ok": True, "enabled": watcher_enabled()}


def invoke(channel: str, requested: dict[str, Any]) -> Any:
    global POST_ENGAGEMENT_ACTIVE_PROCESS
    if channel == "check-watcher-status":
        return watcher_enabled()
    if channel == "toggle-watcher":
        return set_watcher_enabled(requested)
    if channel == "read-obf-dashboard":
        return read_obf_dashboard()
    if channel == "read-post-engagement-dashboard":
        value = read_post_engagement_dashboard(str(requested.get("day") or "") or None)
        value["is_running"] = value.get("is_running", False) or bool(
            POST_ENGAGEMENT_ACTIVE_PROCESS and POST_ENGAGEMENT_ACTIVE_PROCESS.poll() is None
        )
        return value
    if channel == "add-post-engagement-source":
        return {
            "ok": True,
            "campaign": add_post_engagement_source(
                str(requested.get("url") or "").strip(),
                str(requested.get("day") or "") or None,
                str(requested.get("cdp_account") or "") or None,
            ),
        }
    if channel == "save-post-engagement-settings":
        return {"ok": True, "config": save_post_engagement_config(requested)}
    if channel == "archive-post-engagement-and-fresh":
        if POST_ENGAGEMENT_ACTIVE_PROCESS and POST_ENGAGEMENT_ACTIVE_PROCESS.poll() is None:
            return {
                "ok": False,
                "error": "Post Engagement is still running. Pause it before starting fresh.",
            }
        return {
            "ok": True,
            **archive_post_engagement_campaign(str(requested.get("day") or "") or None),
        }
    if channel == "pause-post-engagement-run":
        campaign = pause_post_engagement_campaign(str(requested.get("day") or "") or None)
        return {"ok": True, "campaign": campaign}
    if channel == "resume-post-engagement-run":
        campaign = resume_post_engagement_campaign(str(requested.get("day") or "") or None)
        if POST_ENGAGEMENT_ACTIVE_PROCESS and POST_ENGAGEMENT_ACTIVE_PROCESS.poll() is None:
            return {"ok": True, "campaign": campaign, "already_running": True}
        args = [sys.executable, str(SCRIPTS_DIR / "post_engagement.py"), "run", "--execute"]
        if requested.get("day"):
            args.extend(["--day", str(requested["day"])])
        log_path = STATE_DIR / "post_engagement" / "runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        command = (
            ["/usr/bin/caffeinate", "-i"] if Path("/usr/bin/caffeinate").exists() else []
        ) + args
        POST_ENGAGEMENT_ACTIVE_PROCESS = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return {"ok": True, "campaign": campaign, "pid": POST_ENGAGEMENT_ACTIVE_PROCESS.pid}
    if channel in {"start-post-engagement-dry-run", "start-post-engagement-run"}:
        if POST_ENGAGEMENT_ACTIVE_PROCESS and POST_ENGAGEMENT_ACTIVE_PROCESS.poll() is None:
            return {"ok": False, "error": "Post Engagement is already running."}
        args = [sys.executable, str(SCRIPTS_DIR / "post_engagement.py"), "run"]
        if requested.get("day"):
            args.extend(["--day", str(requested["day"])])
        if channel == "start-post-engagement-run":
            args.append("--execute")
        log_path = STATE_DIR / "post_engagement" / "runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        command = (
            ["/usr/bin/caffeinate", "-i"] if Path("/usr/bin/caffeinate").exists() else []
        ) + args
        POST_ENGAGEMENT_ACTIVE_PROCESS = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return {"ok": True, "pid": POST_ENGAGEMENT_ACTIVE_PROCESS.pid, "log_path": str(log_path)}
    if channel == "refresh-obf-source-cache":
        snapshot = refresh_obf_source_snapshot()
        return {
            "ok": True,
            "refreshed_at": snapshot["refreshed_at"],
            "dashboard": read_obf_dashboard(),
        }
    if channel == "start-obf-prepare":
        return start_obf_action("prepare", requested)
    if channel == "start-obf-execute":
        return start_obf_action("execute", requested)
    if channel == "save-obf-settings":
        return save_obf_settings(requested)
    if channel == "start-withdrawal-prepare":
        return start_withdrawal_action("prepare", requested)
    if channel == "start-withdrawal-execute":
        return start_withdrawal_action("execute", requested)
    if channel == "start-withdrawal-full":
        return start_withdrawal_action("full", requested)
    if channel == "stop-withdrawal":
        return stop_withdrawal_action()
    if channel == "read-stats":
        return dashboard_stats(requested)
    if channel == "launch-chrome":
        return launch_chrome_accounts(requested)
    if channel == "run-activity-watcher":
        return start_activity_watcher(requested)
    if channel == "read-lead-prep-dashboard":
        return read_lead_prep_dashboard()
    if channel == "read-job-discovery-dashboard":
        return read_job_discovery_dashboard()
    if channel == "save-job-discovery-settings":
        return save_job_discovery_settings(requested)
    if channel == "toggle-job-discovery-scheduler":
        return toggle_job_discovery_scheduler(requested)
    if channel == "start-job-discovery-run":
        return start_job_discovery_action("run")
    if channel == "start-job-discovery-reverify":
        return start_job_discovery_action("reverify")
    if channel == "refresh-lead-review-cache":
        config = read_lead_prep_config()
        target_date = str(config.get("dashboard_date_override") or "").strip()
        refresh_args = ("--target-date", target_date) if target_date else ()
        result = run_project_script("cache_lead_review_dashboard.py", *refresh_args)
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {
                "ok": False,
                "error": result["stderr"]
                or result["stdout"]
                or "Lead Review cache refresh failed.",
            }
        )
    if channel == "save-lead-research-progress":
        day = str(requested.get("day") or local_now().date().isoformat())
        run_id = str(requested.get("run_id") or "").strip()
        allowed = {"not_started", "in_progress", "completed", "skipped"}
        status = requested.get("status")
        status = status if status in allowed else "not_started"
        if not run_id:
            return {"ok": False, "error": "A Lead Review run ID is required."}
        state = read_json(LEAD_RESEARCH_PROGRESS_PATH, {"schema_version": 1, "days": {}})
        state["schema_version"] = 1
        state.setdefault("days", {}).setdefault(day, {})[run_id] = {
            "status": status,
            "notes": str(requested.get("notes") or "")[:1000],
            "updated_at": local_now().isoformat(),
        }
        write_json(LEAD_RESEARCH_PROGRESS_PATH, state)
        return {"ok": True, "progress": state["days"][day][run_id]}
    if channel == "update-lead-review-approval":
        row = int(requested.get("row") or 0)
        run_id = str(requested.get("run_id") or "").strip()
        if row < 2 or not run_id:
            return {
                "ok": False,
                "error": "The cached Lead Review row is invalid. Refresh the table and try again.",
            }
        result = run_project_script(
            "update_lead_review_approval.py",
            "--row",
            str(row),
            "--run-id",
            run_id,
            "--approved",
            "true" if requested.get("approved") else "false",
        )
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {"ok": False, "error": result["stderr"] or result["stdout"]}
        )
    if channel == "update-lead-review-use":
        row = int(requested.get("row") or 0)
        run_id = str(requested.get("run_id") or "").strip()
        use = str(requested.get("use") or "").strip()
        if row < 2 or not run_id:
            return {
                "ok": False,
                "error": "The cached Lead Review row is invalid. Refresh the table and try again.",
            }
        result = run_project_script(
            "update_lead_review_use.py",
            "--row",
            str(row),
            "--run-id",
            run_id,
            "--use",
            use,
        )
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {"ok": False, "error": result["stderr"] or result["stdout"]}
        )
    if channel == "update-lead-review-complete":
        group_row = int(requested.get("group_row") or 0)
        date_value = str(requested.get("date") or "").strip()
        if group_row < 2 or not date_value:
            return {
                "ok": False,
                "error": "The cached date group is invalid. Refresh the table and try again.",
            }
        result = run_project_script(
            "update_lead_review_group_status.py",
            "--group-row",
            str(group_row),
            "--date",
            date_value,
            "--complete",
            "true" if requested.get("complete") else "false",
        )
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {"ok": False, "error": result["stderr"] or result["stdout"]}
        )
    if channel == "sync-lead-review-group":
        payload_json = json.dumps(requested, separators=(",", ":"))
        result = run_project_script("sync_lead_review_group.py", "--payload-json", payload_json)
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {
                "ok": False,
                "error": result["stderr"] or result["stdout"] or "Review handoff failed.",
            }
        )
    if channel == "save-manual-lead-research":
        dashboard = read_lead_prep_dashboard()
        if not dashboard.get("processing", {}).get("manual_editable"):
            return {
                "ok": False,
                "error": "This group has already been bridged and is read-only.",
            }
        lead_id = str(requested.get("lead_id") or "").strip()
        allowed_ids = {
            str(lead.get("run_id") or "")
            for lead in dashboard.get("processing", {}).get("leads", [])
        }
        if lead_id not in allowed_ids:
            return {
                "ok": False,
                "error": "The selected lead is not in the active processing group.",
            }
        try:
            executives = normalize_manual_executives(requested.get("executives"))
            path, computation = create_manual_computation(dashboard)
            lead = next(
                item
                for item in computation.get("leads", [])
                if str(item.get("lead_id") or "") == lead_id
            )
            lead["executives"] = executives
            lead["status"] = "manual_ready" if manual_lead_ready(lead) else "manual_draft"
            lead["updated_at"] = local_now().isoformat()
            ready_count = sum(1 for item in computation.get("leads", []) if manual_lead_ready(item))
            computation["updated_at"] = local_now().isoformat()
            computation["status"] = (
                "manual_research_ready"
                if ready_count == len(computation.get("leads", []))
                else "manual_research_in_progress"
            )
            write_json(path, computation)
            return {"ok": True, "dashboard": read_lead_prep_dashboard()}
        except (ValueError, StopIteration) as error:
            return {"ok": False, "error": str(error)}
    if channel == "bridge-manual-lead-processing":
        dashboard = read_lead_prep_dashboard()
        processing = dashboard.get("processing", {})
        computation_file = str(processing.get("computation_file") or "")
        if processing.get("computation_mode") != "manual" or not computation_file:
            return {"ok": False, "error": "No manual research computation is ready to bridge."}
        if processing.get("bridged"):
            return {"ok": False, "error": "This manual research group has already been bridged."}
        if not processing.get("bridge_ready"):
            return {
                "ok": False,
                "error": "Complete P1 name, title, and LinkedIn for every lead before bridging.",
            }
        result = run_project_script(
            "lead_exec_research.py",
            "write-computation",
            "--computation-file",
            computation_file,
        )
        return (
            {"ok": True, "dashboard": read_lead_prep_dashboard()}
            if result["ok"]
            else {
                "ok": False,
                "error": result["stderr"] or result["stdout"] or "Manual research bridge failed.",
            }
        )
    if channel == "save-lead-prep-settings":
        current = read_lead_prep_config()
        next_config = {
            **current,
            "autonomous_prep_enabled": bool(requested.get("autonomous_prep_enabled")),
            "prep_time": str(requested.get("prep_time") or current["prep_time"]),
            "base_volume": max(
                1,
                min(
                    500,
                    int(requested.get("base_volume") or current["base_volume"]),
                ),
            ),
            "processing_batch_size": 30,
            "approval_gate_enabled": bool(requested.get("approval_gate_enabled")),
            "overlap_scan_mode": ("off" if requested.get("overlap_scan_mode") == "off" else "auto"),
            "fresh_volume_top_up_mode": (
                "off" if requested.get("fresh_volume_top_up_mode") == "off" else "auto"
            ),
        }
        write_json(LEAD_PREP_CONFIG_PATH, next_config)
        return {"ok": True, "config": next_config}
    raise ValueError(f"Unsupported browser API channel: {channel}")


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "operations-control-center",
                    "project": str(PROJECT_DIR),
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/invoke":
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            result = invoke(str(body.get("channel") or ""), body.get("value") or {})
            self._send_json({"ok": True, "result": result})
        except Exception as error:
            self._send_json(
                {"ok": False, "error": str(error)},
                HTTPStatus.BAD_REQUEST,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("MONITOR_HOST", "127.0.0.1"),
        choices=("127.0.0.1", "localhost", "::1"),
        help="Loopback-only bind address; this unauthenticated control API must not be public.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("MONITOR_PORT", "8765")))
    args = parser.parse_args()
    threading.Thread(target=refresh_obf_source_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(
        f"Operations Control Center available at http://{args.host}:{args.port}/dashboard.html",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
