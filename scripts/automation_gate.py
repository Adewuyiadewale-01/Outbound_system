#!/usr/bin/env python3
"""Small approval gate for lead research automations."""

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import get_client, get_worksheet, normalize_rows, open_sheet  # noqa: E402

load_repo_env()

DEFAULT_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
DEFAULT_REVIEW_TAB = "Lead Review"
REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS
AUTOMATION_DIR = ROOT / "state" / "lead_exec_research" / "automation"
CLAIMS_DIR = AUTOMATION_DIR / "claims"
LEAD_PREP_CONFIG_PATH = ROOT / "state" / "lead_prep_orchestration_config.json"


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def checkbox_truthy(value: Any) -> bool:
    return clean_text(value).lower() in {"true", "yes", "y", "1", "checked"}


def approval_gate_enabled() -> bool:
    try:
        payload = json.loads(LEAD_PREP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("approval_gate_enabled", False))


def resolve_all_leads(args: argparse.Namespace) -> bool:
    requested = getattr(args, "all_leads", None)
    if requested is not None:
        return bool(requested)
    return not approval_gate_enabled()


def parse_review_slice(value: Any) -> dict[str, int] | None:
    raw = clean_text(value).lower()
    if not raw or raw in {"all", "none"}:
        return None
    if "/" not in raw:
        raise ValueError("--review-slice must use N/M format, for example 1/3.")
    index_raw, total_raw = raw.split("/", 1)
    try:
        index = int(index_raw)
        total = int(total_raw)
    except ValueError as exc:
        raise ValueError("--review-slice must use numeric N/M format.") from exc
    if total < 1 or index < 1 or index > total:
        raise ValueError("--review-slice requires 1 <= N <= M.")
    return {"index": index, "total": total}


def apply_review_slice(rows: list[dict[str, Any]], value: Any) -> list[dict[str, Any]]:
    parsed = parse_review_slice(value)
    if not parsed:
        return rows
    index = parsed["index"] - 1
    total = parsed["total"]
    return [row for offset, row in enumerate(rows) if offset % total == index]


def parse_review_group_date(value: Any):
    raw = clean_text(value)
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def read_current_review_group(args: argparse.Namespace) -> dict[str, Any]:
    client = get_client(str(Path(args.credentials)))
    worksheet = get_worksheet(open_sheet(client, args.sheet_url), args.review_tab)
    values = worksheet.get_all_values()
    if not values:
        return {"headers": [], "rows": [], "group_row": None, "group_date": ""}
    headers = values[0]
    if "Date" not in headers or "Run ID" not in headers:
        rows = normalize_rows(values)
        return {
            "headers": headers,
            "rows": [row for row in rows if clean_text(row.get("Run ID"))],
            "group_row": None,
            "group_date": "",
        }

    date_idx = headers.index("Date")
    run_idx = headers.index("Run ID")
    today = datetime.now().date()
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        parsed_date = parse_review_group_date(padded[date_idx])
        if parsed_date:
            if current:
                groups.append(current)
            current = {
                "group_row": row_number,
                "group_date": parsed_date,
                "group_values": {
                    header: padded[index] if index < len(padded) else ""
                    for index, header in enumerate(headers)
                },
                "rows": [],
            }
            continue
        if current and clean_text(padded[run_idx]):
            item = {
                header: padded[index] if index < len(padded) else ""
                for index, header in enumerate(headers)
            }
            item["_row_number"] = row_number
            current["rows"].append(item)
    if current:
        groups.append(current)

    todays_groups = [group for group in groups if group["group_date"] == today]
    group = todays_groups[-1] if todays_groups else None
    if not group:
        return {"headers": headers, "rows": [], "group_row": None, "group_date": today.isoformat()}
    return {
        "headers": headers,
        "rows": group["rows"],
        "group_row": group["group_row"],
        "group_date": group["group_date"].isoformat(),
        "group_values": group.get("group_values", {}),
        "review_complete": checkbox_truthy(
            group.get("group_values", {}).get("Design Review Complete")
        ),
    }


def read_review_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    return read_current_review_group(args)["rows"]


def approved_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if checkbox_truthy(row.get("Approved"))]


