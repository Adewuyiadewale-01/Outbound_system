#!/usr/bin/env python3
"""Run one prepared withdrawal session through its frozen Primary Lane CDP accounts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
WORKERS = STATE / "outreach_workers.json"
WITHDRAW = ROOT / "scripts" / "withdraw_connections.py"


def read(path: Path):
    return json.loads(path.read_text())


def healthy(worker):
    try:
        with urllib.request.urlopen(
            f"http://{worker['cdp_host']}:{worker['cdp_port']}/json/version", timeout=5
        ) as response:
            return bool(json.loads(response.read()).get("Browser"))
    except Exception:
        return False


def ensure_cdp(worker):
    if healthy(worker):
        return "already_running"
    launch = ROOT / worker["launch_script"]
    if not launch.is_file():
        raise RuntimeError(f"Missing launch script for {worker['id']}: {launch}")
    subprocess.Popen(
        ["bash", str(launch)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(2)
        if healthy(worker):
            return "started"
    raise RuntimeError(f"CDP unavailable for {worker['id']} on {worker['cdp_port']}")


def lane_session(source, worker, day):
    lane = worker["primary_lane"]
    plan = source["runtime_plan"]
    batches = []
    for batch in plan.get("batches", []):
        targets = [
            target for target in batch.get("targets", []) if target.get("primary_lane") == lane
        ]
        if targets:
            copy = dict(batch)
            copy["targets"] = targets
            copy["batch_size"] = len(targets)
            batches.append(copy)
    payload = dict(source)
    payload["runtime_plan"] = dict(
        plan, batches=batches, queue=[t for b in batches for t in b["targets"]]
    )
    path = STATE / "withdrawal_sessions" / f"{day}-{worker['id']}-lane.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path, len(payload["runtime_plan"]["queue"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--session", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--normal-gap-minutes", type=int, default=None)
    args = parser.parse_args()
    day = date.fromisoformat(args.date).isoformat()
    source_path = (
        Path(args.session) if args.session else STATE / "withdrawal_sessions" / f"{day}.json"
    )
    source = read(source_path)
    workers = [dict(w) for w in read(WORKERS).get("workers", []) if w.get("enabled", True)]
    by_lane = {w.get("primary_lane"): w for w in workers}
    queue = source.get("runtime_plan", {}).get("queue", [])
    missing = sorted(
        {
            str(t.get("primary_lane") or "")
            for t in queue
            if str(t.get("primary_lane") or "") not in by_lane
        }
    )
    if missing:
        raise SystemExit(
            f"Withdrawal queue has unmapped Primary Lane values: {', '.join(missing)}. Re-prepare after assigning lanes."
        )
    scheduled = [
        w for w in workers if any(t.get("primary_lane") == w["primary_lane"] for t in queue)
    ]
    gap = int(
        read(WORKERS).get("normal_handoff_gap_minutes", 10)
        if args.normal_gap_minutes is None
        else args.normal_gap_minutes
    )
    results = []
    for index, worker in enumerate(scheduled):
        if index:
            time.sleep(gap * 60)
        recovery = "dry_run" if args.dry_run else ensure_cdp(worker)
        session, count = lane_session(source, worker, day)
        env = {
            **os.environ,
            "LINKEDIN_CDP_HOST": worker.get("cdp_host", "127.0.0.1"),
            "LINKEDIN_CDP_PORT": str(worker["cdp_port"]),
        }
        command = [
            sys.executable,
            str(WITHDRAW),
            "--date",
            day,
            "--session",
            str(session),
            "--cooldown-hours",
            "0",
        ]
        if args.resume:
            command.append("--resume")
        if args.dry_run:
            command.append("--dry-run")
        run = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
        results.append(
            {
                "worker_id": worker["id"],
                "primary_lane": worker["primary_lane"],
                "cdp_port": worker["cdp_port"],
                "targets": count,
                "cdp_recovery": recovery,
                "ok": run.returncode == 0,
                "stdout_tail": run.stdout[-4000:],
                "stderr_tail": run.stderr[-4000:],
            }
        )
        if run.returncode:
            print(
                json.dumps(
                    {"ok": False, "status": "paused_for_lane_failure", "results": results}, indent=2
                )
            )
            return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": "completed",
                "mode": "sequential_by_primary_lane",
                "results": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
