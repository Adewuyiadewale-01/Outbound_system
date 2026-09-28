#!/usr/bin/env python3
"""Bounded no-send E2E test: open then dismiss one invite modal per lane."""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helpers"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_outreach_lanes as lanes  # noqa: E402
from linkedin_helper import LinkedInSession  # noqa: E402
from outreach_helper import CREDS_PATH, OBF_SHEET_URL  # noqa: E402
from sheets_helper import read_tab  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-url")
    args = parser.parse_args()
    if args.check_url:
        session = LinkedInSession()
        connected = session.connect()
        if not connected.get("ok"):
            print(json.dumps({"ok": False, "preflight": connected}))
            return 1
        state = session.inspect_profile_action_state(args.check_url)
        result = session.verify_no_note_send_ui(args.check_url, state)
        result["preflight"] = connected
        result["profile_state"] = state
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    workers = lanes.active_workers(lanes.read_json(ROOT / "state" / "outreach_workers.json"))
    rows = read_tab(CREDS_PATH, OBF_SHEET_URL, "Prospects - Test")["rows"]
    by_lane = {worker["primary_lane"]: worker for worker in workers}
    selected = {}
    for row in rows:
        lane = str(row.get("Primary Lane") or "").strip()
        if lane not in by_lane or lane in selected:
            continue
        prefix = "P2" if str(row.get("Engaged Person") or "").strip() == "Person 2" else "P1"
        url = str(row.get(f"{prefix} LinkedIn") or "").strip()
        if url:
            selected[lane] = {
                "id": row.get("ID", ""),
                "company": row.get("Company", ""),
                "url": url,
            }
    missing = [
        worker["primary_lane"] for worker in workers if worker["primary_lane"] not in selected
    ]
    if missing:
        raise SystemExit(f"No usable test prospect for lane(s): {', '.join(missing)}")

    prepared = {
        "date": date.today().isoformat(),
        "test_only": True,
        "purpose": "no_send_connection_modal_e2e",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "handoff_gap_minutes": 0,
        "lanes": [
            {"worker_id": by_lane[lane]["id"], "primary_lane": lane, "prospect": item}
            for lane, item in selected.items()
        ],
    }
    path = ROOT / "state" / "outreach_test_sessions" / f"{date.today().isoformat()}-modal-e2e.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    results = []
    for worker in workers:
        prospect = selected[worker["primary_lane"]]
        recovery = lanes.ensure_worker_cdp(worker)
        child = subprocess.run(
            [
                sys.executable,
                str(ROOT / "helpers" / "linkedin_helper.py"),
                "verify-connection-modal",
                "--url",
                prospect["url"],
            ],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "LINKEDIN_CDP_HOST": worker["cdp_host"],
                "LINKEDIN_CDP_PORT": str(worker["cdp_port"]),
            },
        )
        try:
            result = json.loads(child.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            result = {
                "ok": False,
                "error": "modal_check_no_result",
                "stdout": child.stdout[-1000:],
                "stderr": child.stderr[-1000:],
            }
        results.append(
            {
                "worker_id": worker["id"],
                "primary_lane": worker["primary_lane"],
                "prospect": prospect,
                "cdp": recovery,
                "modal_check": result,
            }
        )
    output = {
        "ok": all(item["modal_check"].get("ok") for item in results),
        "test_only": True,
        "sent": 0,
        "handoff_gap_minutes": 0,
        "state_path": str(path),
        "results": results,
    }
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
