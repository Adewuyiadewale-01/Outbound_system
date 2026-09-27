#!/usr/bin/env python3
"""Small checkpoint watcher for local orchestration workflows.

The watcher is intentionally short-lived. A scheduler such as launchd can run it
every few minutes; each invocation checks state, runs at most one due checkpoint,
records the result, and exits.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
HELPERS_DIR = ROOT / "helpers"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from runtime_environment import load_repo_env

load_repo_env()

STATE_DIR = ROOT / "state"
WATCHER_STATE_PATH = STATE_DIR / "orchestration_watcher.json"
WATCHER_LOCK_PATH = STATE_DIR / "orchestration_watcher.lock"
WATCHER_RUNTIME_PATH = STATE_DIR / "orchestration_watcher_runtime.json"
WATCHER_AWAKE_PATH = STATE_DIR / "orchestration_watcher_awake.json"
FOLLOWUP_SESSION_DIR = STATE_DIR / "followup_sessions"
FOLLOWUP_RUNNER = ROOT / "scripts" / "linkedin_followup_runner.py"
OBF_RUNNER = ROOT / "helpers" / "linkedin_outreach_session.py"
OBF_CONFIG_PATH = STATE_DIR / "obf_orchestration_config.json"
LEAD_PREP_CONFIG_PATH = STATE_DIR / "lead_prep_orchestration_config.json"
FOLLOWUP_CONFIG_PATH = STATE_DIR / "followup_orchestration_config.json"
WITHDRAWAL_CONFIG_PATH = STATE_DIR / "withdrawal_orchestration_config.json"
PYTHON = sys.executable or "python3"
DEFAULT_TIMEZONE = "Africa/Lagos"
LOCK_STALE_SECONDS = 2 * 60 * 60
RETRY_DELAYS_MINUTES = (5, 15, 30)
MAX_CHECKPOINT_ATTEMPTS = 1 + len(RETRY_DELAYS_MINUTES)
OPERATING_WINDOW_START = clock_time(hour=8, minute=15)
OPERATING_WINDOW_END = clock_time(hour=23, minute=30)
TERMINAL_CHECKPOINT_STATUSES = {
    "completed",
    "skipped",
    "failed_terminal",
    "interrupted_terminal",
    "paused_manual_stop",
    "cutoff_skipped",
    "needs_attention",
}

FOLLOWUP_CHECKPOINTS = [
    {
        "name": "prepare_and_day_pass",
        "due": "11:40",
        "commands": [
            [PYTHON, str(FOLLOWUP_RUNNER), "--prepare-only"],
            ["sleep", "300"],
            [PYTHON, str(FOLLOWUP_RUNNER), "--mode", "run"],
        ],
    },
]

ACTIVITY_CHECKPOINTS = [
    {
        "name": "activity_check_9pm",
        "due": "21:00",
        "commands": [],
    }
]
ACTIVITY_WORKERS_CONFIG_PATH = ROOT / "config" / "activity_workers.json"
OUTREACH_WORKERS_CONFIG_PATH = ROOT / "config" / "outreach_workers.json"
ACTIVITY_LANE_RUNNER = ROOT / "scripts" / "run_activity_lanes.py"
START_CUTOFF_BUFFER = timedelta(minutes=10)

DEFAULT_OBF_CONFIG = {
    "enabled": False,
    "prep_time": "08:25",
    "exec_time": "08:30",
    "window_end": "10:30",
    "max_sends": 30,
    "timezone": DEFAULT_TIMEZONE,
}
DEFAULT_LEAD_PREP_CONFIG = {
    "autonomous_prep_enabled": False,
    "prep_time": "14:00",
    "first_review_deadline": "18:00",
    "fallback_review_deadline": "22:00",
    "base_volume": 60,
    "processing_batch_size": 30,
    "approval_gate_enabled": False,
    "overlap_scan_mode": "auto",
    "fresh_volume_top_up_mode": "auto",
}
DEFAULT_FOLLOWUP_CONFIG = {"enabled": True}
DEFAULT_WITHDRAWAL_CONFIG = {"enabled": True}


def followup_session_path(day: str) -> Path:
    return FOLLOWUP_SESSION_DIR / f"{day}.json"


def activity_commands(
    day: str,
    limit: int = 0,
    mode: str = "full",
    runner_dry_run: bool = False,
) -> list[list[str]]:
    # The activity runner derives its prepared-session path from --date.
    command = [PYTHON, str(ROOT / "scripts" / "check_prefinal_activity.py"), "--date", day]
    if mode == "prepare-only":
        command.append("--prepare-only")
    elif mode == "activity-only":
        command.append("--activity-only")
    if limit > 0:
        command.extend(["--limit", str(limit)])
    if runner_dry_run:
        command.extend(["--dry-run", "--live-dry-run"])
    lane_config = read_json(ACTIVITY_WORKERS_CONFIG_PATH)
    sequential_lanes_enabled = bool(lane_config.get("enabled")) and not runner_dry_run
    if sequential_lanes_enabled and mode == "full":
        prepare_command = [
            PYTHON,
            str(ROOT / "scripts" / "check_prefinal_activity.py"),
            "--date",
            day,
            "--prepare-only",
        ]
        if limit > 0:
            prepare_command.extend(["--limit", str(limit)])
        return [prepare_command, [PYTHON, str(ACTIVITY_LANE_RUNNER), "--date", day]]
    if sequential_lanes_enabled and mode == "activity-only":
        return [[PYTHON, str(ACTIVITY_LANE_RUNNER), "--date", day]]
    return [command]


def obf_config() -> dict[str, Any]:
    payload = {**DEFAULT_OBF_CONFIG, **read_json(OBF_CONFIG_PATH)}
    payload["enabled"] = bool(payload.get("enabled"))
    payload["max_sends"] = max(1, min(30, int(payload.get("max_sends") or 30)))
    parse_hhmm(str(payload.get("prep_time") or "08:25"))
    parse_hhmm(str(payload.get("exec_time") or "08:30"))
    parse_hhmm(str(payload.get("window_end") or "10:30"))
    return payload


def lead_prep_config() -> dict[str, Any]:
    stored = read_json(LEAD_PREP_CONFIG_PATH)
    if not stored.get("overlap_scan_mode") and stored.get("reconciliation_mode"):
        stored["overlap_scan_mode"] = stored["reconciliation_mode"]
    payload = {**DEFAULT_LEAD_PREP_CONFIG, **stored}
    payload["autonomous_prep_enabled"] = bool(payload.get("autonomous_prep_enabled"))
    payload["base_volume"] = max(1, min(500, int(payload.get("base_volume") or 60)))
    payload["processing_batch_size"] = max(
        1, min(500, int(payload.get("processing_batch_size") or 30))
    )
    payload["approval_gate_enabled"] = bool(payload.get("approval_gate_enabled"))
    payload["overlap_scan_mode"] = "off" if payload.get("overlap_scan_mode") == "off" else "auto"
    payload["fresh_volume_top_up_mode"] = (
        "off" if payload.get("fresh_volume_top_up_mode") == "off" else "auto"
    )
    parse_hhmm(str(payload.get("prep_time") or "14:00"))
    parse_hhmm(str(payload.get("first_review_deadline") or "18:00"))
    parse_hhmm(str(payload.get("fallback_review_deadline") or "22:00"))
    return payload


def followup_config() -> dict[str, Any]:
    payload = {**DEFAULT_FOLLOWUP_CONFIG, **read_json(FOLLOWUP_CONFIG_PATH)}
    payload["enabled"] = bool(payload.get("enabled"))
    return payload


def withdrawal_config() -> dict[str, Any]:
    payload = {**DEFAULT_WITHDRAWAL_CONFIG, **read_json(WITHDRAWAL_CONFIG_PATH)}
    payload["enabled"] = bool(payload.get("enabled"))
    return payload


def lead_prep_commands(config: dict[str, Any]) -> list[list[str]]:
    command = [
        PYTHON,
        str(ROOT / "scripts" / "lead_exec_research.py"),
        "prepare-review",
        "--limit",
        str(config["base_volume"]),
        "--batch-size",
        str(config["processing_batch_size"]),
    ]
    if config.get("overlap_scan_mode") == "off":
        command.append("--disable-overlap-scan")
    if config.get("fresh_volume_top_up_mode") == "off":
        command.append("--disable-overlap-top-ups")
    return [command]


def lead_review_cache_commands() -> list[list[str]]:
    return [[PYTHON, str(ROOT / "scripts" / "cache_lead_review_dashboard.py")]]


def obf_commands(day: str, action: str, config: dict[str, Any]) -> list[list[str]]:
    if action == "prepare":
        return [[PYTHON, str(OBF_RUNNER), "prepare-8_30-session", "--date", day]]
    return [
        [
            PYTHON,
            str(ROOT / "scripts" / "run_outreach_lanes.py"),
            "--date",
            day,
            "--max-sends",
            str(config["max_sends"]),
        ]
    ]


def followup_run_command(day: str) -> list[str]:
    return [
        PYTHON,
        str(FOLLOWUP_RUNNER),
        "--mode",
        "run",
        "--session",
        str(followup_session_path(day)),
    ]


def withdrawal_commands(
    day: str,
    *,
    prepare: bool = True,
    limit: int = 0,
    resume: bool = False,
    cooldown_hours: float | None = None,
) -> list[list[str]]:
    session = STATE_DIR / "withdrawal_sessions" / f"{day}.json"
    commands: list[list[str]] = []
    if prepare:
        commands.extend(
            [
                [PYTHON, str(ROOT / "scripts" / "backfill_withdrawal_countdown.py")],
                [
                    PYTHON,
                    str(ROOT / "scripts" / "prepare_connection_withdrawals.py"),
                    "--date",
                    day,
                    "--session",
                    str(session),
                ],
                ["sleep", "300"],
            ]
        )
    withdraw = [
        PYTHON,
        str(ROOT / "scripts" / "run_withdrawal_lanes.py"),
        "--session",
        str(session),
        "--date",
        day,
    ]
    if limit > 0:
        withdraw.extend(["--limit", str(limit)])
    if resume:
        withdraw.append("--resume")
    if cooldown_hours is not None:
        withdraw.extend(["--cooldown-hours", str(cooldown_hours)])
    commands.append(withdraw)
    return commands


def clean(value: Any) -> str:
    return str(value or "").strip()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_hhmm(value: str) -> clock_time:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return clock_time(hour=hour, minute=minute)


def checkpoint_due(checkpoint: dict[str, Any], now: datetime) -> bool:
    return now.time() >= parse_hhmm(checkpoint["due"])


def followups_allowed(now: datetime) -> bool:
    # Python weekday: Monday=0, Sunday=6.
    return now.weekday() != 6


def today_key(now: datetime) -> str:
    return now.date().isoformat()


def day_state(state: dict[str, Any], workflow: str, day: str) -> dict[str, Any]:
    workflows = state.setdefault("workflows", {})
    workflow_state = workflows.setdefault(workflow, {})
    days = workflow_state.setdefault("days", {})
    current = days.setdefault(day, {})
    current.setdefault("checkpoints", {})
    return current


def checkpoint_done(day_payload: dict[str, Any], checkpoint_name: str) -> bool:
    checkpoint = day_payload.get("checkpoints", {}).get(checkpoint_name, {})
    return checkpoint.get("status") in TERMINAL_CHECKPOINT_STATUSES


def checkpoint_ready(day_payload: dict[str, Any], checkpoint_name: str, now: datetime) -> bool:
    checkpoint = day_payload.get("checkpoints", {}).get(checkpoint_name, {})
    if checkpoint.get("status") in TERMINAL_CHECKPOINT_STATUSES:
        return False
    retry_at = clean(checkpoint.get("next_retry_at"))
    if not retry_at:
        return True
    try:
        retry_dt = datetime.fromisoformat(retry_at)
    except ValueError:
        return True
    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=now.tzinfo)
    return now >= retry_dt


def cdp_port_open(host: str = "127.0.0.1", port: int = 18800, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_table() -> str:
    try:
        return subprocess.check_output(["ps", "-axo", "pid=,command="], text=True, timeout=5)
    except Exception:
        return ""


def related_runner_active() -> list[str]:
    current_pid = os.getpid()
    active = []
    for line in process_table().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if (
            "linkedin_outreach_session.py" in command
            or "run_outreach_lanes.py" in command
            or "linkedin_followup_runner.py" in command
            or "check_prefinal_activity.py" in command
            or "run_activity_lanes.py" in command
            or "lead_exec_research.py" in command
            or "withdraw_connections.py" in command
            or "run_withdrawal_lanes.py" in command
        ):
            active.append(stripped)
    return active


def should_wait_for_chrome() -> dict[str, Any]:
    active = related_runner_active()
    if active:
        return {"busy": True, "reason": "orchestration_process_active", "processes": active[:8]}
    if not cdp_port_open():
        return {"busy": False, "warning": "chrome_cdp_port_not_open"}
    return {"busy": False}


def close_stale_running_checkpoints(
    state: dict[str, Any], day: str, now: datetime
) -> list[dict[str, str]]:
    """Release checkpoints orphaned by a killed watcher so they can resume."""
    active = related_runner_active()
    if active:
        return []

    released: list[dict[str, str]] = []
    for workflow, workflow_payload in state.get("workflows", {}).items():
        day_payload = workflow_payload.get("days", {}).get(day, {})
        checkpoints = day_payload.get("checkpoints", {})
        for checkpoint_name, checkpoint in checkpoints.items():
            if checkpoint.get("status") != "running":
                continue
            checkpoint.update(
                {
                    "status": "resume_pending",
                    "interrupted_at": now.isoformat(timespec="seconds"),
                    "reason": "runner_process_not_found_after_interruption",
                    "next_retry_at": now.isoformat(timespec="seconds"),
                }
            )
            released.append({"workflow": workflow, "checkpoint": checkpoint_name})
    return released


def required_cdp_workers(workflow: str) -> list[dict[str, Any]]:
    config_path = (
        ACTIVITY_WORKERS_CONFIG_PATH if workflow == "activity" else OUTREACH_WORKERS_CONFIG_PATH
    )
    config = read_json(config_path)
    workers = [item for item in config.get("workers", []) if item.get("enabled", True)]
    if workflow == "followups":
        return workers[:1]
    return workers


def ensure_chrome_workers(workflow: str) -> dict[str, Any]:
    workers = required_cdp_workers(workflow)
    if not workers:
        workers = [
            {
                "id": "account_1",
                "cdp_host": "127.0.0.1",
                "cdp_port": 18800,
                "launch_script": "scripts/launch-chrome.sh",
            }
        ]
    launched: list[str] = []
    for worker in workers:
        host = clean(worker.get("cdp_host")) or "127.0.0.1"
        port = int(worker.get("cdp_port") or 18800)
        if cdp_port_open(host, port):
            continue
        script = Path(clean(worker.get("launch_script")))
        if not script.is_absolute():
            script = ROOT / script
        if not script.is_file():
            return {
                "ok": False,
                "reason": "chrome_launch_script_missing",
                "worker_id": worker.get("id"),
                "script": str(script),
            }
        try:
            subprocess.Popen(
                ["bash", str(script)],
                cwd=str(ROOT),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            launched.append(clean(worker.get("id")) or str(port))
        except Exception as exc:
            return {
                "ok": False,
                "reason": "chrome_launch_failed",
                "worker_id": worker.get("id"),
                "error": str(exc),
            }
    for _ in range(20):
        unhealthy = [
            worker
            for worker in workers
            if not cdp_port_open(
                clean(worker.get("cdp_host")) or "127.0.0.1", int(worker.get("cdp_port") or 18800)
            )
        ]
        if not unhealthy:
            return {
                "ok": True,
                "workers": [worker.get("id") for worker in workers],
                "launched": launched,
            }
        time.sleep(2)
    return {
        "ok": False,
        "reason": "chrome_cdp_preflight_failed",
        "unhealthy_workers": [worker.get("id") for worker in unhealthy],
        "launched": launched,
    }


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_operating_window_awake(now: datetime) -> dict[str, Any]:
    if not (OPERATING_WINDOW_START <= now.time() < OPERATING_WINDOW_END):
        return {"active": False, "reason": "outside_operating_window"}
    existing = read_json(WATCHER_AWAKE_PATH)
    pid = int(existing.get("pid") or 0)
    if process_alive(pid):
        return {"active": True, "pid": pid, "until": existing.get("until")}
    end_at = datetime.combine(now.date(), OPERATING_WINDOW_END, tzinfo=now.tzinfo)
    duration = max(60, int((end_at - now).total_seconds()))
    proc = subprocess.Popen(
        ["caffeinate", "-dimsu", "-t", str(duration)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    payload = {
        "pid": proc.pid,
        "started_at": now.isoformat(timespec="seconds"),
        "until": end_at.isoformat(timespec="seconds"),
    }
    write_json(WATCHER_AWAKE_PATH, payload)
    return {"active": True, **payload}


def notify_attention(workflow: str, checkpoint: str, reason: str) -> None:
    message = f"{workflow}: {checkpoint} needs attention ({reason})"
    try:
        subprocess.Popen(
            [
                "osascript",
                "-e",
                f'display notification {json.dumps(message)} with title "Outreach watcher"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


@contextmanager
def watcher_lock() -> Iterable[None]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if WATCHER_LOCK_PATH.exists():
        lock = read_json(WATCHER_LOCK_PATH)
        pid = int(lock.get("pid") or 0)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            raise RuntimeError(f"watcher lock active for pid {pid}")
        WATCHER_LOCK_PATH.unlink(missing_ok=True)
    write_json(
        WATCHER_LOCK_PATH,
        {
            "pid": os.getpid(),
            "started_epoch": now,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        yield
    finally:
        WATCHER_LOCK_PATH.unlink(missing_ok=True)


def command_timeout_seconds(command: list[str]) -> int:
    joined = " ".join(command)
    if "run_activity_lanes.py" in joined or "check_prefinal_activity.py" in joined:
        day = ""
        if "--date" in command:
            index = command.index("--date")
            if index + 1 < len(command):
                day = command[index + 1]
        session = read_json(STATE_DIR / "activity_sessions" / f"{day}.json") if day else {}
        targets = session.get("prepared_targets") or []
        fixed = sum(
            int(item.get("delay_sec") or 0)
            + int(item.get("activity_diversion_sec") or 0)
            + int(item.get("batch_gap_sec") or 0)
            for item in targets
        )
        estimated = fixed + len(targets) * 90 + 30 * 60
        return max(2 * 60 * 60, min(4 * 60 * 60, estimated))
    if "run_withdrawal_lanes.py" in joined or "withdraw_connections.py" in joined:
        return 4 * 60 * 60
    if "run_outreach_lanes.py" in joined or "linkedin_outreach_session.py" in joined:
        return 3 * 60 * 60
    if "linkedin_followup_runner.py" in joined or "lead_exec_research.py" in joined:
        return 2 * 60 * 60
    if " sleep " in f" {joined} ":
        return 15 * 60
    return 2 * 60 * 60


def terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def run_command(command: list[str], dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "command": command, "returncode": 0}
    started = datetime.now().isoformat(timespec="seconds")
    timeout_seconds = command_timeout_seconds(command)
    stdout_file = tempfile.NamedTemporaryFile(prefix="watcher-stdout-", suffix=".log", delete=False)
    stderr_file = tempfile.NamedTemporaryFile(prefix="watcher-stderr-", suffix=".log", delete=False)
    stdout_path = Path(stdout_file.name)
    stderr_path = Path(stderr_file.name)
    timed_out = False
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        stdout_file.close()
        stderr_file.close()
        deadline = time.monotonic() + timeout_seconds
        while proc.poll() is None:
            write_json(
                WATCHER_RUNTIME_PATH,
                {
                    "watcher_pid": os.getpid(),
                    "command_pid": proc.pid,
                    "command": command,
                    "started_at": started,
                    "heartbeat_at": datetime.now().isoformat(timespec="seconds"),
                    "timeout_seconds": timeout_seconds,
                },
            )
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_process_group(proc)
                break
            time.sleep(5)
        returncode = proc.wait() if proc.poll() is None else proc.returncode
        stdout_text = (
            stdout_path.read_text(encoding="utf-8", errors="replace")
            if stdout_path.exists()
            else ""
        )
        stderr_text = (
            stderr_path.read_text(encoding="utf-8", errors="replace")
            if stderr_path.exists()
            else ""
        )
        return {
            "ok": returncode == 0 and not timed_out,
            "command": command,
            "returncode": returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "started_at": started,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "stdout_tail": stdout_text[-4000:],
            "stderr_tail": stderr_text[-4000:],
        }
    except Exception as exc:
        if proc is not None:
            terminate_process_group(proc)
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "started_at": started,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
            "stderr_tail": str(exc),
        }
    finally:
        stdout_file.close()
        stderr_file.close()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        WATCHER_RUNTIME_PATH.unlink(missing_ok=True)


def run_checkpoint(checkpoint: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    results = []
    for command in checkpoint["commands"]:
        wrapped_command = ["caffeinate", "-dimsu"] + command
        result = run_command(wrapped_command, dry_run)
        results.append(result)
        if not result.get("ok"):
            break
    return {
        "name": checkpoint["name"],
        "due": checkpoint["due"],
        "ok": all(result.get("ok") for result in results),
        "commands": results,
    }


def checkpoint_failure_reason(result: dict[str, Any]) -> str:
    failed = next((item for item in result.get("commands", []) if not item.get("ok")), {})
    if failed.get("timed_out"):
        return "runtime_budget_exceeded"
    if isinstance(failed.get("returncode"), int) and failed.get("returncode") < 0:
        return "runner_interrupted"
    text = " ".join(
        [
            clean(failed.get("error")),
            clean(failed.get("stderr_tail")),
            clean(failed.get("stdout_tail")),
        ]
    ).lower()
    if any(
        marker in text
        for marker in (
            "nameresolutionerror",
            "failed to resolve",
            "ssleoferror",
            "connectionerror",
            "connection reset",
            "network is unreachable",
            "connecttimeout",
            "readtimeout",
            "temporarily unavailable",
        )
    ):
        return "network_failure"
    if '"termination_signal"' in text or '"interrupted": true' in text:
        return "runner_interrupted"
    if "login" in text or "checkpoint challenge" in text or "security challenge" in text:
        return "linkedin_authentication_required"
    if any(
        marker in text
        for marker in (
            "missing required",
            "schema error",
            "invalid_grant",
            "invalid_client",
            "credentials file not found",
        )
    ):
        return "configuration_or_schema_error"
    if "chrome" in text or "cdp" in text or "timeout" in text:
        return "browser_or_cdp_failure"
    return "runner_failed"


def failure_requires_attention_immediately(reason: str) -> bool:
    return reason in {"linkedin_authentication_required", "configuration_or_schema_error"}


def retry_at_for_attempt(now: datetime, attempt: int) -> datetime:
    delay_index = min(max(attempt - 1, 0), len(RETRY_DELAYS_MINUTES) - 1)
    return now + timedelta(minutes=RETRY_DELAYS_MINUTES[delay_index])


def extract_command_json(command_result: dict[str, Any]) -> dict[str, Any]:
    text = clean(command_result.get("stdout_tail"))
    if not text:
        return {}
    decoder = json.JSONDecoder()
    best: dict[str, Any] = {}
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            best = parsed
    return best


def latest_followup_runner_summary(checkpoint_payload: dict[str, Any]) -> dict[str, Any]:
    result = checkpoint_payload.get("result", {})
    commands = result.get("commands") or []
    if not commands:
        return {}
    return extract_command_json(commands[-1])


def followup_continuation_needed(checkpoint_payload: dict[str, Any]) -> bool:
    if checkpoint_payload.get("status") != "completed":
        return False
    summary = latest_followup_runner_summary(checkpoint_payload)
    capped_with_remaining = int(summary.get("remaining_due") or 0) > 0 and int(
        summary.get("sent") or 0
    ) >= int(summary.get("send_cap") or 20)
    return bool(summary.get("next_run_recommended")) or capped_with_remaining


def withdrawal_continuation_needed(checkpoint_payload: dict[str, Any], session_path: Path) -> bool:
    """Continue after a clean first pass left prepared targets unprocessed."""
    if checkpoint_payload.get("status") != "completed" or not session_path.exists():
        return False
    summary = latest_followup_runner_summary(checkpoint_payload)
    session = read_json(session_path)
    selected = int(session.get("selected_count") or 0)
    processed = int(summary.get("processed") or 0)
    return bool(
        selected
        and processed < selected
        and int(summary.get("confirmed") or 0) >= processed
        and int(summary.get("failed") or 0) == 0
        and int(summary.get("blocked") or 0) == 0
    )


def due_followup_checkpoint(
    now: datetime, day_payload: dict[str, Any], day: str
) -> dict[str, Any] | None:
    if not followups_allowed(now):
        return None
    base = FOLLOWUP_CHECKPOINTS[0]
    if checkpoint_due(base, now) and checkpoint_ready(day_payload, base["name"], now):
        return {
            **base,
            "commands": [
                [
                    PYTHON,
                    str(FOLLOWUP_RUNNER),
                    "--prepare-only",
                    "--session",
                    str(followup_session_path(day)),
                ],
                ["sleep", "300"],
                followup_run_command(day),
            ],
        }

    # One automatic continuation at most: initial pass + one capped pass.
    current_name = "followups_run_2"
    if not checkpoint_ready(day_payload, current_name, now):
        return None
    previous = day_payload.get("checkpoints", {}).get(base["name"], {})
    if not followup_continuation_needed(previous):
        return None
    prev_completed = previous.get("completed_at")
    if not prev_completed:
        return None
    prev_dt = datetime.fromisoformat(prev_completed)
    if prev_dt.tzinfo is None:
        prev_dt = prev_dt.replace(tzinfo=now.tzinfo)
    next_due_dt = prev_dt + timedelta(hours=2)
    if now < next_due_dt:
        return None
    return {
        "name": current_name,
        "due": next_due_dt.strftime("%H:%M"),
        "commands": [followup_run_command(day)],
    }


def due_obf_checkpoint(
    now: datetime,
    day_payload: dict[str, Any],
    day: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not config.get("enabled"):
        return None
    # Python weekday: Monday=0, Saturday=5, Sunday=6.  The runner has the
    # same guard so a dashboard/manual invocation cannot bypass this hold.
    if now.weekday() >= 5:
        return None
    existing_checkpoints = day_payload.get("checkpoints", {})
    recoverable_after_cutoff = any(
        item.get("status") in {"retry_waiting", "resume_pending"}
        for item in existing_checkpoints.values()
    )
    if (
        now.time() > parse_hhmm(str(config.get("window_end") or "10:30"))
        and not recoverable_after_cutoff
    ):
        return None
    checkpoints = [
        {
            "name": "obf_prepare",
            "due": str(config.get("prep_time") or "08:25"),
            "commands": obf_commands(day, "prepare", config),
        },
        {
            "name": "obf_execute",
            "due": str(config.get("exec_time") or "08:30"),
            "commands": obf_commands(day, "execute", config),
        },
    ]
    for checkpoint in checkpoints:
        if checkpoint["name"] == "obf_execute":
            prepared_path = STATE_DIR / "outreach_sequences" / f"{day}-prepared.json"
            prepared = read_json(prepared_path)
            if not prepared.get("ready"):
                continue
        if checkpoint_due(checkpoint, now) and checkpoint_ready(
            day_payload, checkpoint["name"], now
        ):
            return checkpoint
    return None


def due_activity_checkpoint(now: datetime, day_payload: dict[str, Any]) -> dict[str, Any] | None:
    for checkpoint in ACTIVITY_CHECKPOINTS:
        if checkpoint_due(checkpoint, now) and checkpoint_ready(
            day_payload, checkpoint["name"], now
        ):
            return {**checkpoint, "commands": activity_commands(today_key(now))}
    return None


def due_forced_activity_retry(
    now: datetime,
    day_payload: dict[str, Any],
    day: str,
) -> dict[str, Any] | None:
    """Resume a watcher-owned manual activity run on later launchd ticks.

    A forced run prepares its durable session on the first attempt.  Subsequent
    attempts must reuse that session, otherwise an ordinary five-minute watcher
    tick cannot rediscover the manual checkpoint and its retry timer is inert.
    """
    name = "activity_check_forced"
    existing = day_payload.get("checkpoints", {}).get(name, {})
    if existing.get("status") not in {"retry_waiting", "resume_pending"}:
        return None
    if not checkpoint_ready(day_payload, name, now):
        return None
    session_path = STATE_DIR / "activity_sessions" / f"{day}.json"
    if not session_path.exists():
        return None
    return {
        "name": name,
        "due": "manual-retry",
        "commands": activity_commands(day, mode="activity-only"),
    }


def due_lead_prep_checkpoint(
    now: datetime,
    day_payload: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    prep_due = str(config.get("prep_time") or "14:00")
    prep_clock = datetime.combine(now.date(), parse_hhmm(prep_due), tzinfo=now.tzinfo)
    cache_due = (prep_clock + timedelta(minutes=10)).strftime("%H:%M")
    checkpoints: list[dict[str, Any]] = []
    if config.get("autonomous_prep_enabled"):
        checkpoints.append(
            {
                "name": "lead_prep_prepare",
                "due": prep_due,
                "commands": lead_prep_commands(config),
            }
        )
    checkpoints.append(
        {
            "name": "lead_review_daily_cache",
            "due": cache_due,
            "commands": lead_review_cache_commands(),
        }
    )
    for checkpoint in checkpoints:
        if checkpoint_due(checkpoint, now) and checkpoint_ready(
            day_payload, checkpoint["name"], now
        ):
            return checkpoint
    return None


def due_withdrawal_checkpoint(
    now: datetime, day_payload: dict[str, Any], day: str
) -> dict[str, Any] | None:
    if not withdrawal_config().get("enabled"):
        return None
    base_name = "withdrawals_run_1"
    session_path = STATE_DIR / "withdrawal_sessions" / f"{day}.json"
    if checkpoint_ready(day_payload, base_name, now):
        existing = day_payload.get("checkpoints", {}).get(base_name, {})
        resuming = (
            existing.get("status") in {"retry_waiting", "resume_pending"} and session_path.exists()
        )
        checkpoint = {
            "name": base_name,
            "due": "18:00",
            "commands": withdrawal_commands(
                day,
                prepare=not resuming,
                limit=30,
                resume=resuming,
                cooldown_hours=0 if resuming else None,
            ),
        }
        if checkpoint_due(checkpoint, now):
            return checkpoint
        return None

    # One automatic continuation at most: continue the same prepared session
    # after the first pass, skipping already confirmed withdrawals.
    current_name = "withdrawals_run_2"
    if not checkpoint_ready(day_payload, current_name, now):
        return None
    previous = day_payload.get("checkpoints", {}).get(base_name, {})
    if not withdrawal_continuation_needed(previous, session_path):
        return None
    prev_completed = previous.get("completed_at")
    if not prev_completed:
        return None
    prev_dt = datetime.fromisoformat(prev_completed)
    if prev_dt.tzinfo is None:
        prev_dt = prev_dt.replace(tzinfo=now.tzinfo)
    next_due_dt = datetime.combine(now.date(), parse_hhmm("19:30"), tzinfo=now.tzinfo)
    if now < next_due_dt:
        return None
    return {
        "name": current_name,
        "due": next_due_dt.strftime("%H:%M"),
        "commands": withdrawal_commands(
            day, prepare=False, limit=20, resume=True, cooldown_hours=0
        ),
    }


def next_workflow_window(now: datetime, active_workflow: str) -> dict[str, Any] | None:
    """Return the next other workflow's scheduled base window."""
    windows = {
        "obf": str(obf_config().get("prep_time") or "08:25"),
        "followups": "11:40",
        "lead_prep": str(lead_prep_config().get("prep_time") or "14:00"),
        "withdrawals": "18:00",
        "activity": "21:00",
    }
    candidates = []
    for workflow, due in windows.items():
        if workflow == active_workflow:
            continue
        candidate = datetime.combine(now.date(), parse_hhmm(due), tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append((candidate, workflow, due))
    if not candidates:
        return None
    due_at, workflow, due = min(candidates, key=lambda item: item[0])
    return {"workflow": workflow, "due": due, "due_at": due_at}


def start_cutoff(now: datetime, active_workflow: str) -> dict[str, Any] | None:
    next_window = next_workflow_window(now, active_workflow)
    if not next_window:
        return None
    cutoff_at = next_window["due_at"] - START_CUTOFF_BUFFER
    if now >= cutoff_at:
        return {**next_window, "cutoff_at": cutoff_at}
    return None


def status_payload(now: datetime, state: dict[str, Any]) -> dict[str, Any]:
    day = today_key(now)
    payload = day_state(state, "followups", day)
    due = due_followup_checkpoint(now, payload, day)
    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
        "workflow": "followups",
        "today": day,
        "next_due_checkpoint": due["name"] if due else "",
        "checkpoints": payload.get("checkpoints", {}),
        "chrome": should_wait_for_chrome(),
        "alerts": [item for item in state.get("alerts", []) if not item.get("acknowledged")],
        "awake": read_json(WATCHER_AWAKE_PATH),
        "session_exists": (FOLLOWUP_SESSION_DIR / f"{day}.json").exists(),
    }


