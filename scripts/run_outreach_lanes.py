#!/usr/bin/env python3
"""Execute the immutable outreach queue through its designated CDP accounts.

Preparation owns the split: every row is assigned from Primary Lane to exactly
one worker in the normal prepared-state file.  This runner only honours that
frozen assignment and runs the populated lanes sequentially.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state"
WORKERS_PATH = STATE_DIR / "outreach_workers.json"
OUTREACH_RUNNER = ROOT / "helpers" / "linkedin_outreach_session.py"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def prepared_path(day: str) -> Path:
    return STATE_DIR / "outreach_sequences" / f"{day}-prepared.json"


def active_workers(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("enabled"):
        raise ValueError("Sequential outreach lanes are disabled in state/outreach_workers.json.")
    workers = [dict(item) for item in config.get("workers", []) if item.get("enabled", True)]
    if not workers:
        raise ValueError("No enabled outreach account is configured.")
    seen_ids, seen_lanes = set(), set()
    for worker in workers:
        worker_id = str(worker.get("id") or "").strip()
        lane = str(worker.get("primary_lane") or "").strip()
        if not worker_id or not lane:
            raise ValueError("Every outreach account needs id and primary_lane.")
        if worker_id in seen_ids or lane in seen_lanes:
            raise ValueError("Outreach worker ids and Primary Lane mappings must each be unique.")
        port = int(worker.get("cdp_port") or 0)
        if not 1 <= port <= 65535:
            raise ValueError(f"Outreach account {worker_id} has an invalid CDP port.")
        if not str(worker.get("profile_dir") or "").strip():
            raise ValueError(f"Outreach account {worker_id} needs its own profile_dir.")
        worker["id"] = worker_id
        worker["primary_lane"] = lane
        worker["cdp_host"] = str(worker.get("cdp_host") or "127.0.0.1")
        worker["cdp_port"] = port
        seen_ids.add(worker_id)
        seen_lanes.add(lane)
    return workers


def lane_counts(prepared: dict[str, Any], workers: Sequence[dict[str, Any]]) -> dict[str, int]:
    queue = prepared.get("queue") or []
    if not prepared.get("ready") or not queue:
        raise ValueError("No ready shared prepared queue exists for this date.")
    plan = prepared.get("worker_plan") or {}
    if not plan.get("enabled"):
        raise ValueError(
            "Prepared queue has no account assignment. Run preparation again before execution."
        )
    allowed = {worker["id"] for worker in workers}
    counts = {worker["id"]: 0 for worker in workers}
    for prospect in queue:
        worker_id = str(prospect.get("worker_id") or "").strip()
        if worker_id not in allowed:
            raise ValueError(
                f"Prepared prospect {prospect.get('id') or prospect.get('company') or 'unknown'} "
                "does not have a valid assigned account. Re-run preparation."
            )
        counts[worker_id] += 1
    return counts


def cdp_healthy(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=5) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("Browser"))
    except Exception:
        return False


def managed_chrome_pids(worker: dict[str, Any]) -> list[int]:
    needle = f"--user-data-dir={worker['profile_dir']}"
    try:
        rows = subprocess.check_output(
            ["ps", "-axo", "pid=,command="], text=True, timeout=5
        ).splitlines()
    except Exception:
        return []
    pids: list[int] = []
    for row in rows:
        pid_text, _, command = row.strip().partition(" ")
        if "Google Chrome.app/Contents/MacOS/Google Chrome" not in command or needle not in command:
            continue
        try:
            pids.append(int(pid_text))
        except ValueError:
            pass
    return pids


def ensure_worker_cdp(worker: dict[str, Any]) -> dict[str, Any]:
    if cdp_healthy(worker["cdp_host"], worker["cdp_port"]):
        return {"ok": True, "action": "cdp_already_running"}
    launch = ROOT / str(worker.get("launch_script") or "")
    if not launch.is_file():
        raise RuntimeError(f"Launch script for {worker['id']} is missing: {launch}")
    # A stale managed profile can own the port without answering CDP.  Stop only
    # that exact profile, then launch it detached just as the watcher does.
    for pid in managed_chrome_pids(worker):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 20
    while managed_chrome_pids(worker) and time.time() < deadline:
        time.sleep(1)
    if managed_chrome_pids(worker):
        raise RuntimeError(f"Managed Chrome for {worker['id']} did not stop cleanly.")
    subprocess.Popen(
        ["bash", str(launch)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(2)
        if cdp_healthy(worker["cdp_host"], worker["cdp_port"]):
            return {"ok": True, "action": "browser_started"}
    raise RuntimeError(f"CDP for {worker['id']} did not become healthy.")


def parse_result(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "ok" in value and "status" in value:
            return value
    return {}


def run_lane(
    day: str, prepared: Path, worker: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(OUTREACH_RUNNER),
        "run",
        "--date",
        day,
        "--require-prepared-session",
        "--prepared-path",
        str(prepared),
        "--worker-id",
        worker["id"],
        "--no-notes",
        "--max-sends",
        str(args.max_sends),
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.verify_connection_modal:
        command.append("--verify-connection-modal")
    env = {
        **os.environ,
        "LINKEDIN_CDP_HOST": worker["cdp_host"],
        "LINKEDIN_CDP_PORT": str(worker["cdp_port"]),
    }
    started_at = datetime.now().isoformat(timespec="seconds")
    process = subprocess.Popen(
        command, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    try:
        stdout, stderr = process.communicate()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
        return {
            "worker_id": worker["id"],
            "interrupted": True,
            "returncode": process.returncode,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
    summary = parse_result(stdout) or parse_result(stderr)
    return {
        "worker_id": worker["id"],
        "primary_lane": worker["primary_lane"],
        "cdp_port": worker["cdp_port"],
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "ok": process.returncode == 0 and summary.get("ok") is True,
        "returncode": process.returncode,
        **(
            {
                "termination_signal": signal.Signals(-process.returncode).name,
                "interrupted": True,
                "termination_source": "unknown_external_signal",
                "worker_pid": process.pid,
            }
            if process.returncode < 0
            else {}
        ),
        "summary": summary,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run frozen outreach lanes sequentially through their mapped CDP accounts."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate split and runner selection without touching Chrome or sending.",
    )
    parser.add_argument(
        "--verify-connection-modal",
        action="store_true",
        help="Open and dismiss invite modals without sending or reporting writes.",
    )
    parser.add_argument("--max-sends", type=int, default=30)
    parser.add_argument("--normal-gap-minutes", type=int, default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.max_sends <= 30:
        raise SystemExit("--max-sends must be between 1 and 30.")
    day = date.fromisoformat(args.date).isoformat()
    workers = active_workers(read_json(WORKERS_PATH))
    state_path = prepared_path(day)
    prepared = read_json(state_path)
    counts = lane_counts(prepared, workers)
    scheduled = [worker for worker in workers if counts[worker["id"]] > 0]
    gap = int(
        read_json(WORKERS_PATH).get("normal_handoff_gap_minutes", 10)
        if args.normal_gap_minutes is None
        else args.normal_gap_minutes
    )
    if not 0 <= gap <= 30:
        raise SystemExit("--normal-gap-minutes must be between 0 and 30.")
    plan = {
        "date": day,
        "prepared_path": str(state_path),
        "mode": "sequential_by_primary_lane",
        "normal_handoff_gap_minutes": gap,
        "workers": [
            {
                "id": worker["id"],
                "primary_lane": worker["primary_lane"],
                "cdp_port": worker["cdp_port"],
                "prospects": counts[worker["id"]],
            }
            for worker in scheduled
        ],
    }
    if args.plan_only:
        print(json.dumps({"ok": True, "plan_only": True, **plan}, indent=2))
        return 0
    results = []
    for index, worker in enumerate(scheduled):
        if index:
            time.sleep(gap * 60)
        try:
            recovery = (
                {"ok": True, "action": "dry_run_no_cdp"}
                if args.dry_run
                else ensure_worker_cdp(worker)
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "paused_for_browser_recovery",
                        **plan,
                        "results": results,
                        "worker_id": worker["id"],
                        "error": str(exc),
                    },
                    indent=2,
                )
            )
            return 1
        lane_result = run_lane(day, state_path, worker, args)
        lane_result["cdp_recovery"] = recovery
        results.append(lane_result)
        summary = lane_result.get("summary") or {}
        if lane_result.get("interrupted"):
            print(
                json.dumps(
                    {"ok": False, "status": "interrupted", **plan, "results": results}, indent=2
                )
            )
            return 130
        if not lane_result.get("ok") or not summary.get("ok", True):
            print(
                json.dumps(
                    {"ok": False, "status": "paused_for_lane_failure", **plan, "results": results},
                    indent=2,
                )
            )
            return 1
    print(json.dumps({"ok": True, "status": "completed", **plan, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
