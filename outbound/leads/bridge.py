"""Pre-final → Prospects bridge for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S9). Pure move.
"""

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import gspread

from outbound.leads.config import (
    BRIDGES_DIR,
    DEFAULT_DESTINATION_TAB,
    DEFAULT_OUTREACH_CONTROL_TAB,
    DESTINATION_COLUMNS,
    FINAL_BRIDGE_COLUMNS,
    OPTIONAL_DESTINATION_COLUMNS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    PROSPECTS_COLUMNS,
)
from outbound.leads.runs import source_sheet_url
from outbound.leads.sheetsio import read_worksheet, require_columns
from outbound.leads.text import clean_text, is_linkedin_profile_url, normalize_key, normalize_url
from outbound.shared.sheets import (
    format_sheet_date,
    get_client,
    get_worksheet,
    normalize_rows,
    open_sheet,
    sheet_values_equal,
)


def parse_bridge_date(value: str) -> datetime:
    raw = clean_text(value)
    if not raw:
        return datetime.now()
    lowered = raw.lower()
    if lowered == "today":
        return datetime.now()
    if lowered == "yesterday":
        return datetime.now() - timedelta(days=1)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported bridge target date: {value}")


def bridge_date_value(value: str) -> str:
    parsed = parse_bridge_date(value)
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def bridge_date_key(value: str) -> str:
    return parse_bridge_date(value).strftime("%Y-%m-%d")


def bridge_state_path(target_date: str, fingerprint: str) -> Path:
    return BRIDGES_DIR / f"{bridge_date_key(target_date)}_{fingerprint}.json"