def selected_rows(
    rows: list[dict[str, Any]],
    all_leads: bool = False,
    lane_scope: str = "all",
    review_slice: str = "",
) -> list[dict[str, Any]]:
    """Return the rows selected for a processing checkpoint."""
    if all_leads:
        selected = [row for row in rows if clean_text(row.get("Run ID"))]
    else:
        selected = approved_rows(rows)
    normalized_scope = clean_text(lane_scope).lower()
    if normalized_scope in {"design", "automation"}:
        selected = [
            row
            for row in selected
            if clean_text(row.get("Primary Lane")).lower() == normalized_scope
        ]
    return apply_review_slice(selected, review_slice)


def approval_fingerprint(rows: list[dict[str, Any]]) -> str:
    lead_ids = sorted(
        clean_text(row.get("Run ID")) for row in rows if clean_text(row.get("Run ID"))
    )
    digest = hashlib.sha256("\n".join(lead_ids).encode("utf-8")).hexdigest()[:16]
    return digest


def claim_path(fingerprint: str) -> Path:
    return CLAIMS_DIR / f"{fingerprint}.json"


def dns_preflight(host: str, port: int = 443) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return {
            "started_at": started_at,
            "host": host,
            "port": port,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"started_at": started_at, "host": host, "port": port, "ok": True, "error": ""}


def dns_preflight_with_retries(host: str, port: int = 443) -> dict[str, Any]:
    attempts = int(os.environ.get("AUTOMATION_GATE_DNS_ATTEMPTS", "5") or "5")
    delay_seconds = float(os.environ.get("AUTOMATION_GATE_DNS_DELAY_SECONDS", "3") or "3")
    attempt_results: list[dict[str, Any]] = []
    for attempt in range(1, max(attempts, 1) + 1):
        result = dns_preflight(host, port)
        result["attempt"] = attempt
        attempt_results.append(result)
        if result["ok"]:
            break
        if attempt < attempts:
            time.sleep(delay_seconds)
    final = attempt_results[-1]
    return {
        "ok": final["ok"],
        "host": host,
        "port": port,
        "attempts": attempt_results,
        "error": final.get("error", ""),
    }


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    diagnostics = {"oauth2_dns": dns_preflight_with_retries("oauth2.googleapis.com", 443)}
    if not diagnostics["oauth2_dns"].get("ok"):
        print(
            f"[automation_gate] DNS preflight failed: {diagnostics['oauth2_dns']['error']}",
            file=sys.stderr,
        )
    group_error = ""
    try:
        group = read_current_review_group(args)
    except Exception as exc:  # noqa: BLE001
        group_error = f"{type(exc).__name__}: {exc}"
        today = datetime.now().date().isoformat()
        group = {"headers": [], "rows": [], "group_row": None, "group_date": today}
    approved = approved_rows(group["rows"])
    approved_design = [
        row for row in approved if clean_text(row.get("Primary Lane")).lower() == "design"
    ]
    lane_scope = getattr(args, "lane_scope", "all")
    review_slice = getattr(args, "review_slice", "")
    all_leads = resolve_all_leads(args)
    unsliced_rows = selected_rows(
        group["rows"],
        all_leads=all_leads,
        lane_scope=lane_scope,
    )
    rows = selected_rows(
        group["rows"],
        all_leads=all_leads,
        lane_scope=lane_scope,
        review_slice=review_slice,
    )

    def overlap_status(row: dict[str, Any]) -> str:
        return clean_text(row.get("Overlap Status") or row.get("Research Source"))

    archive_matches = [row for row in group["rows"] if overlap_status(row) == "Archive Match"]
    fresh_rows = [row for row in group["rows"] if overlap_status(row) == "Fresh"]
    conflicts = [row for row in group["rows"] if overlap_status(row) == "Possible Match"]
    fingerprint = approval_fingerprint(rows) if rows else ""
    claim = {}
    if fingerprint and claim_path(fingerprint).exists():
        claim = json.loads(claim_path(fingerprint).read_text())
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "diagnostics": diagnostics,
        "group_error": group_error,
        "approved_count": len(approved),
        "approved_design_count": len(approved_design),
        "selected_count": len(rows),
        "slice_total_selected_count": len(unsliced_rows),
        "selection_mode": "approval_disabled" if all_leads else "approved_only",
        "approval_gate_enabled": not all_leads,
        "lane_scope": lane_scope,
        "review_slice": review_slice,
        "group_date": group.get("group_date", ""),
        "group_row": group.get("group_row"),
        "group_total_rows": len(group.get("rows", [])),
        "review_complete": bool(group.get("review_complete")),
        "archive_match_count": len(archive_matches),
        "fresh_count": len(fresh_rows),
        "archive_conflict_count": len(conflicts),
        "threshold": args.threshold,
        "ready": bool(rows) if all_leads else len(approved_design) >= args.threshold,
        "fingerprint": fingerprint,
        "claim_status": claim.get("status", ""),
        "claim_file": str(claim_path(fingerprint)) if fingerprint else "",
        "approved_lead_ids": [clean_text(row.get("Run ID")) for row in approved],
        "approved_companies": [clean_text(row.get("Company Name")) for row in approved],
        "selected_lead_ids": [clean_text(row.get("Run ID")) for row in rows],
        "selected_companies": [clean_text(row.get("Company Name")) for row in rows],
    }


