#!/usr/bin/env python3
"""Restore false withdrawal successes confirmed against LinkedIn's Sent list export.

This command only restores a record when all three conditions hold:
1. It was recorded as a confirmed withdrawal in a local journal.
2. Its profile appears in the user-supplied current LinkedIn Sent export.
3. Its live Outreach Log row is still `Request Withdrawn`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helpers"))

from outreach_helper import CREDS_PATH, OBF_SHEET_URL, OUTREACH_LOG_TAB  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

LOCAL_TZ = ZoneInfo("Africa/Lagos")
SESSIONS = ROOT / "state" / "withdrawal_sessions"
JOURNALS = ROOT / "state" / "withdrawal_journal"
REPORTS = ROOT / "state" / "withdrawal_reconciliations"


def profile_slug(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", str(url or ""), re.I)
    return match.group(1).lower() if match else ""


def name_key(value: str) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def read_export(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\d+\t'(.+)'\t'(.*)'$", line)
        if match:
            rows.append({"name": match.group(1), "url": match.group(2)})
    if not rows:
        raise ValueError("No LinkedIn Sent invitations were found in the supplied export.")
    return rows


def local_false_successes(export_rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for session_path in SESSIONS.glob("*.json"):
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for target in (session.get("runtime_plan") or {}).get("queue") or []:
            prospect_id = str(target.get("prospect_id") or "").strip()
            if prospect_id:
                targets[prospect_id] = target

    successes: dict[str, dict[str, Any]] = {}
    for journal_path in JOURNALS.glob("*.jsonl"):
        for line in journal_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            prospect_id = str(event.get("prospect_id") or "").strip()
            if (
                event.get("event") == "withdrawal_target_result"
                and event.get("confirmed") is True
                and prospect_id in targets
            ):
                successes[prospect_id] = {
                    "target": targets[prospect_id],
                    "event": event,
                    "journal": journal_path.name,
                }

    by_slug = {
        profile_slug(item["target"].get("contact_linkedin", "")): prospect_id
        for prospect_id, item in successes.items()
    }
    by_name = {
        name_key(item["target"].get("contact_name", "")): prospect_id
        for prospect_id, item in successes.items()
    }
    matched: dict[str, dict[str, Any]] = {}
    for item in export_rows:
        prospect_id = by_slug.get(profile_slug(item["url"])) or by_name.get(name_key(item["name"]))
        if prospect_id:
            matched[prospect_id] = {**successes[prospect_id], "linkedin_export": item}
    return matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--linkedin-export",
        required=True,
        help="Text export copied from LinkedIn Sent Invitations.",
    )
    parser.add_argument("--credentials", default=CREDS_PATH)
    parser.add_argument("--sheet-url", default=OBF_SHEET_URL)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the reconciliation. Without this flag, preview only.",
    )
    args = parser.parse_args()

    matched = local_false_successes(read_export(Path(args.linkedin_export)))
    client = get_client(args.credentials)
    sheet = open_sheet(client, args.sheet_url)
    worksheet = get_worksheet(sheet, OUTREACH_LOG_TAB)
    values = worksheet.get_all_values()
    headers = [str(value or "").strip() for value in values[0]]
    required = ["Prospect ID", "Current Progress", "Outcome", "Notes"]
    missing = [header for header in required if header not in headers]
    if missing:
        raise ValueError(f"{OUTREACH_LOG_TAB} is missing columns: {', '.join(missing)}")
    index = {header: position for position, header in enumerate(headers)}
    live_rows = {
        str(row[index["Prospect ID"]] if index["Prospect ID"] < len(row) else "").strip(): (
            row_number,
            row,
        )
        for row_number, row in enumerate(values[1:], start=2)
    }
    now = datetime.now(LOCAL_TZ).isoformat()
    updates: list[dict[str, Any]] = []
    restored: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def a1(row_number: int, column_number: int) -> str:
        letters = ""
        while column_number:
            column_number, remainder = divmod(column_number - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row_number}"

    for prospect_id, item in matched.items():
        location = live_rows.get(prospect_id)
        if not location:
            skipped.append({"prospect_id": prospect_id, "reason": "missing_outreach_log_row"})
            continue
        row_number, row = location
        progress = str(
            row[index["Current Progress"]] if index["Current Progress"] < len(row) else ""
        ).strip()
        if progress != "Request Withdrawn":
            skipped.append(
                {
                    "prospect_id": prospect_id,
                    "reason": "live_progress_not_request_withdrawn",
                    "live_progress": progress,
                }
            )
            continue
        previous_notes = str(row[index["Notes"]] if index["Notes"] < len(row) else "").strip()
        audit = f"withdrawal_reconciled={now}; LinkedIn Sent export confirms request is still pending; restored from false withdrawal success"
        notes = f"{previous_notes} | {audit}" if previous_notes else audit
        for header, value in (
            ("Current Progress", "Conn Request"),
            ("Outcome", "Pending"),
            ("Notes", notes),
        ):
            updates.append({"range": a1(row_number, index[header] + 1), "values": [[value]]})
        restored.append(
            {
                "prospect_id": prospect_id,
                "row_number": row_number,
                "company": item["target"].get("company"),
                "contact_name": item["linkedin_export"]["name"],
                "prior_reason": item["event"].get("reason", ""),
            }
        )

    report = {
        "created_at": now,
        "apply": bool(args.apply),
        "matched_current_linkedin_pending": len(matched),
        "restored": restored,
        "skipped": skipped,
    }
    if args.apply and updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = (
        REPORTS
        / f"{datetime.now(LOCAL_TZ).date().isoformat()}-false-withdrawal-reconciliation.json"
    )
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(report_path),
                "matched": len(matched),
                "restored": len(restored) if args.apply else 0,
                "would_restore": len(restored),
                "skipped": len(skipped),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
