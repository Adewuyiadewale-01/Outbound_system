#!/usr/bin/env python3
"""Rank Pre-final rows into the Final outreach tab using P1/P2 activity."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import gspread

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from linkedin_helper import LinkedInSession, relative_days_from_time_text  # noqa: E402
from linkedin_outreach_session import _run_diversion, sequence_date_key  # noqa: E402
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import get_client, normalize_rows, open_sheet, require_columns  # noqa: E402

load_repo_env()


REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS
DEFAULT_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
DEFAULT_PREFINAL_TAB = "Pre-final"
DEFAULT_FINAL_TAB = "Final"
STATE_DIR = ROOT / "state" / "lead_exec_research" / "final_rankings"
OUTREACH_SEQUENCE_STATE_DIR = ROOT / "state" / "outreach_sequences"

BASE_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "Use",
]
PERSON_COLUMNS = ["Name", "Title", "LinkedIn", "Email"]
P1_COLUMNS = [f"P1 {column}" for column in PERSON_COLUMNS]
P2_COLUMNS = [f"P2 {column}" for column in PERSON_COLUMNS]
ACTIVITY_COLUMNS = ["P1 Activity", "P2 Activity"]
FINAL_REQUIRED_COLUMNS = (
    BASE_COLUMNS
    + P1_COLUMNS
    + ACTIVITY_COLUMNS[:1]
    + P2_COLUMNS
    + ACTIVITY_COLUMNS[1:]
    + ["Category"]
)
CATEGORY_PRIORITY = {
    "Hyper": 0,
    "High": 1,
    "Alpha-medium": 2,
    "Medium": 3,
    "Low": 4,
}
ACTIVITY_SCORE = {"Very active": 2, "Active": 1, "Not active": 0, "": 0}


def prepared_session_path_for_date(date_value: str) -> Path:
    return OUTREACH_SEQUENCE_STATE_DIR / f"{sequence_date_key(date_value)}-prepared.json"


def load_runtime_plan(path: str, date_value: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plan_path = Path(path).expanduser() if path else prepared_session_path_for_date(date_value)
    if not plan_path.exists():
        raise ValueError(f"Runtime plan not found: {plan_path}")
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    runtime_plan = payload.get("runtime_plan", [])
    if not isinstance(runtime_plan, list) or not runtime_plan:
        raise ValueError(f"Runtime plan is empty in {plan_path}")
    return runtime_plan, {
        "path": str(plan_path),
        "date": payload.get("date", date_value),
        "date_key": payload.get("date_key", sequence_date_key(date_value)),
        "prepared_at": payload.get("prepared_at", ""),
        "available_slots": len(runtime_plan),
    }


def runtime_plan_item_summary(plan_item: dict[str, Any] | None) -> dict[str, Any]:
    if not plan_item:
        return {}
    return {
        "slot_id": plan_item.get("slot_id"),
        "delay_sec": int(plan_item.get("delay_sec") or 0),
        "lead_diversion": clean_text(plan_item.get("lead_diversion")).lower() or "none",
        "lead_diversion_sec": plan_item.get("lead_diversion_sec"),
    }


def apply_runtime_plan_after_lead(
    session: LinkedInSession | None,
    plan_item: dict[str, Any] | None,
    *,
    is_last: bool,
    enabled: bool,
) -> dict[str, Any]:
    summary = runtime_plan_item_summary(plan_item)
    result: dict[str, Any] = {
        **summary,
        "enabled": bool(enabled and plan_item),
        "diversion_executed": False,
        "delay_applied": False,
    }
    if not enabled or not plan_item:
        return result

    diversion = summary.get("lead_diversion", "none")
    diversion_sec = summary.get("lead_diversion_sec")
    if session is not None and diversion and diversion != "none":
        diversion_result = _run_diversion(
            session=session,
            diversion=diversion,
            diversion_sec=int(diversion_sec) if diversion_sec not in {None, ""} else None,
        )
        result["diversion"] = diversion_result
        result["diversion_executed"] = bool(diversion_result.get("executed"))
        if not diversion_result.get("ok", True):
            result["ok"] = False
            result["error"] = diversion_result.get("error", "diversion_failed")
            return result

    delay_sec = int(summary.get("delay_sec") or 0)
    if not is_last and delay_sec > 0:
        time.sleep(delay_sec)
        result["delay_applied"] = True
    result["ok"] = True
    return result


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_url(value: Any) -> str:
    raw = clean_text(value).replace(" ", "")
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    return raw


def is_linkedin_profile_url(value: Any) -> bool:
    url = normalize_url(value).lower()
    return bool(re.match(r"^https?://([^/]+\.)?linkedin\.com/in/[^/?#]+/?", url))


def normalized_profile_key(value: Any) -> str:
    url = normalize_url(value).lower()
    url = re.sub(r"[?#].*$", "", url).rstrip("/")
    url = url.replace("https://linkedin.com/", "https://www.linkedin.com/")
    return url


def read_worksheet(
    credentials_path: Path, sheet_url: str, tab_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = spreadsheet.worksheet(tab_name)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{tab_name} is empty.")
    return values[0], normalize_rows(values)


def person_from_row(row: dict[str, Any], prefix: str) -> dict[str, str]:
    return {
        "name": clean_text(row.get(f"{prefix} Name")),
        "title": clean_text(row.get(f"{prefix} Title")),
        "linkedin": normalize_url(row.get(f"{prefix} LinkedIn")),
        "email": clean_text(row.get(f"{prefix} Email")),
        "activity": clean_text(row.get(f"{prefix} Activity")),
    }


def set_person(row: dict[str, Any], prefix: str, person: dict[str, str]) -> None:
    row[f"{prefix} Name"] = clean_text(person.get("name"))
    row[f"{prefix} Title"] = clean_text(person.get("title"))
    row[f"{prefix} LinkedIn"] = normalize_url(person.get("linkedin"))
    row[f"{prefix} Email"] = clean_text(person.get("email"))
    row[f"{prefix} Activity"] = clean_text(person.get("activity"))


def count_entries_within(tab: dict[str, Any], days_limit: int) -> int:
    count = 0
    for activity in tab.get("activities", []) or []:
        if is_aggregate_activity_container(activity):
            continue
        days = relative_days_from_time_text(activity.get("time_text", ""))
        if days is not None and days <= days_limit:
            count += 1
    return count


def is_aggregate_activity_container(activity: dict[str, Any]) -> bool:
    sample = clean_text(activity.get("card_text_sample")).lower()
    return sample.startswith("all activity posts comments") and "loaded " in sample


def activity_level(activity_detail: dict[str, Any]) -> str:
    if not activity_detail or activity_detail.get("error"):
        return ""
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    posts = tabs.get("posts", {}) or {}
    comments = tabs.get("comments", {}) or {}
    reactions = tabs.get("reactions", {}) or {}
    if any(tab.get("activity_classification_uncertain") for tab in (posts, comments, reactions)):
        return ""
    if not any(non_aggregate_entries(tab) for tab in (posts, comments, reactions)):
        return ""

    posts_7d = count_entries_within(posts, 7)
    comments_7d = count_entries_within(comments, 7)
    reactions_7d = count_entries_within(reactions, 7)
    if posts_7d >= 1 or comments_7d >= 2 or reactions_7d >= 2:
        return "Very active"

    posts_14d = count_entries_within(posts, 14)
    comments_30d = count_entries_within(comments, 30)
    reactions_30d = count_entries_within(reactions, 30)
    if posts_14d >= 1 or comments_30d + reactions_30d >= 5:
        return "Active"

    return "Not active"


def row_has_uncertain_activity(row: dict[str, Any]) -> bool:
    for prefix in ("P1", "P2"):
        if is_linkedin_profile_url(row.get(f"{prefix} LinkedIn")) and not clean_text(
            row.get(f"{prefix} Activity")
        ):
            return True
    return False


def category_for_row(row: dict[str, Any]) -> str:
    usable_linkedins = [
        row.get("P1 LinkedIn", ""),
        row.get("P2 LinkedIn", ""),
    ]
    linkedin_count = sum(1 for url in usable_linkedins if is_linkedin_profile_url(url))
    activities = [clean_text(row.get("P1 Activity")), clean_text(row.get("P2 Activity"))]

    if linkedin_count >= 2:
        if "Very active" in activities:
            return "Hyper"
        if "Active" in activities:
            return "High"
        return "Low"
    if linkedin_count == 1:
        if "Very active" in activities:
            return "Alpha-medium"
        if "Active" in activities:
            return "Medium"
        return "Low"
    return ""


def should_swap(p1_activity: str, p2_activity: str) -> bool:
    p2_score = ACTIVITY_SCORE.get(clean_text(p2_activity), 0)
    if p2_score <= 0:
        return False
    return p2_score > ACTIVITY_SCORE.get(clean_text(p1_activity), 0)


def rank_row(
    row: dict[str, Any],
    activity_reader: Callable[[str], dict[str, Any]],
    activity_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    ranked = {column: clean_text(row.get(column)) for column in BASE_COLUMNS}
    p1 = person_from_row(row, "P1")
    p2 = person_from_row(row, "P2")

    for person in (p1, p2):
        if not is_linkedin_profile_url(person.get("linkedin")):
            person["activity"] = ""
            continue
        key = normalized_profile_key(person["linkedin"])
        if key not in activity_cache:
            activity_cache[key] = activity_reader(person["linkedin"])
        person["activity"] = activity_level(activity_cache[key])

    if should_swap(p1.get("activity", ""), p2.get("activity", "")):
        p1, p2 = p2, p1

    set_person(ranked, "P1", p1)
    set_person(ranked, "P2", p2)
    ranked["Category"] = category_for_row(ranked)
    ranked["Engaged Person"] = clean_text(row.get("Engaged Person")) or "Person 1"
    if not ranked["Category"]:
        return None
    return ranked


def final_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        CATEGORY_PRIORITY.get(clean_text(row.get("Category")), 99),
        0 if is_linkedin_profile_url(row.get("P2 LinkedIn")) else 1,
        clean_text(row.get("Company")).lower(),
    )


def build_fixture_reader(fixture_path: str) -> Callable[[str], dict[str, Any]]:
    fixture = json.loads(Path(fixture_path).read_text()) if fixture_path else {}
    normalized = {normalized_profile_key(key): value for key, value in fixture.items()}

    def reader(profile_url: str) -> dict[str, Any]:
        return normalized.get(normalized_profile_key(profile_url), {})

    return reader


class LiveActivityReader:
    def __init__(self, timeout: float, retries: int):
        self.timeout = timeout
        self.retries = retries
        self.session = LinkedInSession()
        self.connected = False

    def connect(self) -> dict[str, Any]:
        if self.connected:
            return {"ok": True, "status": "already_connected"}
        connect_result = self.session.connect(skip_rate_check=True)
        if not connect_result.get("ok"):
            return connect_result
        self.connected = True
        return connect_result

    def close(self) -> None:
        if self.connected:
            self.session.disconnect()
            self.connected = False

    def read_once(self, profile_url: str) -> dict[str, Any]:
        connect_result = self.connect()
        if not connect_result.get("ok"):
            return {
                "error": True,
                "danger": connect_result.get("block_reason", "LinkedIn preflight failed"),
            }
        return self.session.read_activity_detail(profile_url, max_seconds=self.timeout)

    def __call__(self, profile_url: str) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        best_detail: dict[str, Any] = {}
        best_score = -1
        for _attempt_index in range(max(0, self.retries) + 1):
            detail = self.read_once(profile_url)
            attempts.append(detail)
            level = activity_level(detail)
            score = ACTIVITY_SCORE.get(level, -1)
            if score > best_score:
                best_detail = detail
                best_score = score
            if score > 0:
                best_detail["_attempts"] = [
                    activity_evidence(profile_url, item) for item in attempts
                ]
                return best_detail
        if best_detail:
            best_detail["_attempts"] = [activity_evidence(profile_url, item) for item in attempts]
        return best_detail


def build_live_reader(timeout: float, retries: int) -> LiveActivityReader:
    return LiveActivityReader(timeout, retries)


def non_aggregate_entries(tab: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in tab.get("activities", []) or []
        if not is_aggregate_activity_container(item)
    ]


def activity_evidence(profile_url: str, activity_detail: dict[str, Any]) -> dict[str, Any]:
    tabs = activity_detail.get("tabs", {}) if isinstance(activity_detail, dict) else {}
    evidence: dict[str, Any] = {
        "profile_url": profile_url,
        "level": activity_level(activity_detail),
        "error": bool(activity_detail.get("error")) if isinstance(activity_detail, dict) else True,
        "danger": activity_detail.get("danger", "") if isinstance(activity_detail, dict) else "",
        "attempts": activity_detail.get("_attempts", [])
        if isinstance(activity_detail, dict)
        else [],
        "tabs": {},
    }
    for tab_name in ("posts", "comments", "reactions"):
        tab = tabs.get(tab_name, {}) or {}
        entries = []
        for item in non_aggregate_entries(tab)[:8]:
            time_text = clean_text(item.get("time_text"))
            entries.append(
                {
                    "time_text": time_text,
                    "days": relative_days_from_time_text(time_text),
                    "sample": clean_text(item.get("card_text_sample"))[:160],
                }
            )
        evidence["tabs"][tab_name] = {
            "total_visible": int(tab.get("total_visible", 0) or 0),
            "real_entries": len(non_aggregate_entries(tab)),
            "uncertain": bool(tab.get("activity_classification_uncertain")),
            "entries": entries,
        }
    return evidence


def source_rows_for_ranking(rows: list[dict[str, Any]], limit: int = 0) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        if not clean_text(row.get("ID")) and not clean_text(row.get("Company")):
            continue
        if not is_linkedin_profile_url(row.get("P1 LinkedIn")) and not is_linkedin_profile_url(
            row.get("P2 LinkedIn")
        ):
            continue
        selected.append(row)
        if limit and len(selected) >= limit:
            break
    return selected


def write_final_rows(
    credentials_path: Path,
    sheet_url: str,
    final_tab: str,
    rows: list[dict[str, Any]],
    dry_run: bool,
) -> int:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = spreadsheet.worksheet(final_tab)
    headers = worksheet.row_values(1)
    require_columns(headers, FINAL_REQUIRED_COLUMNS, final_tab)
    if dry_run:
        return 0

    last_column = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip("1")
    worksheet.batch_clear([f"A2:{last_column}{worksheet.row_count}"])
    if not rows:
        return 0
    payload = [[row.get(header, "") for header in headers] for row in rows]
    start_cell = gspread.utils.rowcol_to_a1(2, 1)
    end_cell = gspread.utils.rowcol_to_a1(1 + len(payload), len(headers))
    worksheet.update(
        range_name=f"{start_cell}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return len(rows)


def state_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def run(args: argparse.Namespace) -> dict[str, Any]:
    headers, source_rows = read_worksheet(Path(args.credentials), args.sheet_url, args.prefinal_tab)
    require_columns(headers, BASE_COLUMNS + P1_COLUMNS + P2_COLUMNS, args.prefinal_tab)

    runtime_plan: list[dict[str, Any]] = []
    runtime_plan_source: dict[str, Any] = {}
    runtime_enabled = bool(not args.activity_fixture and not args.no_runtime_plan)
    candidate_limit = args.limit
    if runtime_enabled:
        runtime_plan, runtime_plan_source = load_runtime_plan(
            args.runtime_plan_path, args.sequence_date
        )
        available_slots = len(runtime_plan)
        candidate_limit = min(args.limit, available_slots) if args.limit else available_slots
        runtime_plan_source["candidate_limit"] = candidate_limit
        runtime_plan_source["limited_by"] = (
            "cli_limit" if args.limit and args.limit < available_slots else "runtime_plan_slots"
        )

    candidates = source_rows_for_ranking(source_rows, limit=candidate_limit)
    if runtime_enabled:
        runtime_plan = runtime_plan[: len(candidates)]
        runtime_plan_source["mapped_slots"] = len(runtime_plan)

    if args.activity_fixture:
        activity_reader = build_fixture_reader(args.activity_fixture)
    else:
        activity_reader = build_live_reader(args.activity_timeout, args.activity_retries)

    activity_cache: dict[str, dict[str, Any]] = {}
    ranked_rows: list[dict[str, Any]] = []
    runtime_events: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(candidates):
            plan_item = runtime_plan[index] if index < len(runtime_plan) else None
            ranked = rank_row(row, activity_reader, activity_cache)
            if ranked:
                if plan_item:
                    ranked["_runtime_slot_id"] = plan_item.get("slot_id")
                ranked_rows.append(ranked)

            runtime_event = apply_runtime_plan_after_lead(
                getattr(activity_reader, "session", None),
                plan_item,
                is_last=index == len(candidates) - 1,
                enabled=runtime_enabled,
            )
            runtime_event.update(
                {
                    "lead_id": row.get("ID", ""),
                    "company": row.get("Company", ""),
                    "candidate_index": index + 1,
                }
            )
            runtime_events.append(runtime_event)
            if runtime_event.get("ok") is False:
                break
    finally:
        close = getattr(activity_reader, "close", None)
        if callable(close):
            close()

    ranked_rows.sort(key=final_sort_key)
    uncertain_rows = [
        {
            "lead_id": row.get("ID", ""),
            "company": row.get("Company", ""),
            "p1": row.get("P1 Name", ""),
            "p1_activity": row.get("P1 Activity", ""),
            "p2": row.get("P2 Name", ""),
            "p2_activity": row.get("P2 Activity", ""),
        }
        for row in ranked_rows
        if row_has_uncertain_activity(row)
    ]
    runtime_failures = [event for event in runtime_events if event.get("ok") is False]
    blocked_runtime = bool(runtime_failures and not args.dry_run)
    blocked_uncertain = bool(uncertain_rows and not args.dry_run and not args.allow_uncertain_write)
    rows_written = 0
    if not blocked_uncertain and not blocked_runtime:
        rows_written = write_final_rows(
            Path(args.credentials), args.sheet_url, args.final_tab, ranked_rows, args.dry_run
        )
    status = "dry_run" if args.dry_run else "written"
    if blocked_runtime:
        status = "blocked_runtime_plan"
    elif blocked_uncertain:
        status = "blocked_uncertain_activity"
    result = {
        "ok": not blocked_uncertain and not blocked_runtime,
        "status": status,
        "dry_run": bool(args.dry_run),
        "source_tab": args.prefinal_tab,
        "final_tab": args.final_tab,
        "source_rows_seen": len(source_rows),
        "candidate_rows": len(candidates),
        "ranked_rows": len(ranked_rows),
        "rows_written": rows_written,
        "category_counts": {
            category: len([row for row in ranked_rows if row.get("Category") == category])
            for category in CATEGORY_PRIORITY
        },
        "activity_profiles_read": len(activity_cache),
        "lead_ids": [row.get("ID", "") for row in ranked_rows],
        "uncertain_activity_rows": uncertain_rows,
        "runtime_failures": runtime_failures,
        "runtime_plan": {
            "enabled": runtime_enabled,
            "source": runtime_plan_source,
            "events": runtime_events,
            "delays_applied": len(
                [event for event in runtime_events if event.get("delay_applied")]
            ),
            "diversions_executed": len(
                [event for event in runtime_events if event.get("diversion_executed")]
            ),
        },
    }
    path = state_path()
    payload = {
        **result,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": hashlib.sha256(
            json.dumps(ranked_rows, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "rows": ranked_rows,
        "activity_evidence": {
            key: activity_evidence(key, detail) for key, detail in sorted(activity_cache.items())
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    result["state_file"] = str(path)
    return result


def fake_activity(
    *, posts: Sequence[str] = (), comments: Sequence[str] = (), reactions: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "tabs": {
            "posts": {"activities": [{"time_text": value} for value in posts]},
            "comments": {"activities": [{"time_text": value} for value in comments]},
            "reactions": {"activities": [{"time_text": value} for value in reactions]},
        }
    }


def fake_aggregate_activity() -> dict[str, Any]:
    return {
        "tabs": {
            "posts": {"activities": []},
            "comments": {"activities": [{"time_text": "11mo"}]},
            "reactions": {
                "activities": [
                    {
                        "time_text": "1w",
                        "card_text_sample": "All activity\nPosts\nComments\nImages\nReactions\nLoaded 20 Reactions posts\nFeed post number 1",
                    },
                    {
                        "time_text": "1w",
                        "card_text_sample": "Feed post number 1\nArno van Brakel likes this",
                    },
                    {
                        "time_text": "2w",
                        "card_text_sample": "Feed post number 2\nArno van Brakel likes this",
                    },
                ]
            },
        }
    }


def run_self_tests() -> None:
    assert activity_level(fake_activity(posts=["6d"])) == "Very active"
    assert activity_level(fake_activity(comments=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(reactions=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(posts=["13d"])) == "Active"
    assert (
        activity_level(fake_activity(comments=["20d", "21d", "22d"], reactions=["23d", "24d"]))
        == "Active"
    )
    assert activity_level(fake_activity()) == ""
    assert activity_level(fake_activity(comments=["300d"])) == "Not active"
    assert activity_level(fake_aggregate_activity()) == "Not active"

    base = {
        "ID": "1",
        "Company": "Example",
        "Website": "https://example.com",
        "Company LinkedIn": "",
        "Emp Count": "10",
        "Source Tab": "",
        "Use": "",
        "P1 Name": "Founder One",
        "P1 Title": "Founder",
        "P1 LinkedIn": "https://www.linkedin.com/in/founder-one",
        "P1 Email": "",
        "P2 Name": "CEO Two",
        "P2 Title": "CEO",
        "P2 LinkedIn": "https://www.linkedin.com/in/ceo-two",
        "P2 Email": "",
    }
    fixture = {
        normalized_profile_key(base["P1 LinkedIn"]): fake_activity(),
        normalized_profile_key(base["P2 LinkedIn"]): fake_activity(posts=["2d"]),
    }
    ranked = rank_row(base, lambda url: fixture[normalized_profile_key(url)], {})
    assert ranked is not None
    assert ranked["P1 Name"] == "CEO Two"
    assert ranked["P1 Activity"] == "Very active"
    assert ranked["Category"] == "Hyper"

    fixture = {
        normalized_profile_key(base["P1 LinkedIn"]): fake_activity(posts=["1d"]),
        normalized_profile_key(base["P2 LinkedIn"]): fake_activity(posts=["12d"]),
    }
    ranked = rank_row(base, lambda url: fixture[normalized_profile_key(url)], {})
    assert ranked is not None
    assert ranked["P1 Name"] == "Founder One"
    assert ranked["Category"] == "Hyper"

    one_linkedin = dict(base)
    one_linkedin["P2 LinkedIn"] = ""
    ranked = rank_row(one_linkedin, lambda _url: fake_activity(posts=["1d"]), {})
    assert ranked is not None
    assert ranked["Category"] == "Alpha-medium"

    blank = rank_row(base, lambda _url: {"error": True}, {})
    assert blank is not None
    assert blank["Category"] == "Low"
    assert blank["P1 Activity"] == ""
    assert blank["P2 Activity"] == ""

    inactive = rank_row(base, lambda _url: fake_activity(comments=["300d"]), {})
    assert inactive is not None
    assert inactive["P1 Name"] == "Founder One"
    assert inactive["P1 Activity"] == "Not active"
    assert inactive["P2 Activity"] == "Not active"
    assert inactive["Category"] == "Low"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank Pre-final leads into Final using P1/P2 activity"
    )
    parser.add_argument(
        "--sheet-url", default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_SHEET_URL)
    )
    parser.add_argument(
        "--prefinal-tab", default=os.environ.get("LEAD_RESEARCH_PREFINAL_TAB", DEFAULT_PREFINAL_TAB)
    )
    parser.add_argument(
        "--final-tab", default=os.environ.get("LEAD_RESEARCH_FINAL_TAB", DEFAULT_FINAL_TAB)
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--activity-timeout", type=float, default=45.0)
    parser.add_argument("--activity-retries", type=int, default=1)
    parser.add_argument("--activity-fixture", default="")
    parser.add_argument(
        "--sequence-date",
        default=os.environ.get("OUTREACH_SEQUENCE_DATE", date.today().isoformat()),
    )
    parser.add_argument(
        "--runtime-plan-path", default=os.environ.get("OUTREACH_RUNTIME_PLAN_PATH", "")
    )
    parser.add_argument("--no-runtime-plan", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-uncertain-write", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("prefinal_to_final self-tests passed")
        return 0
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    result = run(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
