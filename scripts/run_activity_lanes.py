#!/usr/bin/env python3
"""Run an already-prepared Activity Check through two sequential CDP lanes.

The lanes never overlap: the second one starts only after the first exits and
the configured gap has elapsed.  Activity results share the existing prepared
session; the final bridge runs once, after both lanes complete.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
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
WORKER_CONFIG_PATH = STATE_DIR / "activity_workers.json"
ACTIVITY_SCRIPT = ROOT / "scripts" / "check_prefinal_activity.py"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temp.replace(path)


def session_path(day: str) -> Path:
    return STATE_DIR / "activity_sessions" / f"{day}.json"


def active_workers(config: dict[str, Any]) -> list[dict[str, Any]]:
    workers = [dict(worker) for worker in config.get("workers", []) if worker.get("enabled")]
    if len(workers) != 2:
        raise ValueError("Exactly two enabled activity workers are required for sequential lanes.")
    ids = [str(worker.get("id") or "").strip() for worker in workers]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("Each enabled activity worker needs a unique id.")
    for worker in workers:
        port = int(worker.get("cdp_port") or 0)
        if not 1 <= port <= 65535:
            raise ValueError(f"Worker {worker['id']} has an invalid cdp_port.")
        worker["cdp_port"] = port
        worker["cdp_host"] = str(worker.get("cdp_host") or "127.0.0.1")
        if not str(worker.get("profile_dir") or "").strip():
            raise ValueError(f"Worker {worker['id']} needs a profile_dir for controlled recovery.")
    return workers


def assign_prepared_targets(
    state: dict[str, Any], workers: Sequence[dict[str, Any]]
) -> dict[str, int]:
    """Assign whole source rows to lanes so P1/P2 never split across accounts."""
    prepared = state.get("prepared_targets") or []
    if not prepared:
        raise ValueError("Prepared activity session has no targets.")
    valid_ids = {worker["id"] for worker in workers}
    existing = {
        str(target.get("worker_id") or "") for target in prepared if target.get("worker_id")
    }
    if existing:
        if not existing.issubset(valid_ids):
            raise ValueError(
                "Prepared session is assigned to workers not enabled in activity_workers.json."
            )
    else:
        owner_by_row: dict[str, str] = {}
        for target in prepared:
            row_key = str(target.get("lead_id") or target.get("row_number") or target.get("key"))
            if row_key not in owner_by_row:
                owner_by_row[row_key] = workers[len(owner_by_row) % len(workers)]["id"]
            target["worker_id"] = owner_by_row[row_key]
        state["worker_plan"] = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "sequential",
            "workers": [worker["id"] for worker in workers],
            "assignment": "round_robin_by_source_row",
        }
    counts = {worker["id"]: 0 for worker in workers}
    for target in prepared:
        worker_id = str(target.get("worker_id") or "")
        if worker_id not in counts:
            raise ValueError(f"Target {target.get('key')} has no valid worker assignment.")
        counts[worker_id] += 1
    if not all(counts.values()):
        raise ValueError("Both enabled workers must receive at least one prepared target.")
    return counts


def workers_for_prepared_session(
    state: dict[str, Any], workers: Sequence[dict[str, Any]], single_worker_below_total_targets: int
) -> list[dict[str, Any]]:
    """Choose the lane count once, while preserving any existing assignment.

    A resume must never rebalance targets just because a configuration value
    changed or because one lane already finished.
    """
    prepared = state.get("prepared_targets") or []
    existing_ids = {
        str(target.get("worker_id") or "") for target in prepared if target.get("worker_id")
    }
    if existing_ids:
        selected = [worker for worker in workers if worker["id"] in existing_ids]
        if len(selected) != len(existing_ids):
            raise ValueError(
                "Prepared session is assigned to workers not enabled in activity_workers.json."
            )
        return selected
    if len(prepared) < single_worker_below_total_targets:
        return [workers[0]]
    return list(workers)


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def cdp_http_healthy(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=5) as response:
            return bool(json.loads(response.read().decode()).get("Browser"))
    except Exception:
        return False


def managed_chrome_pids(worker: dict[str, Any]) -> list[int]:
    profile_dir = str(worker["profile_dir"])
    needle = f"--user-data-dir={profile_dir}"
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
            continue
    return pids


def ensure_worker_cdp(worker: dict[str, Any], *, recover: bool = False) -> dict[str, Any]:
    if cdp_http_healthy(worker["cdp_host"], worker["cdp_port"]):
        return {"ok": True, "action": "cdp_healthy" if recover else "cdp_already_running"}
    launch = ROOT / str(worker.get("launch_script") or "")
    if not launch.is_file():
        raise RuntimeError(
            f"CDP for {worker['id']} is unavailable and launch script is missing: {launch}"
        )
    stopped_pids = managed_chrome_pids(worker)
    for pid in stopped_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + 20
    while managed_chrome_pids(worker) and time.time() < deadline:
        time.sleep(1)
    if managed_chrome_pids(worker):
        raise RuntimeError(f"Managed Chrome for {worker['id']} did not stop cleanly.")
    # Match the watcher: Chrome must not inherit a short-lived terminal or the
    # launcher process that requested it.  It owns its own session until the
    # user closes the dedicated browser profile.
    subprocess.Popen(
        ["bash", str(launch)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(2)
        if cdp_http_healthy(worker["cdp_host"], worker["cdp_port"]):
            return {
                "ok": True,
                "action": "browser_restarted" if stopped_pids else "browser_started",
                "stopped_pids": stopped_pids,
            }
    raise RuntimeError(f"Could not start CDP for {worker['id']} on port {worker['cdp_port']}.")


def close_worker_cdp(worker: dict[str, Any]) -> dict[str, Any]:
    """End the managed Chrome process before a fresh-session retry."""
    stopped_pids = managed_chrome_pids(worker)
    for pid in stopped_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + 20
    while managed_chrome_pids(worker) and time.time() < deadline:
        time.sleep(1)
    remaining = managed_chrome_pids(worker)
    return {"ok": not remaining, "stopped_pids": stopped_pids, "remaining_pids": remaining}


def pending_404_retry_counts(day: str, workers: Sequence[dict[str, Any]]) -> dict[str, int]:
    allowed = {worker["id"] for worker in workers}
    state = read_json(session_path(day))
    counts = {worker_id: 0 for worker_id in allowed}
    for target in state.get("prepared_targets", []):
        worker_id = str(target.get("worker_id") or "")
        key = str(target.get("key") or "")
        record = state.get("targets", {}).get(key, {})
        if worker_id in counts and record.get("status") == "retry_pending_404":
            counts[worker_id] += 1
    return counts


def extract_runner_result(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    best: dict[str, Any] = {}
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        # The activity result contains nested dicts (sequence, plan, etc.).
        # Only a top-level runner result has both of these keys; otherwise a
        # later nested object could hide a paused_for_browser_recovery status.
        if isinstance(value, dict) and "ok" in value and "status" in value:
            return value
    return best


def run_lane(
    day: str,
    worker: dict[str, Any],
    *,
    limit_per_lane: int = 0,
    test_stop_worker: str = "",
    test_stop_after_completed: int = 0,
    retry_pending_404: bool = False,
) -> dict[str, Any]:
    env = {
        **os.environ,
        "LINKEDIN_CDP_HOST": worker["cdp_host"],
        "LINKEDIN_CDP_PORT": str(worker["cdp_port"]),
    }
    command = [
        sys.executable,
        str(ACTIVITY_SCRIPT),
        "--date",
        day,
        "--activity-only",
        "--worker-id",
        worker["id"],
        "--no-bridges",
    ]
    if limit_per_lane > 0:
        command.extend(["--limit", str(limit_per_lane)])
    if retry_pending_404:
        command.append("--retry-pending-404")
    if test_stop_after_completed and worker["id"] == test_stop_worker:
        command.extend(["--test-stop-cdp-after-completed", str(test_stop_after_completed)])
    started_at = datetime.now().isoformat(timespec="seconds")
    state = read_json(session_path(day))
    targets = [
        item
        for item in state.get("prepared_targets", [])
        if str(item.get("worker_id") or "") == worker["id"]
    ]
    fixed_seconds = sum(
        int(item.get("delay_sec") or 0)
        + int(item.get("activity_diversion_sec") or 0)
        + int(item.get("batch_gap_sec") or 0)
        for item in targets
    )
    timeout_seconds = max(
        2 * 60 * 60, min(4 * 60 * 60, fixed_seconds + len(targets) * 90 + 30 * 60)
    )
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), text=True, capture_output=True, env=env, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "worker_id": worker["id"],
            "cdp_port": worker["cdp_port"],
            "command": command,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "ok": False,
            "returncode": None,
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
            "summary": {
                "ok": False,
                "status": "lane_timeout",
                "reason": "derived_runtime_budget_exceeded",
            },
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
    return {
        "worker_id": worker["id"],
        "cdp_port": worker["cdp_port"],
        "command": command,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "timeout_seconds": timeout_seconds,
        "summary": extract_runner_result(result.stdout),
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def finalize(day: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ACTIVITY_SCRIPT),
        "--date",
        day,
        "--activity-only",
        "--finalize-only",
    ]
    result = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True, timeout=15 * 60)
    return {
        "command": command,
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run two sequential Activity Check CDP lanes from a prepared session."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Validate the prepared split without launching Chrome or processing targets.",
    )
    parser.add_argument(
        "--normal-gap-minutes",
        type=int,
        default=None,
        help="Override the normal completed-lane handoff gap.",
    )
    parser.add_argument(
        "--failure-gap-minutes",
        type=int,
        default=None,
        help="Override the CDP-unhealthy handoff gap.",
    )
    parser.add_argument(
        "--post-run-404-retry-gap-minutes",
        type=int,
        default=None,
        help="Fresh-session wait before retrying 404-like results.",
    )
    parser.add_argument(
        "--limit-per-lane",
        type=int,
        default=0,
        help="Run at most this many assigned targets per lane (for a bounded test).",
    )
    parser.add_argument(
        "--skip-finalize",
        action="store_true",
        help="Do not write Final/Prospects after lanes finish (for E2E tests).",
    )
    parser.add_argument(
        "--test-stop-worker",
        default="",
        help="E2E test only: worker id whose Chrome will close after a completed target.",
    )
    parser.add_argument(
        "--test-stop-after-completed",
        type=int,
        default=0,
        help="E2E test only: close the test worker's Chrome after N completed targets.",
    )
    args = parser.parse_args(argv)

    config = read_json(WORKER_CONFIG_PATH)
    if not config.get("enabled"):
        raise SystemExit("Sequential activity lanes are disabled in state/activity_workers.json.")
    workers = active_workers(config)
    day = date.fromisoformat(args.date).isoformat()
    state_file = session_path(day)
    state = read_json(state_file)
    single_worker_below_total_targets = int(config.get("single_worker_below_total_targets", 30))
    if single_worker_below_total_targets < 1:
        raise SystemExit("single_worker_below_total_targets must be at least 1.")
    scheduled_workers = workers_for_prepared_session(
        state, workers, single_worker_below_total_targets
    )
    counts = assign_prepared_targets(state, scheduled_workers)
    normal_gap = int(
        config.get("normal_handoff_gap_minutes", 10)
        if args.normal_gap_minutes is None
        else args.normal_gap_minutes
    )
    failure_gap = int(
        config.get("failure_handoff_gap_minutes", 5)
        if args.failure_gap_minutes is None
        else args.failure_gap_minutes
    )
    retry_gap = int(
        config.get("post_run_404_retry_cooldown_minutes", 5)
        if args.post_run_404_retry_gap_minutes is None
        else args.post_run_404_retry_gap_minutes
    )
    if not 0 <= normal_gap <= 30 or not 0 <= failure_gap <= 30 or not 0 <= retry_gap <= 30:
        raise SystemExit("All handoff and retry gaps must be between 0 and 30 minutes.")
    if args.limit_per_lane < 0 or args.test_stop_after_completed < 0:
        raise SystemExit("Lane limits and test stop counts must be >= 0.")
    worker_ids = {worker["id"] for worker in scheduled_workers}
    if args.test_stop_worker and args.test_stop_worker not in worker_ids:
        raise SystemExit("--test-stop-worker must identify an enabled activity worker.")
    if args.test_stop_after_completed and not args.test_stop_worker:
        raise SystemExit("--test-stop-after-completed requires --test-stop-worker.")
    plan = {
        "date": day,
        "workers": [
            {"id": item["id"], "cdp_port": item["cdp_port"], "targets": counts[item["id"]]}
            for item in scheduled_workers
        ],
        "single_worker_below_total_targets": single_worker_below_total_targets,
        "normal_gap_minutes": normal_gap,
        "failure_gap_minutes": failure_gap,
        "post_run_404_retry_gap_minutes": retry_gap,
    }
    if args.plan_only:
        print(json.dumps({"ok": True, "plan_only": True, **plan}, indent=2))
        return 0

    write_json_atomic(state_file, state)
    results: list[dict[str, Any]] = []
    pending = {worker["id"]: worker for worker in scheduled_workers}
    recovery_needed = set()
    cdp_failure_counts = {worker["id"]: 0 for worker in scheduled_workers}
    test_stop_used = False
    current = scheduled_workers[0]
    next_gap = 0
    while pending:
        if next_gap:
            time.sleep(next_gap * 60)
        recovering = current["id"] in recovery_needed
        try:
            recovery = ensure_worker_cdp(current, recover=recovering)
        except Exception as exc:
            cdp_failure_counts[current["id"]] += 1
            if cdp_failure_counts[current["id"]] >= 2:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "paused_for_browser_recovery",
                            **plan,
                            "results": results,
                            "worker_id": current["id"],
                            "error": str(exc),
                            "reason": "same_cdp_failed_twice",
                        },
                        indent=2,
                    )
                )
                return 1
            recovery_needed.add(current["id"])
            alternatives = [
                worker for worker_id, worker in pending.items() if worker_id != current["id"]
            ]
            if not alternatives:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "paused_for_browser_recovery",
                            **plan,
                            "results": results,
                            "worker_id": current["id"],
                            "error": str(exc),
                            "reason": "no_healthy_alternate_cdp",
                        },
                        indent=2,
                    )
                )
                return 1
            results.append(
                {
                    "worker_id": current["id"],
                    "ok": False,
                    "summary": {"status": "paused_for_browser_recovery"},
                    "orchestrator_recovery": {"ok": False, "error": str(exc)},
                }
            )
            current = alternatives[0]
            next_gap = failure_gap
            continue
        result = run_lane(
            day,
            current,
            limit_per_lane=args.limit_per_lane,
            test_stop_worker=args.test_stop_worker,
            test_stop_after_completed=(
                args.test_stop_after_completed
                if current["id"] == args.test_stop_worker and not test_stop_used
                else 0
            ),
        )
        result["orchestrator_recovery"] = recovery
        results.append(result)
        summary_status = str(result.get("summary", {}).get("status") or "")
        if (
            current["id"] == args.test_stop_worker
            and args.test_stop_after_completed
            and int(result.get("summary", {}).get("completed_this_run") or 0)
            >= args.test_stop_after_completed
        ):
            test_stop_used = True
        if result["ok"]:
            pending.pop(current["id"], None)
            recovery_needed.discard(current["id"])
            if not pending:
                break
            current = next(iter(pending.values()))
            next_gap = normal_gap
            continue
        if summary_status != "paused_for_browser_recovery":
            print(
                json.dumps(
                    {"ok": False, "status": "lane_failed", **plan, "results": results}, indent=2
                )
            )
            return 1
        cdp_failure_counts[current["id"]] += 1
        if cdp_failure_counts[current["id"]] >= 2:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "paused_for_browser_recovery",
                        **plan,
                        "results": results,
                        "reason": "same_cdp_failed_twice",
                    },
                    indent=2,
                )
            )
            return 1
        recovery_needed.add(current["id"])
        alternatives = [
            worker for worker_id, worker in pending.items() if worker_id != current["id"]
        ]
        current = alternatives[0] if alternatives else current
        next_gap = failure_gap

    retry_counts = pending_404_retry_counts(day, scheduled_workers)
    retry_results: list[dict[str, Any]] = []
    if any(retry_counts.values()):
        shutdown = {worker["id"]: close_worker_cdp(worker) for worker in scheduled_workers}
        if not all(item["ok"] for item in shutdown.values()):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "paused_for_browser_recovery",
                        **plan,
                        "results": results,
                        "retry_shutdown": shutdown,
                    },
                    indent=2,
                )
            )
            return 1
        state = read_json(state_file)
        state["post_run_404_retry"] = {
            "pending_by_worker": retry_counts,
            "shutdown": shutdown,
            "cooldown_minutes": retry_gap,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        write_json_atomic(state_file, state)
        if retry_gap:
            time.sleep(retry_gap * 60)
        for worker in scheduled_workers:
            if not retry_counts.get(worker["id"]):
                continue
            try:
                recovery = ensure_worker_cdp(worker, recover=True)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "paused_for_browser_recovery",
                            **plan,
                            "results": results,
                            "retry_results": retry_results,
                            "worker_id": worker["id"],
                            "error": str(exc),
                            "reason": "fresh_404_retry_cdp_unavailable",
                        },
                        indent=2,
                    )
                )
                return 1
            retry_result = run_lane(day, worker, retry_pending_404=True)
            retry_result["orchestrator_recovery"] = recovery
            retry_result["phase"] = "fresh_404_retry"
            retry_results.append(retry_result)
            close_worker_cdp(worker)
            if not retry_result["ok"]:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "lane_failed",
                            **plan,
                            "results": results,
                            "retry_results": retry_results,
                        },
                        indent=2,
                    )
                )
                return 1
    if args.skip_finalize:
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "handoff_test_completed",
                    **plan,
                    "results": results,
                    "retry_results": retry_results,
                    "finalize": {"skipped": True},
                },
                indent=2,
            )
        )
        return 0
    final_result = finalize(day)
    print(
        json.dumps(
            {
                "ok": bool(final_result["ok"]),
                "status": "completed" if final_result["ok"] else "finalize_failed",
                **plan,
                "results": results,
                "retry_results": retry_results,
                "finalize": final_result,
            },
            indent=2,
        )
    )
    return 0 if final_result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
