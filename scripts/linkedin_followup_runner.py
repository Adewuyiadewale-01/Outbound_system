#!/usr/bin/env python3
"""LinkedIn follow-up runner driven by Pipeline due rows.

Safety rule: never send a follow-up unless the opened thread shows that the
latest message is ours. If the recipient sent last, mark Pipeline Outcome as
Replied and stop for that lead.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sys
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from linkedin_helper import LinkedInSession, check_circuit_breakers  # noqa: E402
from outreach_helper import CREDS_PATH, OBF_SHEET_URL, PIPELINE_TAB  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

DEFAULT_TIMEZONE = "Africa/Lagos"
FOLLOWUP_TEMPLATES_TAB = "Follow-up Templates"
FOLLOWUP_SEQUENCE_TAB = "Followup Sequence"
MESSAGE_DRAFTS_TAB = "Messaging Drafts"
STATE_DIR = ROOT / "state" / "followup_sessions"
JOURNAL_DIR = ROOT / "state" / "followup_journal"
MESSAGING_URL = "https://www.linkedin.com/messaging/"
RUN_GUARD_PATH = ROOT / "state" / "followup_run_guard.json"
HISTORY_PATH = ROOT / "state" / "followup_history.json"
AUDIT_DIR = Path.home() / "Desktop" / "audit"

PIPELINE_SEQUENCE = {
    "First Message": {"next_action": "FU-1", "days": 3},
    "FU-1": {"next_action": "FU-2", "days": 4},
    "FU-2": {"next_action": "FU-3", "days": 3},
    "FU-3": {"next_action": "FU-4", "working_days": 1},
    "FU-4": {"next_action": "FU-5", "days": 4},
    "FU-5": {"next_action": "FU-6", "days": 5},
    "FU-6": {"next_action": "FU-7", "days": 6},
    "FU-7": {"next_action": "FU-8", "days": 7},
    "FU-8": {"next_action": "FU-9", "days": 8},
    "FU-9": {"next_action": "FU-10", "days": 12},
}
FOLLOWUP_STEPS = {f"FU-{i}" for i in range(1, 10)}
DEFAULT_RUN_SEND_CAP = 20
PIPELINE_REQUIRED_COLUMNS = [
    "Prospect ID",
    "Company",
    "Contact Name",
    "Contact Linkedin",
    "Current Progress",
    "Next Action",
    "Next Action Due",
    "Last Action Date",
    "Outcome",
]
TEMPLATE_HEADERS = ["Step", "Enabled", "Message Body", "Notes"]
MESSAGE_DRAFT_HEADERS = ["Prospect ID", "Stage", "Draft Message", "Status"]
FOLLOWUP_SLOT_HEADERS = [
    "Slot ID",
    "Batch #",
    "Delay Sec",
    "Pre-Send Pause Sec",
    "After Send Delay Sec",
    "Enabled",
]
FOLLOWUP_BATCH_HEADERS = ["Batch #", "Batch Size", "Batch Pause Sec", "Enabled"]
LOCAL_USER_NAME = "Anthony Adewuyi"


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def today_key(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def session_path(day: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{day}.json"


def journal_path(day: str) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR / f"{day}.jsonl"


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


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
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_guard() -> dict[str, Any]:
    return read_json(RUN_GUARD_PATH)


SHUTDOWN_REQUESTED = False


def handle_shutdown(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    print(
        f"\nGraceful shutdown requested (signal {signum}). Will exit after current target finishes.",
        file=sys.stderr,
    )


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def write_guard(payload: dict[str, Any]) -> None:
    write_json(RUN_GUARD_PATH, payload)


def read_history() -> dict[str, Any]:
    history = read_json(HISTORY_PATH)
    history.setdefault("sent_messages", {})
    return history


def write_history(payload: dict[str, Any]) -> None:
    payload.setdefault("sent_messages", {})
    write_json(HISTORY_PATH, payload)


def normalize_message(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def message_appears_in_event(expected: str, event_text: str) -> bool:
    expected_norm = normalize_message(expected)
    event_norm = normalize_message(event_text)
    if not expected_norm or not event_norm:
        return False
    if expected_norm in event_norm:
        return True
    # LinkedIn sometimes truncates visible event text; require a meaningful prefix
    # match instead of a brittle full-body equality.
    prefix = expected_norm[: min(len(expected_norm), 240)]
    return len(prefix) >= 80 and prefix in event_norm


def safe_filename_stem(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"[/:\\\\]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9._&()+,' -]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "audit"


def audit_pdf_path(company: str) -> Path | None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    expected = AUDIT_DIR / f"{safe_filename_stem(company)}.pdf"
    if expected.exists():
        return expected
    target_key = safe_filename_stem(company).lower()
    for candidate in AUDIT_DIR.glob("*.pdf"):
        if safe_filename_stem(candidate.stem).lower() == target_key:
            return candidate
    return None


def template_requires_pdf(body: str) -> bool:
    return bool(re.search(r"\[\s*pdf\s*\]", str(body or ""), flags=re.I))


def strip_pdf_token(body: str) -> str:
    return re.sub(r"(?im)^\s*\[\s*pdf\s*\]\s*$\n?", "", str(body or ""))


def a1(row_number: int, col_number: int) -> str:
    letters = ""
    n = col_number
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row_number}"


def header_index(headers: Iterable[Any]) -> dict[str, int]:
    return {str(header or "").strip(): i for i, header in enumerate(headers)}


def require_columns(index: dict[str, int], columns: Iterable[str], tab: str) -> None:
    missing = [column for column in columns if column not in index]
    if missing:
        raise ValueError(f"{tab} is missing required columns: {', '.join(missing)}")


def cell(row: list[Any], index: dict[str, int], column: str) -> str:
    pos = index.get(column)
    if pos is None or pos >= len(row):
        return ""
    return str(row[pos] or "").strip()


def parse_sheet_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None and numeric >= 20000:
        return (datetime(1899, 12, 30) + timedelta(days=numeric)).date()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def add_working_days(start: date, days: int) -> date:
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def skip_sunday(value: date) -> date:
    """Sunday never consumes a follow-up timeline day or becomes a send date."""
    while value.weekday() == 6:
        value += timedelta(days=1)
    return value


def next_pipeline_step(sent_step: str, sent_date: date) -> tuple[str, str]:
    rule = PIPELINE_SEQUENCE.get(sent_step)
    if not rule:
        return "", ""
    if rule.get("working_days"):
        due = add_working_days(sent_date, int(rule["working_days"]))
    else:
        due = sent_date + timedelta(days=int(rule.get("days", 0)))
    return rule["next_action"], skip_sunday(due).isoformat()


def normalize_step(value: Any) -> str:
    text = clean(value)
    match = re.match(r"^fu[-\s]?(\d+)$", text, flags=re.I)
    if match:
        return f"FU-{int(match.group(1))}"
    if text.lower() in {"first message", "first msg", "m1"}:
        return "First Message"
    return text


def is_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1", "enabled", "y"}


def ensure_templates_tab(spreadsheet: Any) -> Any:
    try:
        worksheet = get_worksheet(spreadsheet, FOLLOWUP_TEMPLATES_TAB)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=FOLLOWUP_TEMPLATES_TAB, rows=100, cols=len(TEMPLATE_HEADERS)
        )
    headers = [str(h or "").strip() for h in worksheet.row_values(1)]
    if headers[: len(TEMPLATE_HEADERS)] != TEMPLATE_HEADERS:
        worksheet.update(
            range_name="A1:D1", values=[TEMPLATE_HEADERS], value_input_option="USER_ENTERED"
        )
    if worksheet.row_count < 10:
        worksheet.resize(rows=100, cols=max(len(TEMPLATE_HEADERS), worksheet.col_count))
    values = worksheet.get_all_values()
    nonempty = [row for row in values[1:] if any(str(cell).strip() for cell in row)]
    if not nonempty:
        rows = []
        for i in range(1, 10):
            enabled = "no"
            body = f"Hi [First Name],\\n\\nTODO: replace this FU-{i} follow-up template.\\n\\nBest,\\nTony"
            rows.append([f"FU-{i}", enabled, body, "Disabled placeholder"])
        worksheet.update(range_name="A2:D10", values=rows, value_input_option="USER_ENTERED")
    return worksheet


def load_templates(worksheet: Any) -> dict[str, dict[str, str]]:
    values = worksheet.get_all_values()
    if not values:
        return {}
    headers = [str(h or "").strip() for h in values[0]]
    index = header_index(headers)
    require_columns(index, TEMPLATE_HEADERS, FOLLOWUP_TEMPLATES_TAB)
    templates: dict[str, dict[str, str]] = {}
    for row in values[1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        step = normalize_step(cell(row, index, "Step"))
        if not step:
            continue
        templates[step] = {
            "step": step,
            "enabled": "yes" if is_enabled(cell(row, index, "Enabled")) else "no",
            "body": cell(row, index, "Message Body"),
            "notes": cell(row, index, "Notes"),
        }
    return templates


def load_first_message_drafts(spreadsheet: Any) -> dict[str, dict[str, str]]:
    try:
        worksheet = get_worksheet(spreadsheet, MESSAGE_DRAFTS_TAB)
    except Exception:
        return {}
    values = worksheet.get_all_values()
    if not values:
        return {}
    headers = [str(h or "").strip() for h in values[0]]
    index = header_index(headers)
    missing = [header for header in MESSAGE_DRAFT_HEADERS if header not in index]
    if missing:
        return {}
    drafts: dict[str, dict[str, str]] = {}
    for row in values[1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        prospect_id = cell(row, index, "Prospect ID")
        stage = normalize_step(cell(row, index, "Stage"))
        message = cell(row, index, "Draft Message")
        if not prospect_id or stage != "First Message" or not message:
            continue
        drafts[prospect_id] = {
            "stage": stage,
            "message": message,
            "status": cell(row, index, "Status"),
        }
    return drafts


def expected_previous_for_target(
    prospect_id: str,
    current_progress: str,
    history: dict[str, Any],
    first_message_drafts: dict[str, dict[str, str]],
) -> dict[str, str]:
    if current_progress == "First Message":
        draft = first_message_drafts.get(prospect_id) or {}
        return {
            "step": "First Message",
            "message": draft.get("message", ""),
            "source": MESSAGE_DRAFTS_TAB if draft.get("message") else "",
        }
    sent_messages = history.get("sent_messages", {}).get(prospect_id, {})
    previous = sent_messages.get(current_progress, {})
    return {
        "step": current_progress,
        "message": previous.get("message", ""),
        "source": "followup_history" if previous.get("message") else "",
    }


def render_template(body: str, target: dict[str, Any]) -> str:
    first_name = (
        clean(target.get("contact_name")).split(" ")[0] if clean(target.get("contact_name")) else ""
    )
    replacements = {
        "First Name": first_name,
        "FirstName": first_name,
        "first_name": first_name,
        "firstName": first_name,
        "Company": target.get("company", ""),
        "Firm Name": target.get("company", ""),
        "firm_name": target.get("company", ""),
        "Contact Name": target.get("contact_name", ""),
    }
    text = strip_pdf_token(body)
    for key, value in replacements.items():
        text = text.replace(f"[{key}]", str(value)).replace(f"{{{key}}}", str(value))

    return text


def build_due_targets(
    pipeline_ws: Any,
    templates: dict[str, dict[str, str]],
    today: date,
    limit: int = 0,
    history: dict[str, Any] | None = None,
    first_message_drafts: dict[str, dict[str, str]] | None = None,
    only_prospect_ids: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values = pipeline_ws.get_all_values()
    if not values:
        return [], []
    headers = [str(h or "").strip() for h in values[0]]
    index = header_index(headers)
    require_columns(index, PIPELINE_REQUIRED_COLUMNS, PIPELINE_TAB)
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    allowed_ids = {clean(value) for value in (only_prospect_ids or []) if clean(value)}
    for row_number, row in enumerate(values[1:], start=2):
        row = row + [""] * max(0, len(headers) - len(row))
        prospect_id = cell(row, index, "Prospect ID")
        if not prospect_id:
            continue
        if allowed_ids and prospect_id not in allowed_ids:
            continue
        next_action = normalize_step(cell(row, index, "Next Action"))
        if next_action not in FOLLOWUP_STEPS:
            continue
        outcome = clean(cell(row, index, "Outcome")).lower()
        if outcome in {"replied", "unsure"}:
            skipped.append(
                {
                    "row_number": row_number,
                    "prospect_id": prospect_id,
                    "reason": "terminal_or_manual_review_outcome",
                    "outcome": cell(row, index, "Outcome"),
                }
            )
            continue
        due_date = parse_sheet_date(cell(row, index, "Next Action Due"))
        if due_date:
            due_date = skip_sunday(due_date)
        if not due_date or due_date > today:
            continue
        template = templates.get(next_action)
        if not template or template.get("enabled") != "yes" or not template.get("body"):
            skipped.append(
                {
                    "row_number": row_number,
                    "prospect_id": prospect_id,
                    "reason": "missing_or_disabled_template",
                    "step": next_action,
                }
            )
            continue
        company = cell(row, index, "Company")
        requires_pdf = template_requires_pdf(template["body"])
        pdf_path = audit_pdf_path(company) if requires_pdf else None
        if requires_pdf and not pdf_path:
            skipped.append(
                {
                    "row_number": row_number,
                    "prospect_id": prospect_id,
                    "reason": "missing_audit_pdf",
                    "step": next_action,
                    "company": company,
                    "expected_folder": str(AUDIT_DIR),
                    "expected_filename": f"{safe_filename_stem(company)}.pdf",
                }
            )
            continue
        current_progress = normalize_step(cell(row, index, "Current Progress"))
        expected_previous = expected_previous_for_target(
            prospect_id,
            current_progress,
            history or {"sent_messages": {}},
            first_message_drafts or {},
        )
        target = {
            "row_number": row_number,
            "prospect_id": prospect_id,
            "company": company,
            "contact_name": cell(row, index, "Contact Name"),
            "contact_linkedin": cell(row, index, "Contact Linkedin"),
            "current_progress": current_progress,
            "next_action": next_action,
            "next_action_due": due_date.isoformat(),
            "last_action_date": cell(row, index, "Last Action Date"),
            "outcome": cell(row, index, "Outcome"),
            "expected_previous_step": expected_previous.get("step", ""),
            "expected_previous_message": expected_previous.get("message", ""),
            "expected_previous_source": expected_previous.get("source", ""),
            "audit_required": requires_pdf,
            "audit_path": str(pdf_path) if pdf_path else "",
            "message": "",
        }
        target["message"] = render_template(template["body"], target)
        targets.append(target)
        if limit and len(targets) >= limit:
            break
    return targets, skipped


def normalize_existing_sunday_due_dates(pipeline_ws: Any) -> int:
    """Move pre-existing FU due dates off Sunday before building the daily queue."""
    values = pipeline_ws.get_all_values()
    if not values:
        return 0
    headers = [str(h or "").strip() for h in values[0]]
    index = header_index(headers)
    require_columns(index, ["Next Action", "Next Action Due"], PIPELINE_TAB)
    changed = 0
    for row_number, row in enumerate(values[1:], start=2):
        row = row + [""] * max(0, len(headers) - len(row))
        if normalize_step(cell(row, index, "Next Action")) not in FOLLOWUP_STEPS:
            continue
        due_date = parse_sheet_date(cell(row, index, "Next Action Due"))
        if not due_date or due_date.weekday() != 6:
            continue
        update_row_fields(
            pipeline_ws, row_number, {"Next Action Due": skip_sunday(due_date).isoformat()}
        )
        changed += 1
    return changed


def bucket_for_presence(presence: dict[str, Any], monitor_attempts: int, mode: str) -> str:
    if mode == "evening":
        return "send"
    if mode == "monitor" and monitor_attempts >= 1:
        return "send"
    status = presence.get("presence_status")
    if status == "active_now":
        return "send"
    if status == "recent_today" and recent_presence_within_minutes(
        presence.get("last_seen_text"), 60
    ):
        return "send"
    if status == "recent_today":
        return "monitor"
    if status == "last_seen_old":
        return "evening" if mode == "run" else "monitor"
    if status == "unknown":
        return "monitor"
    return "monitor"


def recent_presence_within_minutes(last_seen_text: Any, limit_minutes: int) -> bool:
    text = clean(last_seen_text).lower().replace("ago", "").strip()
    if not text:
        return False
    if text in {"active now", "online now", "now"}:
        return True
    minute = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)\b", text)
    if minute:
        return int(minute.group(1)) <= limit_minutes
    hour = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours)\b", text)
    if hour:
        return int(hour.group(1)) * 60 <= limit_minutes
    return False


def update_row_fields(worksheet: Any, row_number: int, fields: dict[str, Any]) -> None:
    for attempt in range(5):
        try:
            headers = [str(h or "").strip() for h in worksheet.row_values(1)]
            index = header_index(headers)
            updates = []
            for field, value in fields.items():
                if field not in index:
                    raise ValueError(f"{worksheet.title} is missing column: {field}")
                updates.append({"range": a1(row_number, index[field] + 1), "values": [[value]]})
            if updates:
                worksheet.batch_update(updates, value_input_option="USER_ENTERED")
            return
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))


def rand_seconds(minimum: int, maximum: int) -> int:
    return random.randint(int(minimum), int(maximum))


def ensure_followup_sequence_tab(spreadsheet: Any) -> Any:
    try:
        worksheet = get_worksheet(spreadsheet, FOLLOWUP_SEQUENCE_TAB)
    except Exception:
        worksheet = spreadsheet.add_worksheet(title=FOLLOWUP_SEQUENCE_TAB, rows=80, cols=12)
    headers = [str(h or "").strip() for h in worksheet.row_values(1)]
    desired = FOLLOWUP_SLOT_HEADERS + ["", ""] + FOLLOWUP_BATCH_HEADERS
    if headers[: len(desired)] != desired:
        worksheet.update(range_name="A1:L1", values=[desired], value_input_option="USER_ENTERED")
    if worksheet.row_count < 80:
        worksheet.resize(rows=80, cols=max(12, worksheet.col_count))
    return worksheet


def distribute_batches(total: int) -> list[int]:
    if total <= 0:
        return []
    if total <= 6:
        return [total]
    batch_count = max(2, (total + 5) // 6)
    remaining = total
    sizes: list[int] = []
    for idx in range(batch_count):
        slots_left = batch_count - idx
        if slots_left == 1:
            size = remaining
        else:
            min_remaining = slots_left - 1
            max_size = min(8, remaining - min_remaining)
            size = random.randint(1, max(1, max_size))
        sizes.append(size)
        remaining -= size
    random.shuffle(sizes)
    return sizes


def build_followup_runtime_plan(
    spreadsheet: Any, target_count: int, cap: int = DEFAULT_RUN_SEND_CAP
) -> list[dict[str, Any]]:
    worksheet = ensure_followup_sequence_tab(spreadsheet)
    planned_count = max(0, target_count)
    batch_sizes = distribute_batches(planned_count)
    batch_pause_by_batch = {
        batch_index: rand_seconds(30, 180) for batch_index in range(1, len(batch_sizes) + 1)
    }
    plan: list[dict[str, Any]] = []
    slot_rows: list[list[Any]] = []
    slot_id = 1
    for batch_index, size in enumerate(batch_sizes, start=1):
        for _ in range(size):
            item = {
                "slot_id": slot_id,
                "batch": batch_index,
                "delay_sec": rand_seconds(8, 35),
                "pre_send_pause_sec": rand_seconds(3, 12),
                "after_send_delay_sec": rand_seconds(12, 45),
                "batch_pause_sec": batch_pause_by_batch[batch_index],
                "enabled": "yes",
            }
            plan.append(item)
            slot_rows.append(
                [
                    item["slot_id"],
                    item["batch"],
                    item["delay_sec"],
                    item["pre_send_pause_sec"],
                    item["after_send_delay_sec"],
                    item["enabled"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            slot_id += 1
    for empty_slot in range(len(slot_rows) + 1, max(20, len(slot_rows)) + 1):
        slot_rows.append([empty_slot, "", "", "", "", "", "", "", "", "", "", ""])
    batch_rows: list[list[Any]] = []
    for batch_index, size in enumerate(batch_sizes, start=1):
        batch_rows.append(
            [
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                batch_index,
                size,
                batch_pause_by_batch[batch_index],
                "yes",
            ]
        )
    for _ in range(len(batch_rows) + 1, max(5, len(batch_rows)) + 1):
        batch_rows.append(["", "", "", "", "", "", "", "", "", "", "", ""])
    rows_by_number: dict[int, list[Any]] = {}
    for offset, row in enumerate(slot_rows, start=2):
        rows_by_number[offset] = row
    for offset, row in enumerate(batch_rows, start=2):
        current = rows_by_number.setdefault(offset, [""] * 12)
        current[8:12] = row[8:12]
    if rows_by_number:
        max_row = max(rows_by_number)
        if worksheet.row_count < max_row:
            worksheet.resize(rows=max_row, cols=max(12, worksheet.col_count))
        values = [rows_by_number.get(row_number, [""] * 12) for row_number in range(2, max_row + 1)]
        worksheet.update(
            range_name=f"A2:L{max_row}", values=values, value_input_option="USER_ENTERED"
        )
    return plan


def sleep_seconds(seconds: Any, dry_run: bool = False) -> None:
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        value = 0
    if dry_run or value <= 0:
        return
    time.sleep(value)


def open_messaging(session: LinkedInSession) -> dict[str, Any]:
    session.cdp.navigate(MESSAGING_URL, wait_load=False, timeout=10)
    time.sleep(random.uniform(2.5, 4.0))
    danger = check_circuit_breakers(session.cdp)
    if danger:
        return {"ok": False, "danger": danger}
    # Retry up to 3 times waiting for the conversation list to load
    for attempt in range(3):
        result = session.cdp.evaluate("""
          (() => {
            const pane = document.querySelector("ul.msg-conversations-container__conversations-list");
            return {ok: !!pane, url: location.href, thread_count: pane ? pane.children.length : 0};
          })()
        """)
        if isinstance(result, dict) and result.get("ok"):
            return result
        time.sleep(random.uniform(2.5, 4.0))
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def open_thread_by_name(
    session: LinkedInSession, contact_name: str, max_steps: int = 8
) -> dict[str, Any]:
    if not clean(contact_name):
        return {"ok": False, "reason": "contact_name_missing"}
    script = f"""
    (async () => {{
      const TARGET = {json.dumps(contact_name)};
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const pane = document.querySelector("ul.msg-conversations-container__conversations-list");
      if (!pane) return {{ok: false, reason: "inbox_pane_missing"}};
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      const find = () => [...pane.children].filter(visible).map(el => {{
        const text = norm(el.innerText || el.textContent);
        if (!text.toLowerCase().includes(TARGET.toLowerCase())) return null;
        return {{el, text}};
      }}).filter(Boolean)[0] || null;
      pane.scrollTop = 0;
      pane.dispatchEvent(new Event("scroll", {{bubbles: true}}));
      await sleep(650);
      let found = null;
      const attempts = [];
      for (let step = 0; step < {int(max_steps)}; step++) {{
        found = find();
        attempts.push({{step, scrollTop: pane.scrollTop, found: !!found, matchText: found ? found.text.slice(0, 180) : ""}});
        if (found) break;
        const before = pane.scrollTop;
        pane.scrollTop = Math.min(pane.scrollTop + Math.floor(220 + Math.random() * 120), pane.scrollHeight);
        pane.dispatchEvent(new Event("scroll", {{bubbles: true}}));
        await sleep(500 + Math.random() * 350);
        if (pane.scrollTop === before && pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 5) break;
      }}
      if (!found) return {{ok: false, reason: "thread_not_found", attempts, scrollTop: pane.scrollTop, scrollHeight: pane.scrollHeight}};
      found.el.scrollIntoView({{block: "center"}});
      pane.dispatchEvent(new Event("scroll", {{bubbles: true}}));
      await sleep(800);
      found = find();
      if (!found) return {{ok: false, reason: "thread_disappeared_after_scroll", attempts}};
      const r = found.el.getBoundingClientRect();
      const x = r.left + Math.min(95, r.width * 0.32);
      const y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      if (!hit) return {{ok: false, reason: "element_from_point_null", rect: {{x, y}}}};
      ["mouseover", "mousemove", "mousedown", "mouseup", "click"].forEach(type => {{
        hit.dispatchEvent(new MouseEvent(type, {{bubbles: true, cancelable: true, clientX: x, clientY: y, view: window}}));
      }});
      await sleep(1800);
      return {{ok: true, url: location.href, rowText: found.text.slice(0, 260), attempts, clickedAt: {{x: Math.round(x), y: Math.round(y)}}}};
    }})()
    """
    result = session.cdp.evaluate(script, await_promise=True, timeout=90)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def extract_profile_name_from_url(session: LinkedInSession, profile_url: str) -> dict[str, Any]:
    if not clean(profile_url):
        return {"ok": False, "reason": "profile_url_missing"}
    session.cdp.navigate(profile_url, wait_load=False, timeout=15)
    time.sleep(random.uniform(2.5, 4.0))
    danger = check_circuit_breakers(session.cdp)
    if danger:
        return {
            "ok": False,
            "reason": "profile_preflight_blocked",
            "danger": danger,
            "url": profile_url,
        }
    script = """
    (() => {
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const bad = /skip to|home|my network|jobs|messaging|notifications|premium|more profiles|connect|message|contact info|followers|posts|comments|images/i;
      const titleName = norm(document.title).replace(/\\s*\\|\\s*LinkedIn\\s*$/i, "");
      const bodyText = norm(document.body.innerText || document.body.textContent);
      if (/this page doesn.t exist|please check your url|page not found/i.test(bodyText)) {
        return {ok: false, reason: "profile_not_found", url: location.href, title: document.title, titleName};
      }
      const photo = [...document.querySelectorAll("main img")]
        .filter(visible)
        .map(img => ({img, r: img.getBoundingClientRect(), alt: norm(img.alt)}))
        .filter(x => x.r.width >= 80 && x.r.height >= 80 && x.r.y < 360)
        .sort((a, b) => (b.r.width * b.r.height) - (a.r.width * a.r.height))[0];
      const leftBoundary = photo ? photo.r.left - 40 : 0;
      const rightBoundary = photo ? photo.r.left + 620 : 760;
      const topBoundary = photo ? photo.r.top - 30 : 80;
      const bottomBoundary = photo ? photo.r.top + 330 : 520;
      const hits = [...document.querySelectorAll("main h1, main span, main div")]
        .filter(visible)
        .map((el, i) => {
          const text = norm(el.innerText || el.textContent);
          const r = el.getBoundingClientRect();
          if (!text || bad.test(text)) return null;
          if (text.length < 3 || text.length > 90) return null;
          if (r.x < leftBoundary || r.x > rightBoundary) return null;
          if (r.y < topBoundary || r.y > bottomBoundary) return null;
          const looksLikeName = /^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ.'’ -]+(?:\\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ.'’ -]+)+/.test(text);
          if (!looksLikeName) return null;
          const score =
            (el.tagName === "H1" ? 100 : 0) +
            (r.height >= 24 ? 40 : 0) +
            (r.width >= 160 ? 20 : 0) +
            (text === titleName ? 80 : 0) -
            (/[|•]/.test(text) ? 40 : 0);
          return {i, tag: el.tagName, text, score};
        })
        .filter(Boolean)
        .sort((a, b) => b.score - a.score);
      const best = hits[0] || null;
      const extractedName = best && best.text ? best.text : titleName;
      const source = best && best.text ? "top_card" : titleName ? "document_title" : "none";
      const looksClean =
        !!extractedName &&
        extractedName.length >= 3 &&
        extractedName.length <= 90 &&
        !bad.test(extractedName) &&
        !/[|•]/.test(extractedName) &&
        /\\s/.test(extractedName);
      return {
        ok: looksClean,
        url: location.href,
        title: document.title,
        titleName,
        extractedName,
        source,
        best,
        topHits: hits.slice(0, 5)
      };
    })()
    """
    result = session.cdp.evaluate(script, timeout=20)
    return result if isinstance(result, dict) else {"ok": False, "raw": result, "url": profile_url}


def name_search_terms(*names: Any) -> list[str]:
    terms: list[str] = []
    seen = set()
    suffixes = {"ll.m", "llm", "mba", "msc", "phd", "cfa", "cfp", "mr", "dr"}
    for name in names:
        text = clean(name)
        if not text:
            continue
        parts = [re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ.'’\\-]", "", part).strip(".") for part in text.split()]
        parts = [
            part
            for part in parts
            if len(part) >= 2 and part.lower().replace(".", "") not in suffixes
        ]
        candidates = []
        if parts:
            candidates.append(parts[0])
        if len(parts) >= 2:
            candidates.append(parts[-1])
            candidates.append(f"{parts[0]} {parts[-1]}")
        candidates.append(text)
        for candidate in candidates:
            key = candidate.lower()
            if key and key not in seen:
                seen.add(key)
                terms.append(candidate)
    return terms


def open_thread_by_search_terms(
    session: LinkedInSession, search_terms: list[str], expected_names: list[str]
) -> dict[str, Any]:
    terms = [clean(term) for term in search_terms if clean(term)]
    expected = [clean(name) for name in expected_names if clean(name)]
    if not terms:
        return {"ok": False, "reason": "search_terms_missing"}
    script = f"""
    (async () => {{
      const TERMS = {json.dumps(terms)};
      const EXPECTED = {json.dumps(expected)};
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const words = s => norm(s).toLowerCase().split(/[^a-zà-öø-ÿ0-9.'’-]+/).filter(Boolean);
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      const scoreRow = text => {{
        const rowWords = new Set(words(text));
        let best = 0;
        for (const name of EXPECTED) {{
          const nameWords = words(name).filter(w => !['ll', 'm', 'llm', 'mba', 'msc', 'phd', 'cfa', 'cfp'].includes(w));
          if (!nameWords.length) continue;
          const matched = nameWords.filter(w => rowWords.has(w)).length;
          const ratio = matched / nameWords.length;
          let score = Math.round(ratio * 100);
          if (words(text).join(' ').includes(words(name).join(' '))) score += 50;
          if (nameWords[0] && rowWords.has(nameWords[0])) score += 25;
          if (nameWords.length > 1 && rowWords.has(nameWords[nameWords.length - 1])) score += 25;
          best = Math.max(best, score);
        }}
        return best;
      }};
      const findSearch = () => [...document.querySelectorAll("input, [contenteditable='true'], [role='textbox']")]
        .filter(visible)
        .find(el => /search messages|search/i.test(`${{el.getAttribute('aria-label') || ''}} ${{el.getAttribute('placeholder') || ''}}`));
      const rows = () => [...document.querySelectorAll("ul.msg-conversations-container__conversations-list li, ul.msg-conversations-container__conversations-list > *, [data-view-name='message-thread-list-item'], li")]
        .filter(visible)
        .map((el, i) => {{
          const text = norm(el.innerText || el.textContent);
          if (!text || text.length < 3) return null;
          const r = el.getBoundingClientRect();
          if (r.x > 380 || r.y < 80 || r.width < 120 || r.height < 35) return null;
          const score = scoreRow(text);
          return {{el, i, text, score, rect: {{x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}}}};
        }})
        .filter(Boolean)
        .sort((a, b) => b.score - a.score);

      const attempts = [];
      for (const term of TERMS) {{
        const input = findSearch();
        if (!input) return {{ok: false, reason: "message_search_input_missing", attempts}};
        input.focus();
        await sleep(250);
        if ('value' in input) input.value = '';
        input.innerHTML = '';
        input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'deleteContentBackward'}}));
        await sleep(250);
        if ('value' in input) {{
          input.value = term;
          input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: term}}));
        }} else {{
          document.execCommand('insertText', false, term);
          input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: term}}));
        }}
        input.dispatchEvent(new KeyboardEvent('keydown', {{bubbles: true, cancelable: true, key: 'Enter', code: 'Enter', which: 13, keyCode: 13}}));
        input.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true, cancelable: true, key: 'Enter', code: 'Enter', which: 13, keyCode: 13}}));
        if (input.form) input.form.dispatchEvent(new Event('submit', {{bubbles: true, cancelable: true}}));
        await sleep(1600);
        let currentRows = rows();
        attempts.push({{term, rows: currentRows.slice(0, 5).map(r => ({{text: r.text.slice(0, 220), score: r.score, rect: r.rect}}))}});
        const picked = currentRows.find(r => r.score >= 70) || currentRows.find(r => r.score >= 50);
        if (!picked) continue;
        picked.el.scrollIntoView({{block: 'center'}});
        await sleep(450);
        const r = picked.el.getBoundingClientRect();
        const x = r.left + Math.min(95, r.width * 0.30);
        const y = r.top + r.height / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit) return {{ok: false, reason: 'element_from_point_null', attempts, picked: {{text: picked.text, score: picked.score}}}};
        ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click'].forEach(type => {{
          hit.dispatchEvent(new MouseEvent(type, {{bubbles: true, cancelable: true, clientX: x, clientY: y, view: window}}));
        }});
        await sleep(1800);
        return {{
          ok: true,
          method: 'message_search',
          term,
          pickedText: picked.text.slice(0, 260),
          score: picked.score,
          attempts,
          url: location.href,
          clickedAt: {{x: Math.round(x), y: Math.round(y)}}
        }};
      }}
      return {{ok: false, reason: 'thread_not_found_by_search_terms', attempts}};
    }})()
    """
    result = session.cdp.evaluate(script, await_promise=True, timeout=45)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def open_thread_for_target(session: LinkedInSession, target: dict[str, Any]) -> dict[str, Any]:
    original_name = clean(target.get("contact_name"))
    first = open_thread_by_name(session, original_name, max_steps=8)
    if first.get("ok"):
        first["resolved_contact_name"] = original_name
        first["name_source"] = "sheet"
        return first
    terms = name_search_terms(original_name)
    second = open_thread_by_search_terms(session, terms, [original_name])
    second["first_open"] = first
    second["search_terms"] = terms
    second["resolved_contact_name"] = original_name
    second["name_source"] = "message_search_terms"
    return second


def inspect_thread(session: LinkedInSession, contact_name: str) -> dict[str, Any]:
    script = f"""
    (() => {{
      const CONTACT = {json.dumps(contact_name)};
      const USER = {json.dumps(LOCAL_USER_NAME)};
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const lower = s => norm(s).toLowerCase();
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
      }};
      const headerText = norm([...document.querySelectorAll("section, header, div, a, span")]
        .filter(visible)
        .filter(el => {{
          const r = el.getBoundingClientRect();
          return r.x > 300 && r.x < 950 && r.y > 80 && r.y < 280;
        }})
        .map(el => el.innerText || el.textContent || "")
        .join(" "));
      const identity_ok = lower(headerText).includes(lower(CONTACT).split(" ")[0]) || lower(headerText).includes(lower(CONTACT));
      const activityText = headerText;
      let presence_status = "unknown";
      let last_seen_text = "";
      if (/\\b(active now|online now)\\b/i.test(activityText)) {{
        presence_status = "active_now";
        last_seen_text = "active now";
      }} else {{
        const m = activityText.match(/(?:Mobile|Active|Online)\\s*[·•-]?\\s*((?:\\d+\\s*(?:m|min|mins|h|hr|hrs)|today|now|yesterday|\\d+\\s*d)\\s*ago?)/i);
        if (m) {{
          last_seen_text = norm(m[1]);
          presence_status = /(m|min|h|hr|today|now)/i.test(last_seen_text) ? "recent_today" : "last_seen_old";
        }}
      }}
      const receipts = [...document.querySelectorAll(".msg-s-event-listitem__seen-receipts img.msg-s-event-listitem__seen-receipt-photo, .msg-s-event-listitem__seen-receipts img[alt*='Seen by'], .msg-s-event-listitem__seen-receipts img[title*='Seen by']")]
        .map(img => {{
          const text = norm(img.getAttribute("title") || img.getAttribute("alt"));
          const match = text.match(/^Seen by (.+?) at (.+?)\\.?$/i);
          return {{text, seen_by: match ? match[1] : "", seen_at: match ? match[2] : ""}};
        }});
      const eventTexts = [...document.querySelectorAll(".msg-s-message-list__event, .msg-s-event-listitem, li")]
        .filter(visible)
        .map(el => norm(el.innerText || el.textContent))
        .filter(text => /sent the following message|sent the following messages/i.test(text));
      const latestEvent = eventTexts[eventTexts.length - 1] || "";
      const latestOwnEvent = [...eventTexts].reverse().find(text => lower(text).startsWith(lower(USER)) || lower(text).includes(lower(USER) + " sent the following")) || "";
      const latestRecipientEvent = [...eventTexts].reverse().find(text => lower(text).startsWith(lower(CONTACT)) || lower(text).includes(lower(CONTACT) + " sent the following")) || "";
      let latest_sender = "unknown";
      if (lower(latestEvent).startsWith(lower(CONTACT)) || lower(latestEvent).includes(lower(CONTACT) + " sent the following")) latest_sender = "recipient";
      if (lower(latestEvent).startsWith(lower(USER)) || lower(latestEvent).includes(lower(USER) + " sent the following")) latest_sender = "self";
      const outcome = latest_sender === "recipient" ? "Replied" : latest_sender === "self" ? "No reply" : "Unsure";
      return {{
        ok: true,
        url: location.href,
        identity_ok,
        header_text: headerText.slice(0, 500),
        presence_status,
        last_seen_text,
        read_status: receipts.length ? "seen" : "unknown",
        latest_receipt: receipts[receipts.length - 1] || null,
        latest_sender,
        latest_event: latestEvent.slice(0, 500),
        latest_event_full: latestEvent.slice(0, 6000),
        latest_own_event: latestOwnEvent.slice(0, 6000),
        latest_recipient_event: latestRecipientEvent.slice(0, 2000),
        outcome,
      }};
    }})()
    """
    result = session.cdp.evaluate(script, timeout=20)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def human_type_message(session: LinkedInSession, message: str) -> dict[str, Any]:
    script = f"""
    (async () => {{
      const MESSAGE = {json.dumps(message)};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const rand = (min, max) => Math.floor(min + Math.random() * (max - min + 1));
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      const composer = [...document.querySelectorAll("[contenteditable='true'], [role='textbox'], textarea")]
        .filter(visible)
        .find(el => /write a message/i.test(`${{el.getAttribute("aria-label") || ""}} ${{el.getAttribute("placeholder") || ""}}`));
      if (!composer) return {{ok: false, reason: "composer_not_found"}};
      composer.focus();
      await sleep(rand(500, 1300));
      composer.innerHTML = "";
      composer.dispatchEvent(new InputEvent("input", {{bubbles: true, inputType: "deleteContentBackward", data: null}}));
      await sleep(rand(500, 1000));
      for (let i = 0; i < MESSAGE.length; i++) {{
        const ch = MESSAGE[i];
        document.execCommand("insertText", false, ch);
        composer.dispatchEvent(new InputEvent("input", {{bubbles: true, inputType: "insertText", data: ch}}));
        if (ch === "\\n") await sleep(rand(650, 1400));
        else if (/[.!?]/.test(ch)) await sleep(rand(360, 850));
        else if (ch === "," || ch === ";") await sleep(rand(220, 520));
        else if (ch === " ") await sleep(rand(45, 150));
        else await sleep(rand(22, 88));
      }}
      await sleep(rand(1200, 3200));
      const typed = composer.innerText || composer.textContent || composer.value || "";
      const send = [...document.querySelectorAll("button, [role='button']")]
        .filter(visible)
        .find(el => {{
          const text = norm(el.innerText || el.textContent);
          const aria = norm(el.getAttribute("aria-label"));
          return (/^send$/i.test(text) || /^send$/i.test(aria)) && !el.disabled && el.getAttribute("aria-disabled") !== "true";
        }});
      return {{ok: norm(typed) === norm(MESSAGE), typed: norm(typed), send_ready: !!send}};
    }})()
    """
    result = session.cdp.evaluate(script, await_promise=True, timeout=max(60, len(message) * 0.25))
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def click_send_and_confirm(session: LinkedInSession, message: str) -> dict[str, Any]:
    script = f"""
    (async () => {{
      const MESSAGE = {json.dumps(message)};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      const send = [...document.querySelectorAll("button, [role='button']")]
        .filter(visible)
        .find(el => {{
          const text = norm(el.innerText || el.textContent);
          const aria = norm(el.getAttribute("aria-label"));
          return (/^send$/i.test(text) || /^send$/i.test(aria)) && !el.disabled && el.getAttribute("aria-disabled") !== "true";
        }});
      if (!send) return {{ok: false, reason: "send_button_not_ready"}};
      await sleep(1200 + Math.random() * 4200);
      send.click();
      await sleep(2600);
      const appeared = norm(document.body.innerText || document.body.textContent).includes(norm(MESSAGE).slice(0, 80));
      return {{ok: appeared, confirmed: appeared, reason: appeared ? "" : "sent_message_not_confirmed"}};
    }})()
    """
    result = session.cdp.evaluate(script, await_promise=True, timeout=20)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def clear_composer(session: LinkedInSession) -> dict[str, Any]:
    script = """
    (() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const composer = [...document.querySelectorAll("[contenteditable='true'], [role='textbox'], textarea")]
        .filter(visible)
        .find(el => /write a message/i.test(`${el.getAttribute("aria-label") || ""} ${el.getAttribute("placeholder") || ""}`));
      if (!composer) return {ok: false, reason: "composer_not_found"};
      composer.focus();
      if ("value" in composer) composer.value = "";
      composer.innerHTML = "";
      composer.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "deleteContentBackward", data: null}));
      return {ok: true};
    })()
    """
    result = session.cdp.evaluate(script, timeout=10)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def remove_pending_attachment(session: LinkedInSession, filename: str = "") -> dict[str, Any]:
    script = f"""
    (() => {{
      const FILENAME = {json.dumps(filename)};
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      const nodes = [...document.querySelectorAll("button, [role='button'], svg, [aria-label], [title]")]
        .filter(visible)
        .filter(el => {{
          const hay = `${{norm(el.innerText || el.textContent)}} ${{norm(el.getAttribute("aria-label"))}} ${{norm(el.getAttribute("title"))}}`;
          return /remove attachment/i.test(hay) && (!FILENAME || hay.includes(FILENAME));
        }});
      if (!nodes.length) return {{ok: true, removed: false, reason: "remove_button_not_found"}};
      const node = nodes[0];
      const clickable = node.closest("button, [role='button']") || node;
      clickable.click();
      return {{ok: true, removed: true, clicked_tag: clickable.tagName, node_tag: node.tagName}};
    }})()
    """
    result = session.cdp.evaluate(script, timeout=10)
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def attach_audit_pdf(session: LinkedInSession, pdf_path: str) -> dict[str, Any]:
    path = Path(pdf_path)
    if not path.exists():
        return {"ok": False, "reason": "audit_pdf_missing", "pdf_path": str(path)}
    try:
        document = session.cdp.send("DOM.getDocument", {"depth": -1, "pierce": True}, timeout=10)
        root_id = document.get("root", {}).get("nodeId")
        if not root_id:
            return {"ok": False, "reason": "dom_root_missing", "pdf_path": str(path)}
        query = session.cdp.send(
            "DOM.querySelector",
            {
                "nodeId": root_id,
                "selector": 'input[type="file"][accept*=".pdf"], input[type="file"]:not([accept="image/*"])',
            },
            timeout=10,
        )
        node_id = query.get("nodeId")
        if not node_id:
            return {"ok": False, "reason": "pdf_file_input_not_found", "pdf_path": str(path)}
        session.cdp.send(
            "DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(path)]}, timeout=15
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "cdp_file_upload_failed",
            "error": str(exc),
            "pdf_path": str(path),
        }

    filename = path.name
    script = f"""
    (async () => {{
      const FILENAME = {json.dumps(filename)};
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {{
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }};
      for (let i = 0; i < 60; i++) {{
        const body = norm(document.body.innerText || document.body.textContent);
        const hasPreview = body.includes(FILENAME) || [...document.querySelectorAll("[aria-label], [title]")]
          .some(el => `${{norm(el.getAttribute("aria-label"))}} ${{norm(el.getAttribute("title"))}}`.includes(FILENAME));
        const remove = [...document.querySelectorAll("button, [role='button'], [aria-label], [title]")]
          .filter(visible)
          .find(el => /remove attachment/i.test(`${{norm(el.innerText || el.textContent)}} ${{norm(el.getAttribute("aria-label"))}} ${{norm(el.getAttribute("title"))}}`));
        const attachmentChrome = [...document.querySelectorAll("[class*='attachment'], [class*='msg-form__attachment'], [aria-label], [title]")]
          .filter(visible)
          .find(el => {{
            const hay = `${{norm(el.innerText || el.textContent)}} ${{norm(el.getAttribute("aria-label"))}} ${{norm(el.getAttribute("title"))}}`;
            return hay.includes(FILENAME) || /remove attachment|attached|upload/i.test(hay);
          }});
        const send = [...document.querySelectorAll("button, [role='button']")]
          .filter(visible)
          .find(el => {{
            const text = norm(el.innerText || el.textContent);
            const aria = norm(el.getAttribute("aria-label"));
            return (/^send$/i.test(text) || /^send$/i.test(aria)) && !el.disabled && el.getAttribute("aria-disabled") !== "true";
          }});
        if ((hasPreview || attachmentChrome || remove) && send) {{
          return {{
            ok: true,
            attached: true,
            filename: FILENAME,
            send_ready: true,
            confirmed_by: hasPreview ? 'filename' : (remove ? 'remove_attachment' : 'attachment_chrome')
          }};
        }}
        await sleep(500);
      }}
      return {{ok: false, reason: "attachment_preview_not_confirmed", filename: FILENAME}};
    }})()
    """
    result = session.cdp.evaluate(script, await_promise=True, timeout=15)
    if isinstance(result, dict):
        result["pdf_path"] = str(path)
        return result
    return {"ok": False, "raw": result, "pdf_path": str(path)}


def process_target(
    session: LinkedInSession,
    target: dict[str, Any],
    mode: str,
    state: dict[str, Any],
    dry_run: bool,
    bypass_safety: bool = False,
) -> dict[str, Any]:
    record = state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})
    timing = target.get("runtime_plan") or {}
    open_result = open_thread_for_target(session, target)
    if not open_result.get("ok"):
        return {
            "ok": False,
            "action": "blocked",
            "reason": "thread_open_failed",
            "open_result": open_result,
        }
    resolved_contact_name = (
        clean(open_result.get("resolved_contact_name")) or target["contact_name"]
    )
    record["resolved_contact_name"] = resolved_contact_name
    record["resolved_contact_name_source"] = open_result.get("name_source", "sheet")
    inspect = inspect_thread(session, resolved_contact_name)
    if not inspect.get("ok") or not inspect.get("identity_ok"):
        return {
            "ok": False,
            "action": "blocked",
            "reason": "identity_or_thread_inspection_failed",
            "inspect": inspect,
        }
    if inspect.get("latest_sender") == "recipient":
        return {"ok": True, "action": "mark_replied", "inspect": inspect}
    # TEMPORARY FILTER: skip prospects whose last message contains the "floating" follow-up
    if (
        "floating this to the top in case it got buried"
        in (inspect.get("latest_event_full") or "").lower()
    ):
        return {
            "ok": True,
            "action": "mark_unsure",
            "reason": "temp_filter_floating_followup",
            "inspect": inspect,
        }
    if inspect.get("latest_sender") != "self":
        return {"ok": True, "action": "mark_unsure", "inspect": inspect}
    outgoing_text = inspect.get("latest_own_event") or inspect.get("latest_event_full") or ""
    if message_appears_in_event(target["message"], outgoing_text):
        return {
            "ok": True,
            "action": "sent",
            "reconciled": True,
            "reason": "followup_message_already_present_in_thread",
            "inspect": inspect,
            "sent": {"confirmed": True, "ok": True, "reconciled": True},
        }
    expected_previous_message = target.get("expected_previous_message", "")
    expected_previous_step = target.get("expected_previous_step", "")
    if not expected_previous_message and not bypass_safety:
        return {
            "ok": True,
            "action": "mark_unsure",
            "reason": "expected_previous_message_missing",
            "expected_previous_step": expected_previous_step,
            "expected_previous_source": target.get("expected_previous_source", ""),
            "inspect": inspect,
        }
    if not bypass_safety and not message_appears_in_event(
        expected_previous_message, inspect.get("latest_event_full", "")
    ):
        return {
            "ok": True,
            "action": "mark_unsure",
            "reason": "latest_outgoing_message_does_not_match_expected_previous",
            "expected_previous_step": expected_previous_step,
            "expected_previous_source": target.get("expected_previous_source", ""),
            "inspect": inspect,
        }

    record.update(
        {
            "last_checked_at": datetime.now().isoformat(timespec="seconds"),
            "presence_status": inspect.get("presence_status"),
            "last_seen_text": inspect.get("last_seen_text"),
            "read_status": inspect.get("read_status"),
            "latest_receipt": inspect.get("latest_receipt"),
        }
    )
    if dry_run:
        return {"ok": True, "action": "would_send", "inspect": inspect}

    # ── HARD SAFETY GUARDS (cannot be bypassed) ──────────────────────────
    # Guard 1: Duplicate content — refuse if the message we're about to send
    # already appears in the latest outgoing message in this thread.
    if message_appears_in_event(target["message"], outgoing_text):
        return {
            "ok": False,
            "action": "blocked",
            "reason": "duplicate_message_detected",
            "detail": "The message to send already appears in the thread. Refusing to send a duplicate.",
            "inspect": inspect,
        }
    # Guard 2: Same-day send — refuse if we already sent a message TODAY.
    latest_event = inspect.get("latest_event") or ""
    if inspect.get("latest_sender") == "self" and latest_event.upper().lstrip().startswith("TODAY"):
        return {
            "ok": False,
            "action": "blocked",
            "reason": "same_day_send_detected",
            "detail": "A message was already sent to this person today. Refusing to send again.",
            "inspect": inspect,
        }
    # ── END HARD SAFETY GUARDS ───────────────────────────────────────────

    typed = human_type_message(session, target["message"])
    if not typed.get("ok") or not typed.get("send_ready"):
        return {
            "ok": False,
            "action": "blocked",
            "reason": "typing_or_send_not_ready",
            "typed": typed,
            "inspect": inspect,
        }
    pre_send = inspect_thread(session, resolved_contact_name)
    if pre_send.get("latest_sender") == "recipient":
        clear_composer(session)
        return {
            "ok": True,
            "action": "mark_replied",
            "reason": "recipient_replied_before_send",
            "inspect": pre_send,
        }
    if pre_send.get("latest_sender") != "self":
        clear_composer(session)
        return {
            "ok": True,
            "action": "mark_unsure",
            "reason": "latest_sender_uncertain_before_send",
            "inspect": pre_send,
        }
    if not bypass_safety and not message_appears_in_event(
        expected_previous_message, pre_send.get("latest_event_full", "")
    ):
        clear_composer(session)
        return {
            "ok": True,
            "action": "mark_unsure",
            "reason": "latest_outgoing_changed_before_send",
            "expected_previous_step": expected_previous_step,
            "expected_previous_source": target.get("expected_previous_source", ""),
            "inspect": pre_send,
        }
    attachment = None
    if target.get("audit_required"):
        attachment = attach_audit_pdf(session, target.get("audit_path", ""))
        if not attachment.get("ok"):
            clear_composer(session)
            remove_pending_attachment(session, Path(target.get("audit_path", "")).name)
            return {
                "ok": False,
                "action": "blocked",
                "reason": "pdf_attachment_failed",
                "attachment": attachment,
                "inspect": pre_send,
            }
        final_check = inspect_thread(session, resolved_contact_name)
        if final_check.get("latest_sender") == "recipient":
            clear_composer(session)
            remove_pending_attachment(session, Path(target.get("audit_path", "")).name)
            return {
                "ok": True,
                "action": "mark_replied",
                "reason": "recipient_replied_after_attachment",
                "inspect": final_check,
            }
        if not bypass_safety and (
            final_check.get("latest_sender") != "self"
            or not message_appears_in_event(
                expected_previous_message, final_check.get("latest_event_full", "")
            )
        ):
            clear_composer(session)
            remove_pending_attachment(session, Path(target.get("audit_path", "")).name)
            return {
                "ok": True,
                "action": "mark_unsure",
                "reason": "latest_sender_or_previous_message_uncertain_after_attachment",
                "inspect": final_check,
            }
    sleep_seconds(timing.get("pre_send_pause_sec"), dry_run=dry_run)
    sent = click_send_and_confirm(session, target["message"])
    if not sent.get("confirmed"):
        return {"ok": False, "action": "failed_send_confirm", "sent": sent, "inspect": inspect}
    return {
        "ok": True,
        "action": "sent",
        "inspect": inspect,
        "sent": sent,
        "attachment": attachment,
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(args.timezone))
    if now.weekday() == 6:
        return {"ok": False, "blockers": ["Follow-ups are disabled on Sundays"]}
    day = now.date().isoformat()
    path = Path(args.session) if args.session else session_path(day)
    existing_state = read_json(path)
    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    pipeline_ws = get_worksheet(spreadsheet, PIPELINE_TAB)
    normalized_sunday_due_dates = (
        0 if args.dry_run else normalize_existing_sunday_due_dates(pipeline_ws)
    )
    templates_ws = ensure_templates_tab(spreadsheet)
    templates = load_templates(templates_ws)
    history = read_history()
    first_message_drafts = load_first_message_drafts(spreadsheet)
    targets, skipped = build_due_targets(
        pipeline_ws,
        templates,
        now.date(),
        limit=args.limit,
        history=history,
        first_message_drafts=first_message_drafts,
        only_prospect_ids=args.only_prospect_id,
    )
    runtime_plan = build_followup_runtime_plan(spreadsheet, len(targets), cap=args.send_cap)
    for idx, target in enumerate(targets):
        if idx < len(runtime_plan):
            target["runtime_plan"] = runtime_plan[idx]
    payload = {
        "ok": True,
        "date": day,
        "prepared_at": now.isoformat(),
        "mode": "followup",
        "targets": targets,
        "skipped": skipped,
        "runtime_plan": runtime_plan,
        "send_cap": args.send_cap,
        "template_tab": FOLLOWUP_TEMPLATES_TAB,
        "sequence_tab": FOLLOWUP_SEQUENCE_TAB,
        "first_message_draft_count": len(first_message_drafts),
        "pipeline_tab": PIPELINE_TAB,
        "normalized_sunday_due_dates": normalized_sunday_due_dates,
        "runtime_state": existing_state.get("runtime_state", {}),
    }
    write_json(path, payload)
    append_jsonl(
        journal_path(day),
        {
            "event": "followup_prepared",
            "target_count": len(targets),
            "skipped_count": len(skipped),
            "prepared_at": now.isoformat(),
        },
    )
    return payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(args.timezone))
    if now.weekday() == 6:
        return {"ok": False, "blockers": ["Follow-ups are disabled on Sundays"]}
    day = now.date().isoformat()
    path = Path(args.session) if args.session else session_path(day)
    payload = read_json(path)
    if not payload.get("targets"):
        payload = prepare(args)
    targets = payload.get("targets", [])
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        return {
            "ok": False,
            "blockers": ["No due follow-up targets with enabled templates"],
            "session": str(path),
            "skipped": payload.get("skipped", []),
        }

    state = read_json(path)
    state.setdefault("runtime_state", {})
    history = read_history()
    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    pipeline_ws = get_worksheet(spreadsheet, PIPELINE_TAB)
    linkedin = LinkedInSession()
    connected = linkedin.connect(skip_rate_check=True)
    if not connected.get("ok"):
        return {"ok": False, "blockers": ["LinkedIn connect/preflight failed"], "detail": connected}
    open_result = open_messaging(linkedin)
    if not open_result.get("ok"):
        linkedin.disconnect()
        return {"ok": False, "blockers": ["LinkedIn Messaging not ready"], "detail": open_result}

    send_cap = max(1, int(args.send_cap or DEFAULT_RUN_SEND_CAP))
    summary = {
        "ok": True,
        "processed": 0,
        "sent": 0,
        "replied": 0,
        "unsure": 0,
        "failed": 0,
        "dry_run": args.dry_run,
        "mode": args.mode,
        "session": str(path),
        "send_cap": send_cap,
        "total_targets": len(targets),
        "remaining_due": 0,
        "next_run_recommended": False,
    }
    try:
        for target_index, target in enumerate(targets):
            if SHUTDOWN_REQUESTED:
                break
            record = state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})
            status = clean(record.get("status"))
            if status in {"sent", "replied", "unsure"}:
                summary.setdefault("skipped_done", 0)
                summary["skipped_done"] += 1
                continue
            if summary["sent"] >= send_cap:
                summary.setdefault("skipped_cap", 0)
                summary["skipped_cap"] += 1
                continue
            timing = target.get("runtime_plan") or {}
            sleep_seconds(timing.get("delay_sec"), dry_run=args.dry_run)
            result = process_target(
                linkedin, target, args.mode, state, args.dry_run, bypass_safety=args.bypass_safety
            )
            summary["processed"] += 1
            event = {
                "event": "followup_target_result",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "prospect_id": target.get("prospect_id"),
                "step": target.get("next_action"),
                **result,
            }
            append_jsonl(journal_path(day), event)
            if result.get("action") == "mark_replied":
                state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})[
                    "status"
                ] = "replied"
                if not args.dry_run:
                    update_row_fields(
                        pipeline_ws, int(target["row_number"]), {"Outcome": "Replied"}
                    )
                summary["replied"] += 1
            elif result.get("action") == "mark_unsure":
                state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})[
                    "status"
                ] = "unsure"
                if not args.dry_run:
                    update_row_fields(pipeline_ws, int(target["row_number"]), {"Outcome": "Unsure"})
                summary["unsure"] += 1
            elif result.get("action") in {"sent", "would_send"}:
                if result.get("action") == "sent":
                    state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})[
                        "status"
                    ] = "sent"
                    sent_date = datetime.now(ZoneInfo(args.timezone)).date()
                    next_action, next_due = next_pipeline_step(target["next_action"], sent_date)
                    update_row_fields(
                        pipeline_ws,
                        int(target["row_number"]),
                        {
                            "Current Progress": target["next_action"],
                            "Last Action Date": sent_date.isoformat(),
                            "Next Action": next_action,
                            "Next Action Due": next_due,
                        },
                    )
                    prospect_history = history.setdefault("sent_messages", {}).setdefault(
                        target["prospect_id"], {}
                    )
                    prospect_history[target["next_action"]] = {
                        "message": target["message"],
                        "sent_at": datetime.now(ZoneInfo(args.timezone)).isoformat(
                            timespec="seconds"
                        ),
                        "next_action": next_action,
                        "next_action_due": next_due,
                    }
                    write_history(history)
                    summary["sent"] += 1
                else:
                    summary["sent"] += 0
            else:
                state.setdefault("runtime_state", {}).setdefault(target["prospect_id"], {})[
                    "status"
                ] = "failed"
                if not args.dry_run:
                    update_row_fields(pipeline_ws, int(target["row_number"]), {"Outcome": "Unsure"})
                summary["failed"] += 1
            write_json(path, state)
            if summary["failed"] and not args.continue_on_failure:
                summary["ok"] = False
                break
            sleep_seconds(timing.get("after_send_delay_sec"), dry_run=args.dry_run)
            next_index = target_index + 1
            if next_index < len(targets):
                current_batch = (target.get("runtime_plan") or {}).get("batch")
                next_batch = (targets[next_index].get("runtime_plan") or {}).get("batch")
                if current_batch and next_batch and current_batch != next_batch:
                    batch_pause = next(
                        (
                            item.get("batch_pause_sec")
                            for item in payload.get("runtime_plan", [])
                            if item.get("batch") == current_batch and item.get("batch_pause_sec")
                        ),
                        0,
                    )
                    sleep_seconds(batch_pause, dry_run=args.dry_run)
    finally:
        linkedin.disconnect()
    final_state = state.get("runtime_state", {})
    remaining = 0
    for target in targets:
        status = clean(final_state.get(target.get("prospect_id"), {}).get("status"))
        if status not in {"sent", "replied", "unsure"}:
            remaining += 1
    summary["remaining_due"] = remaining
    summary["next_run_recommended"] = remaining > 0 and summary["sent"] >= send_cap
    append_jsonl(
        journal_path(day),
        {
            "event": "followup_run_summary",
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            **summary,
        },
    )
    write_json(path, state)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare/run LinkedIn Pipeline follow-ups.")
    parser.add_argument("--credentials", default=CREDS_PATH)
    parser.add_argument("--sheet-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--session", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["run"], default="run")
    parser.add_argument(
        "--only-prospect-id",
        action="append",
        default=[],
        help="Restrict prepare/run to one Prospect ID. Repeatable.",
    )
    parser.add_argument(
        "--send-cap",
        type=int,
        default=DEFAULT_RUN_SEND_CAP,
        help="Confirmed sends allowed in one run.",
    )
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--bypass-safety", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.prepare_only:
        result = prepare(args)
    else:
        result = run(args)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