def prefinal_rows_for_bridge(
    credentials_path: Path,
    sheet_url: str,
    source_tab: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    headers, rows = read_worksheet(credentials_path, sheet_url, source_tab)
    # Pre-final contains DESTINATION_COLUMNS; Final adds FINAL_BRIDGE_COLUMNS.
    required = list(DESTINATION_COLUMNS)
    if "Category" in headers:
        required.extend(FINAL_BRIDGE_COLUMNS)
    require_columns(headers, required, source_tab)
    ready = []
    skipped = []
    for row in rows:
        if not clean_text(row.get("ID")) and not clean_text(row.get("Company")):
            continue
        reasons = []
        for column in ("ID", "Company", "Website"):
            if not clean_text(row.get(column)):
                reasons.append(f"missing_{normalize_key(column).replace(' ', '_')}")
        if "Category" in headers and not clean_text(row.get("Category")):
            reasons.append("missing_category")
        p1_linkedin = clean_text(row.get("P1 LinkedIn"))
        p2_linkedin = clean_text(row.get("P2 LinkedIn"))
        if not p1_linkedin and not p2_linkedin:
            reasons.append("missing_linkedin")
        if p1_linkedin and not is_linkedin_profile_url(p1_linkedin):
            reasons.append("invalid_p1_linkedin")
        if p2_linkedin and not is_linkedin_profile_url(p2_linkedin):
            reasons.append("invalid_p2_linkedin")
        if not reasons:
            ready.append(row)
        else:
            skipped.append(
                {
                    "lead_id": clean_text(row.get("ID")),
                    "company": clean_text(row.get("Company")),
                    "reasons": reasons,
                    "row_number": row.get("_row_number"),
                }
            )
    return headers, ready, skipped


def prefinal_bridge_fingerprint(rows: list[dict[str, Any]]) -> str:
    canonical_rows = []
    for row in rows:
        canonical_rows.append(
            {
                key: clean_text(row.get(key))
                for key in DESTINATION_COLUMNS + OPTIONAL_DESTINATION_COLUMNS + FINAL_BRIDGE_COLUMNS
            }
        )
    payload = json.dumps(canonical_rows, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def pick_engaged_person(row: dict[str, Any]) -> str:
    use = normalize_key(row.get("Use"))
    if "person 2" in use or use in {"p2", "2"}:
        return "Person 2"
    if clean_text(row.get("P1 LinkedIn")):
        return "Person 1"
    if clean_text(row.get("P2 LinkedIn")):
        return "Person 2"
    return "Person 1"


def prospect_row_from_prefinal(row: dict[str, Any]) -> dict[str, Any]:
    prospect = {column: "" for column in PROSPECTS_COLUMNS}
    for column in DESTINATION_COLUMNS:
        prospect[column] = clean_text(row.get(column))
    for column in ("P1 Activity", "P2 Activity"):
        prospect[column] = clean_text(row.get(column))
    prospect["Primary Lane"] = clean_text(row.get("Primary Lane"))
    prospect["Source Tab"] = ""
    prospect["Website"] = normalize_url(prospect.get("Website"))
    prospect["Company LinkedIn"] = normalize_url(prospect.get("Company LinkedIn"))
    prospect["P1 LinkedIn"] = normalize_url(prospect.get("P1 LinkedIn"))
    prospect["P2 LinkedIn"] = normalize_url(prospect.get("P2 LinkedIn"))
    engaged_person = clean_text(row.get("Engaged Person"))
    prospect["Engaged Person"] = (
        engaged_person if engaged_person in {"Person 1", "Person 2"} else pick_engaged_person(row)
    )
    prospect["Notes"] = (
        f"source=final_bridge; category={clean_text(row.get('Category'))}; "
        f"bridged_at={datetime.now().isoformat(timespec='seconds')}"
    )
    return prospect


def existing_prospect_ids(credentials_path: Path, sheet_url: str, prospects_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, prospects_tab)
    except Exception:
        return set()
    if "ID" not in headers:
        return set()
    return {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}


def first_prospect_write_row(rows: list[dict[str, Any]]) -> int:
    last = 1
    for row in rows:
        if any(
            clean_text(row.get(column))
            for column in ("ID", "Company", "P1 LinkedIn", "P2 LinkedIn")
        ):
            last = max(last, int(row.get("_row_number", 1)))
    return last + 1


def write_prospect_rows(
    credentials_path: Path,
    sheet_url: str,
    prospects_tab: str,
    rows: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[int, int]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, prospects_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{prospects_tab} is empty.")
    headers = values[0]
    require_columns(headers, PROSPECTS_COLUMNS, prospects_tab)
    existing_rows = normalize_rows(values)
    start_row = first_prospect_write_row(existing_rows)
    if dry_run or not rows:
        return 0, start_row
    payload = [[row.get(header, "") for header in headers] for row in rows]
    start_cell = gspread.utils.rowcol_to_a1(start_row, 1)
    end_cell = gspread.utils.rowcol_to_a1(start_row + len(payload) - 1, len(headers))
    worksheet.update(
        range_name=f"{start_cell}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return len(payload), start_row


def record_outreach_control_prospects_start_row(
    credentials_path: Path,
    sheet_url: str,
    target_date: str,
    start_row: int,
    control_tab: str = DEFAULT_OUTREACH_CONTROL_TAB,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, control_tab)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{control_tab} is empty.")
    headers = [clean_text(header) for header in values[0]]
    if "Date" not in headers:
        raise ValueError(f"{control_tab} is missing required column: Date")
    if OUTREACH_CONTROL_PROSPECTS_START_ROW not in headers:
        headers.append(OUTREACH_CONTROL_PROSPECTS_START_ROW)
        col = len(headers)
        worksheet.update(
            range_name=gspread.utils.rowcol_to_a1(1, col),
            values=[[OUTREACH_CONTROL_PROSPECTS_START_ROW]],
            value_input_option="USER_ENTERED",
        )
    date_idx = headers.index("Date")
    start_idx = headers.index(OUTREACH_CONTROL_PROSPECTS_START_ROW)
    matches: list[int] = []
    for row_number in range(2, len(values) + 1):
        raw = values[row_number - 1]
        cell_value = raw[date_idx] if date_idx < len(raw) else ""
        if sheet_values_equal(cell_value, target_date):
            matches.append(row_number)
    if not matches:
        # Reuse OBF's daily-row policy instead of inventing target values here.
        from linkedin_outreach_session import _read_outreach_control

        _, control_row, _, _ = _read_outreach_control(
            str(credentials_path),
            sheet_url,
            format_sheet_date(target_date),
            auto_create_missing=True,
        )
        matches.append(int(control_row["_row_number"]))
    if len(matches) > 1:
        raise ValueError(f"Multiple {control_tab} rows found for {format_sheet_date(target_date)}")
    row_number = matches[0]
    worksheet.update(
        range_name=gspread.utils.rowcol_to_a1(row_number, start_idx + 1),
        values=[[str(start_row)]],
        value_input_option="USER_ENTERED",
    )
    return {
        "ok": True,
        "worksheet": control_tab,
        "row_number": row_number,
        "field": OUTREACH_CONTROL_PROSPECTS_START_ROW,
        "value": str(start_row),
    }


def bridge_prefinal_to_prospects(args: argparse.Namespace) -> dict[str, Any]:
    target_date = bridge_date_value(args.target_date)
    target_date_key = bridge_date_key(target_date)
    # Bridge should read from Pre-final by default; allow override via --source-tab.
    source_tab = getattr(args, "source_tab", None) or DEFAULT_DESTINATION_TAB
    headers, prefinal_rows, skipped_unready = prefinal_rows_for_bridge(
        Path(args.credentials),
        source_sheet_url(args),
        source_tab,
    )
    fingerprint = prefinal_bridge_fingerprint(prefinal_rows)
    state_file = bridge_state_path(target_date, fingerprint)
    result: dict[str, Any] = {
        "ok": True,
        "status": "pending",
        "target_date": target_date,
        "target_date_key": target_date_key,
        "source_tab": source_tab,
        "prospects_tab": args.prospects_tab,
        "source_rows_seen": len(prefinal_rows) + len(skipped_unready),
        "source_ready_rows": len(prefinal_rows),
        "prefinal_rows_seen": len(prefinal_rows),
        "fingerprint": fingerprint,
        "state_file": str(state_file),
        "rows_written": 0,
        "write_start_row": None,
        "skipped_existing_ids": [],
        "skipped_unready": skipped_unready,
        "blocked": [],
        "dry_run": bool(args.dry_run),
    }
    if not prefinal_rows:
        result["status"] = "skipped_empty_source"
        if not args.dry_run:
            state_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result
    if state_file.exists() and not args.force:
        previous = json.loads(state_file.read_text())
        if (
            previous.get("write_start_row")
            and previous.get("outreach_control_update", {}).get("ok") is False
        ):
            # Rows already exist: retry only the unfinished control update.
            if args.dry_run:
                return {**previous, "ok": False, "status": "control_sync_pending", "dry_run": True}
            try:
                previous["outreach_control_update"] = record_outreach_control_prospects_start_row(
                    Path(args.credentials),
                    args.prospects_sheet_url,
                    target_date,
                    previous["write_start_row"],
                )
                previous.update(ok=True, status="processed", blocked=[])
            except Exception as exc:
                previous.update(ok=False, status="control_sync_pending", blocked=[str(exc)])
            state_file.write_text(json.dumps(previous, indent=2, ensure_ascii=False) + "\n")
            return previous
        if previous.get("status") == "processed":
            result.update(
                {
                    "status": "skipped_already_bridged",
                    "previous_processed_at": previous.get("processed_at"),
                    "rows_written": previous.get("rows_written", 0),
                }
            )
            return result

    existing_ids = existing_prospect_ids(
        Path(args.credentials), args.prospects_sheet_url, args.prospects_tab
    )
    bridge_rows = []
    for row in prefinal_rows:
        lead_id = clean_text(row.get("ID"))
        if lead_id in existing_ids:
            result["skipped_existing_ids"].append(lead_id)
            continue
        prospect = prospect_row_from_prefinal(row)
        bridge_rows.append(prospect)

    result["candidate_rows"] = len(bridge_rows)
    result["lead_ids"] = [row.get("ID", "") for row in bridge_rows]
    try:
        rows_written, start_row = write_prospect_rows(
            Path(args.credentials),
            args.prospects_sheet_url,
            args.prospects_tab,
            bridge_rows,
            dry_run=args.dry_run,
        )
        result["rows_written"] = rows_written
        result["write_start_row"] = start_row
        if rows_written and not args.dry_run and not getattr(args, "skip_outreach_control", False):
            try:
                result["outreach_control_update"] = record_outreach_control_prospects_start_row(
                    Path(args.credentials),
                    args.prospects_sheet_url,
                    target_date,
                    start_row,
                )
            except Exception as exc:
                result["ok"] = False
                result["outreach_control_update"] = {"ok": False, "error": str(exc)}
                result["blocked"].append(f"Prospects Start Row update failed: {exc}")
        if args.dry_run:
            result["status"] = "dry_run"
        elif rows_written:
            result["status"] = "processed" if result["ok"] else "control_sync_pending"
            result["processed_at"] = datetime.now().isoformat(timespec="seconds")
        elif result["skipped_existing_ids"]:
            result["status"] = "skipped_existing_ids"
        else:
            result["status"] = "skipped_no_candidates"
    except Exception as exc:
        result["ok"] = False
        result["status"] = "failed"
        result["blocked"].append(str(exc))
        result["failed_at"] = datetime.now().isoformat(timespec="seconds")

    if not args.dry_run:
        state_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result