def tick(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(args.timezone))
    state = read_json(WATCHER_STATE_PATH)
    day = today_key(now)

    if args.status:
        return status_payload(now, state)

    forced_activity = bool(args.force_activity_date)
    forced_lead_prep = bool(args.force_lead_prep_date)
    if forced_activity and forced_lead_prep:
        return {
            "ok": False,
            "status": "multiple_forced_workflows",
            "reason": "Use only one forced workflow option per watcher run.",
        }
    if forced_activity:
        try:
            day = date.fromisoformat(args.force_activity_date).isoformat()
        except ValueError:
            return {
                "ok": False,
                "status": "invalid_force_activity_date",
                "reason": "--force-activity-date must use YYYY-MM-DD",
            }

    if forced_lead_prep:
        try:
            requested_day = date.fromisoformat(args.force_lead_prep_date).isoformat()
        except ValueError:
            return {
                "ok": False,
                "status": "invalid_force_lead_prep_date",
                "reason": "--force-lead-prep-date must use YYYY-MM-DD",
            }
        if requested_day != today_key(now):
            return {
                "ok": False,
                "status": "force_lead_prep_date_must_be_today",
                "reason": "Lead Prep writes today's grouped review batch, so a forced date must be today.",
            }
        day = requested_day

    forced_run = forced_activity or forced_lead_prep
    awake = (
        ensure_operating_window_awake(now)
        if not forced_run
        else {"active": True, "reason": "forced_run"}
    )

    released = close_stale_running_checkpoints(state, day, now)

    if forced_activity:
        active_payload = day_state(state, "activity", day)
        due = {
            "name": "activity_check_forced",
            "due": "manual",
            "commands": activity_commands(
                day,
                args.activity_limit,
                args.force_activity_mode,
                args.force_runner_dry_run,
            ),
        }
        active_workflow = "activity"
    elif forced_lead_prep:
        active_payload = day_state(state, "lead_prep", day)
        due = {
            "name": "lead_prep_prepare",
            "due": "manual-rerun",
            "commands": lead_prep_commands(lead_prep_config()),
        }
        active_workflow = "lead_prep"
    else:
        due = None
        active_payload = {}
        active_workflow = ""

    if not forced_run:
        config = obf_config()
        obf_payload = day_state(state, "obf", day)
        due = due_obf_checkpoint(now, obf_payload, day, config)
        active_payload = obf_payload
        active_workflow = "obf"

    if not due and followup_config().get("enabled"):
        fu_payload = day_state(state, "followups", day)
        due = due_followup_checkpoint(now, fu_payload, day)
        active_payload = fu_payload
        active_workflow = "followups"

    if not due:
        lead_config = lead_prep_config()
        lead_payload = day_state(state, "lead_prep", day)
        due = due_lead_prep_checkpoint(now, lead_payload, lead_config)
        if due:
            active_payload = lead_payload
            active_workflow = "lead_prep"

    if not due:
        wd_payload = day_state(state, "withdrawals", day)
        due = due_withdrawal_checkpoint(now, wd_payload, day)
        if due:
            active_payload = wd_payload
            active_workflow = "withdrawals"

    if not due:
        act_payload = day_state(state, "activity", day)
        due = due_forced_activity_retry(now, act_payload, day)
        if due:
            active_payload = act_payload
            active_workflow = "activity"

    if not due:
        act_payload = day_state(state, "activity", day)
        due = due_activity_checkpoint(now, act_payload)
        if due:
            active_payload = act_payload
            active_workflow = "activity"

    if not due:
        state["last_tick_at"] = now.isoformat(timespec="seconds")
        write_json(WATCHER_STATE_PATH, state)
        return {
            "ok": True,
            "status": "idle",
            "reason": "no_due_checkpoint",
            "released_interrupted": released,
            "awake": awake,
            **status_payload(now, state),
        }

    preflight_result: dict[str, Any] | None = None
    # OBF preparation is a Sheets-only operation.  Starting either LinkedIn
    # browser before it passes its control-row checks is unnecessary and can
    # leave an idle CDP window open after a blocked preparation.
    requires_chrome = active_workflow in {"activity", "followups", "withdrawals"} or (
        active_workflow == "obf" and due["name"] == "obf_execute"
    )
    if requires_chrome:
        chrome = should_wait_for_chrome()
        if chrome.get("busy"):
            active_payload["checkpoints"][due["name"]] = {
                "status": "waiting",
                "reason": chrome.get("reason"),
                "checked_at": now.isoformat(timespec="seconds"),
                "detail": chrome,
            }
            state["last_tick_at"] = now.isoformat(timespec="seconds")
            write_json(WATCHER_STATE_PATH, state)
            return {
                "ok": True,
                "status": "waiting",
                "checkpoint": due["name"],
                "chrome": chrome,
                "released_interrupted": released,
            }

        chrome_preflight = ensure_chrome_workers(active_workflow)
        if not chrome_preflight.get("ok"):
            preflight_result = {
                "name": due["name"],
                "due": due["due"],
                "ok": False,
                "commands": [
                    {
                        "ok": False,
                        "returncode": None,
                        "error": chrome_preflight.get("reason"),
                        "stderr_tail": json.dumps(chrome_preflight, ensure_ascii=True),
                    }
                ],
            }

    previous_checkpoint = active_payload.get("checkpoints", {}).get(due["name"], {})
    attempt = int(previous_checkpoint.get("attempt") or 0) + 1
    active_payload["checkpoints"][due["name"]] = {
        "status": "running",
        "started_at": now.isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "attempt": attempt,
        "previous_status": previous_checkpoint.get("status"),
    }
    state["last_tick_at"] = now.isoformat(timespec="seconds")
    write_json(WATCHER_STATE_PATH, state)

    result = preflight_result or run_checkpoint(due, args.dry_run)
    completed_now = datetime.now(ZoneInfo(args.timezone))
    completed_at = completed_now.isoformat(timespec="seconds")
    checkpoint_result: dict[str, Any] = {
        "started_at": active_payload["checkpoints"][due["name"]].get("started_at"),
        "completed_at": completed_at,
        "dry_run": bool(args.dry_run),
        "attempt": attempt,
        "result": result,
    }
    if result.get("ok"):
        checkpoint_result["status"] = "completed"
    else:
        reason = checkpoint_failure_reason(result)
        checkpoint_result["reason"] = reason
        if not failure_requires_attention_immediately(reason) and attempt < MAX_CHECKPOINT_ATTEMPTS:
            checkpoint_result["status"] = "retry_waiting"
            checkpoint_result["next_retry_at"] = retry_at_for_attempt(
                completed_now, attempt
            ).isoformat(timespec="seconds")
        else:
            checkpoint_result["status"] = "needs_attention"
            alerts = state.setdefault("alerts", [])
            alert_id = f"{day}:{active_workflow}:{due['name']}"
            alerts[:] = [item for item in alerts if item.get("id") != alert_id]
            alerts.append(
                {
                    "id": alert_id,
                    "day": day,
                    "workflow": active_workflow,
                    "checkpoint": due["name"],
                    "reason": reason,
                    "created_at": completed_at,
                    "acknowledged": False,
                }
            )
            notify_attention(active_workflow, due["name"], reason)
    active_payload["checkpoints"][due["name"]] = checkpoint_result
    state["last_tick_at"] = completed_at
    write_json(WATCHER_STATE_PATH, state)
    return {
        "ok": bool(result.get("ok")),
        "status": active_payload["checkpoints"][due["name"]]["status"],
        "checkpoint": due["name"],
        "released_interrupted": released,
        "awake": awake,
        "result": result,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one short orchestration watcher tick.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--force-activity-date",
        default="",
        metavar="YYYY-MM-DD",
        help="One-shot manual Activity Check through the watcher path; bypasses the 9 PM schedule.",
    )
    parser.add_argument(
        "--force-lead-prep-date",
        default="",
        metavar="YYYY-MM-DD",
        help="One-shot manual Lead Prep rerun through the watcher path; only today's date is accepted.",
    )
    parser.add_argument(
        "--activity-limit",
        type=int,
        default=0,
        help="Optional target limit for --force-activity-date (0 means the full prepared session).",
    )
    parser.add_argument(
        "--force-activity-mode",
        choices=("full", "prepare-only", "activity-only"),
        default="activity-only",
        help="Activity runner mode for --force-activity-date (default: activity-only, so a session is never rebuilt by accident).",
    )
    parser.add_argument(
        "--force-runner-dry-run",
        action="store_true",
        help="Run live Activity reads but prevent runner state writes and bridges during a forced watcher test.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with watcher_lock():
            result = tick(args)
    except RuntimeError as exc:
        result = {"ok": True, "status": "locked", "reason": str(exc)}
    print(json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