def claim(args: argparse.Namespace) -> dict[str, Any]:
    payload = status_payload(args)
    if not payload["ready"]:
        payload["claimed"] = False
        payload["reason"] = "approved_count_below_threshold"
        return payload
    if payload["claim_status"] in {"processing", "processed"}:
        payload["claimed"] = False
        payload["reason"] = f"already_{payload['claim_status']}"
        return payload
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    claim_data = {
        **payload,
        "status": "processing",
        "claimed_at": datetime.now().isoformat(timespec="seconds"),
    }
    claim_path(payload["fingerprint"]).write_text(json.dumps(claim_data, indent=2) + "\n")
    claim_data["claimed"] = True
    return claim_data


def mark(args: argparse.Namespace, status: str) -> dict[str, Any]:
    if not args.fingerprint:
        raise SystemExit("--fingerprint is required.")
    path = claim_path(args.fingerprint)
    data = json.loads(path.read_text()) if path.exists() else {"fingerprint": args.fingerprint}
    data["status"] = status
    opposite_status = "failed" if status == "processed" else "processed"
    data.pop(f"{opposite_status}_at", None)
    data[f"{status}_at"] = datetime.now().isoformat(timespec="seconds")
    if args.note:
        data.setdefault("notes", []).append(args.note)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Approval threshold and duplicate guard for lead automations"
    )
    parser.add_argument("command", choices=["status", "claim", "mark-processed", "mark-failed"])
    parser.add_argument(
        "--sheet-url", default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_SHEET_URL)
    )
    parser.add_argument(
        "--review-tab", default=os.environ.get("LEAD_RESEARCH_REVIEW_TAB", DEFAULT_REVIEW_TAB)
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--threshold", type=int, default=20)
    approval_mode = parser.add_mutually_exclusive_group()
    approval_mode.add_argument(
        "--all-leads",
        dest="all_leads",
        action="store_true",
        default=None,
        help="Process every Lead Review row, overriding the shared approval-gate setting.",
    )
    approval_mode.add_argument(
        "--require-approval",
        dest="all_leads",
        action="store_false",
        help="Process approved rows only, overriding the shared approval-gate setting.",
    )
    parser.add_argument(
        "--lane-scope",
        choices=("all", "design", "automation"),
        default="all",
        help="Limit selected Lead Review rows to one Primary Lane.",
    )
    parser.add_argument(
        "--review-slice",
        default="",
        help="Process a stable slice of selected rows as N/M, for example 1/3.",
    )
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--note", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.threshold < 1:
        raise SystemExit("--threshold must be >= 1.")
    try:
        parse_review_slice(args.review_slice)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    if args.command == "status":
        payload = status_payload(args)
    elif args.command == "claim":
        payload = claim(args)
    elif args.command == "mark-processed":
        payload = mark(args, "processed")
    else:
        payload = mark(args, "failed")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
