"""CLI and dashboard read-model for the engagement workflow."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from outbound.engagement.campaign import (
    add_source,
    archive_and_start_fresh,
    campaign_control,
    load_campaign,
    pause_campaign,
    resume_campaign,
    runner_active,
)
from outbound.engagement.config import CDP_ACCOUNTS, load_config
from outbound.engagement.paths import HIGH_SIGNAL_PATH, HISTORY_PATH
from outbound.engagement.runner import (
    run_campaign_schedule,
    save_config,
)
from outbound.shared.state import read_json


def dashboard(day: str | None = None) -> dict[str, Any]:
    campaign = load_campaign(day)
    config = load_config()
    history = []
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-30:]:
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {
        "is_running": runner_active(),
        "config": config,
        "campaign": campaign,
        "control": campaign_control(campaign["day"]),
        "high_signal": read_json(HIGH_SIGNAL_PATH, {"profiles": []}).get("profiles", []),
        "history": list(reversed(history)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--day")
    source = sub.add_parser("add-source")
    source.add_argument("url")
    source.add_argument("--day")
    source.add_argument("--cdp-account", choices=sorted(CDP_ACCOUNTS))
    configure = sub.add_parser("configure")
    configure.add_argument("json")
    pause = sub.add_parser("pause")
    pause.add_argument("--day")
    resume = sub.add_parser("resume")
    resume.add_argument("--day")
    archive = sub.add_parser("archive-and-fresh")
    archive.add_argument("--day")
    archive.add_argument("--reason", default="fresh_validation_run")
    run = sub.add_parser("run")
    run.add_argument("--day")
    run.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            dashboard(args.day)
            if args.command == "status"
            else add_source(args.url, args.day, args.cdp_account)
            if args.command == "add-source"
            else save_config(json.loads(args.json))
            if args.command == "configure"
            else pause_campaign(args.day)
            if args.command == "pause"
            else resume_campaign(args.day)
            if args.command == "resume"
            else archive_and_start_fresh(args.day, args.reason)
            if args.command == "archive-and-fresh"
            else run_campaign_schedule(args.day, args.execute)
        )
        print(json.dumps(result, indent=2))
        return (
            2
            if args.command == "run"
            and result.get("status")
            not in {"completed", "dry_run_complete", "paused", "waiting_next_batch"}
            else 0
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
