#!/usr/bin/env python3
"""
Lead executive research workflow.

Reads grouped company/employee rows from a Google Sheet, creates a structured
local JSON run file, searches for top executives without paid APIs, reconciles
LinkedIn URLs against existing employee rows, and writes finalized P1/P2 rows
to a Prospects-style destination tab.
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import smtplib
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import WorksheetNotFound

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from lead_research_archive import (  # noqa: E402
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    has_reusable_research,
    hydrate_computation_lead,
)
from prefinal_queue import enqueue_batch, record_prefinal_publish, rows_fingerprint  # noqa: E402
from runtime_environment import load_repo_env  # noqa: E402
from sheets_helper import (  # noqa: E402
    format_sheet_date,
    get_client,
    get_worksheet,
    normalize_rows,
    open_sheet,
    sheet_values_equal,
)

load_repo_env()


REPO_CREDS = ROOT / "credentials" / "google-sheets.json"
OPENCLAW_CREDS = Path.home() / ".openclaw" / "credentials" / "google-sheets.json"
DEFAULT_CREDS = REPO_CREDS if REPO_CREDS.exists() else OPENCLAW_CREDS
STATE_DIR = ROOT / "state" / "lead_exec_research"
RUNS_DIR = STATE_DIR / "runs"
SNAPSHOTS_DIR = STATE_DIR / "snapshots"
COMPUTATIONS_DIR = STATE_DIR / "computations"
PROMPTS_DIR = STATE_DIR / "prompts"
SEARCH_RESULTS_DIR = STATE_DIR / "search_results"
SEARCH_TASKS_DIR = STATE_DIR / "search_tasks"
BRIDGES_DIR = STATE_DIR / "bridges"
RESEARCH_ARCHIVE_DIR = STATE_DIR / "research_archive"
DEFAULT_RESEARCH_ARCHIVE_INDEX = RESEARCH_ARCHIVE_DIR / "index.json"
LEAD_PREP_CONFIG_PATH = ROOT / "state" / "lead_prep_orchestration_config.json"

SOURCE_COLUMNS = [
    "ID",
    "Company Name",
    "Company Website",
    "Company Linkedin",
    "Person Name",
    "Person Role",
    "Person Email",
    "Person Linkedin",
    "Class",
]

SOURCE_ALIASES = {
    "ID": ["ID", "id"],
    "Company Name": ["Company Name", "Company", "name"],
    "Company Website": ["Company Website", "Website", "website"],
    "Company Linkedin": ["Company Linkedin", "Company LinkedIn", "LinkedIn", "linkedin"],
    "Company Employee Count": [
        "Company Employee Count",
        "LinkedIn employees",
        "Emp Count",
        "Employee Count",
    ],
    "Person Name": ["Person Name", "Name"],
    "Person Role": ["Person Role", "Role", "Title"],
    "Person Email": ["Person Email", "Email"],
    "Person Linkedin": ["Person Linkedin", "Person LinkedIn", "LinkedIn URL", "Profile URL"],
    "Class": ["Class", "Source Tab"],
}

DESTINATION_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
]
OPTIONAL_DESTINATION_COLUMNS = [
    "Use",
]
FINAL_BRIDGE_COLUMNS = [
    "P1 Activity",
    "P2 Activity",
    "Category",
]
PROSPECTS_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P1 Activity",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
    "P2 Activity",
    "Engaged Person",
    "Touch Method",
    "Outreach Status",
    "Outcome",
    "Date Queued",
    "Notes",
]

DEFAULT_SHEET_URL = os.environ.get("LEAD_RESEARCH_SHEET_URL", "")
DEFAULT_SOURCE_TAB = "Employee_db"
DEFAULT_DESTINATION_TAB = "Pre-final"
DEFAULT_FINAL_TAB = "Final"
DEFAULT_REVIEW_TAB = "Lead Review"
DEFAULT_NOTIFY_EMAIL = "fixmypresencenl1@gmail.com"
DEFAULT_NOTIFICATION_QUEUE_TAB = "Notification Queue"
DEFAULT_OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")
DEFAULT_PROSPECTS_TAB = "Prospects"
DEFAULT_OUTREACH_CONTROL_TAB = "Outreach Control"
OUTREACH_CONTROL_PROSPECTS_START_ROW = "Prospects Start Row"

REVIEW_COLUMNS = [
    "Run ID",
    "Company Name",
    "Company Website",
    "Emp Count",
    "Approved",
    "Use",
]
REVIEW_OVERLAP_COLUMNS = [
    "Primary Lane",
    "Prep Wave",
    "Overlap Status",
    "Archive Entry ID",
]
PRIMARY_LANES = ("Automation", "Design")

NOTIFICATION_QUEUE_COLUMNS = [
    "Created At",
    "Status",
    "To",
    "Subject",
    "Body",
    "Run ID",
    "Run File",
    "Sent At",
    "Error",
]

EXEC_TITLE_PATTERNS = [
    ("Founder", 96),
    ("Co-Founder", 96),
    ("Chief Executive Officer", 100),
    ("CEO", 100),
    ("Managing Director", 95),
    ("President", 92),
    ("Owner", 90),
    ("Partner", 84),
    ("Chief Technology Officer", 82),
    ("CTO", 82),
    ("Chief Operating Officer", 82),
    ("COO", 82),
    ("Chief Financial Officer", 78),
    ("CFO", 78),
    ("Head of", 72),
    ("Director", 66),
]

ROLE_KEYWORDS = [
    "ceo",
    "chief executive",
    "founder",
    "co-founder",
    "managing director",
    "president",
    "owner",
    "partner",
    "cto",
    "chief technology",
    "coo",
    "chief operating",
    "cfo",
    "chief financial",
    "head of",
    "director",
]


def now_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    COMPUTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    SEARCH_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    SEARCH_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGES_DIR.mkdir(parents=True, exist_ok=True)
    RESEARCH_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def approval_gate_enabled() -> bool:
    """Read the shared Lead Prep setting without making processing depend on the UI."""
    try:
        payload = json.loads(LEAD_PREP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("approval_gate_enabled", False))


def resolve_ignore_review_approval(args: argparse.Namespace) -> bool:
    """Respect an explicit CLI choice, otherwise use the dashboard configuration."""
    requested = getattr(args, "ignore_review_approval", None)
    if requested is not None:
        return bool(requested)
    return not approval_gate_enabled()


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def strip_accents(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_url(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    raw = raw.replace(" ", "")
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.startswith("linkedin.com"):
        raw = "https://www." + raw
    return raw


def meaningful_name_parts(name: str) -> list[str]:
    parts = re.split(r"[^a-zA-Z0-9]+", name.lower())
    blocked = {"de", "den", "der", "van", "von", "the", "and", "of", "mr", "mrs", "ms"}
    return [p for p in parts if len(p) >= 4 and p not in blocked]


def token_overlap_score(left: str, right: str) -> float:
    left_tokens = set(meaningful_name_parts(left))
    right_tokens = set(meaningful_name_parts(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def linkedin_url_name_score(name: str, url: str) -> float:
    url_norm = normalize_key(urllib.parse.unquote(url))
    parts = meaningful_name_parts(name)
    if not parts or not url_norm:
        return 0.0
    hits = sum(1 for part in parts if part in url_norm)
    return hits / len(parts)


def is_linkedin_profile_url(url: str) -> bool:
    return bool(re.search(r"linkedin\.[a-z.]+/in/", normalize_url(url), flags=re.I))


def looks_like_company_row(row: dict[str, Any]) -> bool:
    return bool(clean_text(row.get("ID")) and clean_text(row.get("Company Name")))


def looks_like_employee_row(row: dict[str, Any]) -> bool:
    fields = ["Person Name", "Person Role", "Person Email", "Person Linkedin"]
    return any(clean_text(row.get(field)) for field in fields)


def require_columns(headers: Sequence[str], required: Sequence[str], tab_name: str) -> None:
    missing = [column for column in required if column not in headers]
    if missing:
        raise ValueError(f"{tab_name} is missing required columns: {', '.join(missing)}")


def source_value(row: dict[str, Any], canonical_column: str) -> str:
    for candidate in SOURCE_ALIASES.get(canonical_column, [canonical_column]):
        value = clean_text(row.get(candidate))
        if value:
            return value
    return ""


def canonicalize_source_rows(
    headers: Sequence[str], rows: list[dict[str, Any]], source_tab: str
) -> list[dict[str, Any]]:
    available = set(headers)
    if not any(column in available for column in SOURCE_ALIASES["Company Name"]):
        raise ValueError(
            f"{source_tab} must contain a company name column. "
            f"Accepted names: {', '.join(SOURCE_ALIASES['Company Name'])}"
        )

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        for canonical_column in SOURCE_ALIASES:
            normalized[canonical_column] = source_value(row, canonical_column)
        if not normalized["ID"] and normalized["Company Name"]:
            normalized["ID"] = f"{source_tab}-{row.get('_row_number')}"
        if not normalized["Class"]:
            normalized["Class"] = source_tab
        normalized_rows.append(normalized)
    return normalized_rows


def read_worksheet(
    credentials_path: Path, sheet_url: str, tab_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, tab_name)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError(f"{tab_name} is empty.")
    headers = values[0]
    rows = normalize_rows(values)
    return headers, rows


def load_destination_ids(credentials_path: Path, sheet_url: str, destination_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, destination_tab)
    except Exception:
        return set()
    if "ID" not in headers:
        return set()
    return {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}


def load_destination_company_by_id(
    credentials_path: Path, sheet_url: str, destination_tab: str
) -> dict[str, str]:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, destination_tab)
    except Exception:
        return {}
    if "ID" not in headers:
        return {}
    company_column = (
        "Company" if "Company" in headers else "Company Name" if "Company Name" in headers else ""
    )
    if not company_column:
        return {}
    return {
        clean_text(row.get("ID")): clean_text(row.get(company_column))
        for row in rows
        if clean_text(row.get("ID"))
    }


def load_reviewed_ids(credentials_path: Path, sheet_url: str, review_tab: str) -> set:
    try:
        headers, rows = read_worksheet(credentials_path, sheet_url, review_tab)
    except Exception:
        return set()
    if "Run ID" not in headers:
        return set()
    return {clean_text(row.get("Run ID")) for row in rows if clean_text(row.get("Run ID"))}


def group_source_rows(rows: list[dict[str, Any]], source_tab: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        if looks_like_company_row(row):
            current = {
                "id": clean_text(row.get("ID")),
                "company": {
                    "name": clean_text(row.get("Company Name")),
                    "website": normalize_url(row.get("Company Website")),
                    "linkedin": normalize_url(row.get("Company Linkedin")),
                    "employee_count": clean_text(row.get("Company Employee Count")),
                    "class": clean_text(row.get("Class")),
                },
                "source_tab": source_tab,
                "source_rows": {
                    "company_row": row.get("_row_number"),
                    "employee_rows": [],
                },
                "employees_from_sheet": [],
                "status": "pending",
                "errors": [],
            }
            groups.append(current)
            if looks_like_employee_row(row):
                add_employee(current, row)
            continue
        if current and looks_like_employee_row(row):
            add_employee(current, row)
    return groups


def add_employee(group: dict[str, Any], row: dict[str, Any]) -> None:
    name = clean_text(row.get("Person Name"))
    employee = {
        "name": name,
        "role": clean_role_for_person(name, row.get("Person Role")),
        "email": clean_text(row.get("Person Email")),
        "linkedin": normalize_url(row.get("Person Linkedin")),
        "source_row": row.get("_row_number"),
    }
    group["employees_from_sheet"].append(employee)
    group["source_rows"]["employee_rows"].append(row.get("_row_number"))


def chunks(items: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def assign_primary_lanes(leads: list[dict[str, Any]]) -> dict[str, int]:
    """Assign balanced lanes, grouped for a Design-first review pass."""
    automation_count = (len(leads) + 1) // 2
    design_count = len(leads) - automation_count
    for lead in leads[:design_count]:
        lead["primary_lane"] = "Design"
    for lead in leads[design_count:]:
        lead["primary_lane"] = "Automation"
    return {"Automation": automation_count, "Design": design_count}


class SearchClient:
    def __init__(self, delay_min: float = 2.0, delay_max: float = 5.0, timeout: int = 25):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self.last_request_at = 0.0

    def search(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        self._delay()
        errors = []
        for engine in ("bing", "yahoo", "duckduckgo"):
            try:
                results = self._search_engine(engine, query, limit)
                self.last_request_at = time.time()
                return results
            except Exception as exc:
                errors.append(f"{engine}: {exc}")
        raise RuntimeError("; ".join(errors))

    def _search_engine(self, engine: str, query: str, limit: int) -> list[dict[str, str]]:
        if engine == "bing":
            url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
        elif engine == "yahoo":
            url = "https://search.yahoo.com/search?" + urllib.parse.urlencode({"p": query})
        else:
            url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", errors="ignore")
        if engine == "bing":
            return parse_bing_results(body, limit=limit)
        if engine == "yahoo":
            return parse_yahoo_results(body, limit=limit)
        return parse_duckduckgo_results(body, limit=limit)

    def _delay(self) -> None:
        if self.last_request_at <= 0:
            return
        target = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.time() - self.last_request_at
        if elapsed < target:
            time.sleep(target - elapsed)


def parse_duckduckgo_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>', body)
    for block in blocks:
        if "result__a" not in block:
            continue
        link_match = re.search(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not link_match:
            continue
        raw_url = html.unescape(link_match.group(1))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S
        )
        if not snippet_match:
            snippet_match = re.search(
                r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>', block, re.S
            )
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        url = unwrap_duckduckgo_url(raw_url)
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_bing_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>', body)
    for block in blocks:
        link_match = re.search(
            r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>\s*</h2>", block, re.S
        )
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_yahoo_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<li>\s*<div[^>]+class="[^"]*\balgo\b[^"]*"[^>]*>', body)
    for block in blocks:
        link_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', block, re.S)
        if not link_match:
            continue
        url = unwrap_yahoo_url(html.unescape(link_match.group(1)))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(
            r'<div[^>]+class="[^"]*\bcompText\b[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.S
        )
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def unwrap_yahoo_url(raw_url: str) -> str:
    match = re.search(r"/RU=([^/]+)/", raw_url)
    if match:
        return urllib.parse.unquote(match.group(1))
    return raw_url


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html.unescape(text))


def unwrap_duckduckgo_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return html.unescape(query["uddg"][0])
    return raw_url


def title_weight(text: str) -> int:
    lowered = text.lower()
    best = 0
    for title, weight in EXEC_TITLE_PATTERNS:
        title_pattern = r"\b" + re.escape(title.lower()).replace(r"\ ", r"\s+") + r"\b"
        if re.search(title_pattern, lowered):
            best = max(best, weight)
    return best


def seniority_score(person: dict[str, Any]) -> int:
    return title_weight(clean_text(person.get("title") or person.get("role") or ""))


def compact_company_name(company_name: str) -> str:
    compact = re.split(r"\s+\|\s+", clean_text(company_name), maxsplit=1)[0]
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact or clean_text(company_name)


def search_query_variants(person_name: str, company_name: str) -> list[str]:
    name = clean_text(person_name)
    company = clean_text(company_name)
    short_company = compact_company_name(company)
    ascii_name = clean_text(strip_accents(name))
    ascii_company = clean_text(strip_accents(short_company))
    candidates = [
        f'"{name}" "{company}" LinkedIn',
        f"{name} {company} LinkedIn",
        f'"{name}" "{short_company}" LinkedIn',
        f"{name} {short_company} LinkedIn",
    ]
    if ascii_name != name or ascii_company != short_company:
        candidates.extend(
            [
                f'"{ascii_name}" "{ascii_company}" LinkedIn',
                f"{ascii_name} {ascii_company} LinkedIn",
            ]
        )
    seen = set()
    variants = []
    for query in candidates:
        query = clean_text(query)
        key = query.lower()
        if query and key not in seen:
            variants.append(query)
            seen.add(key)
    return variants


def extract_exec_candidates(
    company_name: str, results: list[dict[str, str]]
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for result in results:
        haystack = f"{result.get('title', '')} {result.get('snippet', '')}"
        for name, role in extract_name_role_pairs(haystack):
            key = normalize_key(name)
            if not key:
                continue
            score = title_weight(role or haystack)
            if normalize_key(company_name) and normalize_key(company_name) in normalize_key(
                haystack
            ):
                score += 8
            if is_linkedin_profile_url(result.get("url", "")):
                score += 6
            existing = candidates.get(key)
            item = {
                "name": name,
                "role": role,
                "source": "exec_search",
                "source_url": result.get("url", ""),
                "source_title": result.get("title", ""),
                "source_snippet": result.get("snippet", ""),
                "score": score,
            }
            if existing is None or item["score"] > existing["score"]:
                candidates[key] = item
    return sorted(candidates.values(), key=lambda item: item["score"], reverse=True)


def extract_name_role_pairs(text: str) -> list[tuple[str, str]]:
    cleaned = clean_text(text)
    pairs: list[tuple[str, str]] = []
    name_pattern = r"([A-Z][a-zA-Z'`.-]+(?:\s+(?:van|von|de|den|der|[A-Z][a-zA-Z'`.-]+)){1,4})"
    role_pattern = r"\b(CEO|Chief Executive Officer|Founder|Co-Founder|Managing Director|President|Owner|Partner|CTO|COO|CFO|Head of [A-Za-z &]+|Director\b[^,;|.-]*)"

    for match in re.finditer(name_pattern + r".{0,80}?" + role_pattern, cleaned):
        pairs.append((clean_person_name(match.group(1)), clean_text(match.group(2))))
    for match in re.finditer(role_pattern + r".{0,80}?" + name_pattern, cleaned):
        pairs.append((clean_person_name(match.group(2)), clean_text(match.group(1))))
    return [(name, role) for name, role in pairs if is_plausible_person_name(name)]


def clean_person_name(name: str) -> str:
    name = re.sub(r"\b(LinkedIn|Profile|About|Team|Leadership|People|Company)\b", "", name)
    return clean_text(name.strip(" -|,.;:"))


def clean_role_for_person(name: str, role: str) -> str:
    cleaned = clean_text(role)
    if not cleaned:
        return ""
    name_key = normalize_key(name)
    role_key = normalize_key(cleaned)
    if name_key and role_key.startswith(name_key + " "):
        words_to_drop = len(name_key.split())
        return clean_text(" ".join(cleaned.split()[words_to_drop:]))
    if role_key == name_key:
        return ""
    return cleaned


def is_plausible_person_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 5:
        return False
    blocked = {"Chief Executive", "Managing Director", "Company LinkedIn"}
    suffixes = {
        "limited",
        "ltd",
        "bv",
        "b.v",
        "inc",
        "llc",
        "plc",
        "capital",
        "management",
        "company",
    }
    lowered_parts = {part.lower().strip(".") for part in parts}
    if name in blocked or lowered_parts & suffixes:
        return False
    return sum(1 for part in parts if part[:1].isupper()) >= 2


def search_execs_for_company(
    search_client: SearchClient, company: dict[str, str], max_execs: int = 3
) -> list[dict[str, Any]]:
    company_name = company.get("name", "")
    queries = [
        f'"{company_name}" CEO founder managing director',
        f'"{company_name}" leadership executive team',
        f'site:linkedin.com/in "{company_name}" CEO founder',
    ]
    all_results: list[dict[str, str]] = []
    for query in queries:
        try:
            all_results.extend(search_client.search(query, limit=5))
        except Exception as exc:
            all_results.append({"title": "", "url": "", "snippet": f"SEARCH_ERROR: {exc}"})
    candidates = extract_exec_candidates(company_name, all_results)
    return candidates[:max_execs]


def employee_role_score(role: str) -> int:
    return title_weight(role)


def fallback_execs_from_employees(
    employees: list[dict[str, Any]], max_execs: int = 3
) -> list[dict[str, Any]]:
    ranked = []
    for employee in employees:
        if not employee.get("name"):
            continue
        ranked.append(
            {
                "name": employee.get("name", ""),
                "role": employee.get("role", ""),
                "source": "employee_rows",
                "score": employee_role_score(employee.get("role", "")),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:max_execs]


def match_employee(
    exec_item: dict[str, Any], employees: list[dict[str, Any]]
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for employee in employees:
        score = token_overlap_score(exec_item.get("name", ""), employee.get("name", ""))
        if score > best_score:
            best = employee
            best_score = score
    if best and best_score >= 0.45:
        matched = dict(best)
        matched["match_score"] = round(best_score, 3)
        return matched
    return None


def score_search_candidate(
    exec_item: dict[str, Any], company: dict[str, str], result: dict[str, str]
) -> float:
    name = exec_item.get("name", "")
    title = result.get("title", "")
    snippet = result.get("snippet", "")
    url = result.get("url", "")
    haystack = normalize_key(f"{title} {snippet}")
    score = 0.0
    if is_linkedin_profile_url(url):
        score += 0.35
    score += linkedin_url_name_score(name, url) * 0.30
    if token_overlap_score(name, f"{title} {snippet}") >= 0.45:
        score += 0.20
    company_name = normalize_key(company.get("name", ""))
    if company_name and company_name in haystack:
        score += 0.10
    if any(keyword in haystack for keyword in ROLE_KEYWORDS):
        score += 0.05
    return round(min(score, 1.0), 3)


def search_linkedin_for_exec(
    search_client: SearchClient,
    exec_item: dict[str, Any],
    company: dict[str, str],
) -> dict[str, Any]:
    queries = search_query_variants(exec_item.get("name", ""), company.get("name", ""))
    query = queries[0] if queries else ""
    all_results: list[dict[str, str]] = []
    errors = []
    try:
        for current_query in queries:
            try:
                results = search_client.search(current_query, limit=5)
            except Exception as exc:
                errors.append(f"{current_query}: {exc}")
                continue
            all_results.extend({**result, "query": current_query} for result in results)
            if results:
                break
    except Exception as exc:  # defensive only; per-query errors are handled above
        errors.append(str(exc))
    if not all_results and errors:
        return {
            "query": query,
            "queries": queries,
            "results": [],
            "selected": None,
            "error": "; ".join(errors),
        }
    scored = []
    for result in all_results:
        item = dict(result)
        item["score"] = score_search_candidate(exec_item, company, result)
        scored.append(item)
    scored.sort(key=lambda item: item["score"], reverse=True)
    selected = scored[0] if scored and scored[0]["score"] >= 0.45 else None
    return {
        "query": query,
        "queries": queries,
        "results": scored,
        "selected": selected,
        "error": None,
    }


def reconcile_exec(
    search_client: SearchClient,
    group: dict[str, Any],
    exec_item: dict[str, Any],
) -> dict[str, Any]:
    company = group["company"]
    employees = group.get("employees_from_sheet", [])
    matched = match_employee(exec_item, employees)
    finalized = {
        "name": exec_item.get("name", ""),
        "role": exec_item.get("role", ""),
        "email": "",
        "linkedin": "",
        "source": exec_item.get("source", "web_search"),
        "matched_sheet_employee": False,
        "match_notes": [],
        "confidence": 0.45,
        "search": None,
    }

    if matched:
        finalized["matched_sheet_employee"] = True
        finalized["email"] = matched.get("email", "")
        if matched.get("role") and not finalized["role"]:
            finalized["role"] = matched.get("role", "")
        url_score = linkedin_url_name_score(finalized["name"], matched.get("linkedin", ""))
        if matched.get("linkedin") and url_score >= 0.34:
            finalized["linkedin"] = matched.get("linkedin", "")
            finalized["confidence"] = max(finalized["confidence"], 0.82 + min(url_score, 1.0) * 0.1)
            finalized["match_notes"].append("Used reconciled LinkedIn from source employee row.")
        elif matched.get("linkedin"):
            finalized["match_notes"].append(
                "Source employee LinkedIn existed but did not match enough name parts."
            )
        else:
            finalized["match_notes"].append("Matched source employee row but LinkedIn was blank.")

    if not finalized["linkedin"]:
        search = search_linkedin_for_exec(search_client, exec_item, company)
        finalized["search"] = search
        selected = search.get("selected")
        if selected:
            finalized["linkedin"] = normalize_url(selected.get("url", ""))
            finalized["confidence"] = max(finalized["confidence"], selected.get("score", 0.0))
            finalized["match_notes"].append("Selected LinkedIn from top search results.")
        elif search.get("error"):
            finalized["match_notes"].append(f"LinkedIn search failed: {search['error']}")
        else:
            finalized["match_notes"].append("No high-confidence LinkedIn result found.")

    finalized["confidence"] = round(float(finalized["confidence"]), 3)
    return finalized


def choose_people(finalized_execs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with_urls = [item for item in finalized_execs if item.get("linkedin")]
    without_urls = [item for item in finalized_execs if not item.get("linkedin")]
    ordered = sorted(with_urls, key=lambda item: item.get("confidence", 0), reverse=True)
    ordered.extend(sorted(without_urls, key=lambda item: item.get("confidence", 0), reverse=True))
    return ordered[:3]


def build_destination_row(group: dict[str, Any]) -> dict[str, Any]:
    selected = choose_people(group.get("finalized_execs", []))
    row = {
        "ID": group.get("id", ""),
        "Company": group.get("company", {}).get("name", ""),
        "Website": group.get("company", {}).get("website", ""),
        "Company LinkedIn": group.get("company", {}).get("linkedin", ""),
        "Emp Count": group.get("company", {}).get("employee_count")
        or len(group.get("employees_from_sheet", [])),
        "Source Tab": group.get("company", {}).get("class") or group.get("source_tab", ""),
        "Primary Lane": group.get("primary_lane", ""),
        "Use": group.get("review_use", ""),
    }
    for idx, person in enumerate(selected, start=1):
        row[f"P{idx} Name"] = person.get("name", "")
        row[f"P{idx} Title"] = person.get("role", "")
        row[f"P{idx} LinkedIn"] = person.get("linkedin", "")
        row[f"P{idx} Email"] = person.get("email", "")
    return row


def build_destination_row_from_computation(lead: dict[str, Any]) -> dict[str, Any]:
    stored_executives = lead.get("executives", [])
    executives = (
        stored_executives[:3]
        if any(
            executive.get("research_source") == "manual_dashboard"
            for executive in stored_executives
        )
        else select_computation_people(stored_executives)
    )
    row = {
        "ID": lead.get("lead_id", ""),
        "Company": lead.get("company", ""),
        "Website": lead.get("website", ""),
        "Company LinkedIn": lead.get("company_linkedin", ""),
        "Emp Count": lead.get("emp_count", ""),
        "Source Tab": lead.get("source_tab", ""),
        "Primary Lane": lead.get("primary_lane", ""),
        "Use": lead.get("use", ""),
    }
    for idx, executive in enumerate(executives, start=1):
        row[f"P{idx} Name"] = executive.get("name", "")
        row[f"P{idx} Title"] = executive.get("title", "")
        row[f"P{idx} LinkedIn"] = executive.get("linkedin_url", "")
        row[f"P{idx} Email"] = executive.get("email", "")
    return row


def select_computation_people(executives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(executives))
    indexed.sort(
        key=lambda item: (
            0 if is_linkedin_profile_url(item[1].get("linkedin_url", "")) else 1,
            -seniority_score(item[1]),
            item[0],
        )
    )
    return [item for _, item in indexed[:3]]


def prefinal_row_readiness(row: dict[str, Any], require_p2: bool = False) -> tuple[bool, list[str]]:
    reasons = []
    for column in ("ID", "Company", "Website"):
        if not clean_text(row.get(column)):
            reasons.append(f"missing_{normalize_key(column).replace(' ', '_')}")
    if not clean_text(row.get("P1 Name")):
        reasons.append("missing_p1_name")
    if not clean_text(row.get("P1 LinkedIn")):
        reasons.append("missing_p1_linkedin")
    elif not is_linkedin_profile_url(row.get("P1 LinkedIn", "")):
        reasons.append("invalid_p1_linkedin")
    if require_p2:
        if not clean_text(row.get("P2 Name")):
            reasons.append("missing_p2_name")
        if not clean_text(row.get("P2 LinkedIn")):
            reasons.append("missing_p2_linkedin")
        elif not is_linkedin_profile_url(row.get("P2 LinkedIn", "")):
            reasons.append("invalid_p2_linkedin")
    return not reasons, reasons


def filter_ready_prefinal_rows(
    rows: list[dict[str, Any]],
    require_p2: bool = False,
    include_unresolved: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ready = []
    skipped = []
    for row in rows:
        ok, reasons = prefinal_row_readiness(row, require_p2=require_p2)
        if ok or include_unresolved:
            ready.append(row)
        else:
            skipped.append(
                {
                    "lead_id": clean_text(row.get("ID")),
                    "company": clean_text(row.get("Company")),
                    "reasons": reasons,
                }
            )
    return ready, skipped


def computation_rows_for_write(
    computation: dict[str, Any], args: argparse.Namespace
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    leads = computation.get("leads", [])
    if args.write_limit:
        leads = leads[: args.write_limit]
    rows = [build_destination_row_from_computation(lead) for lead in leads]
    return filter_ready_prefinal_rows(
        rows,
        require_p2=args.require_p2,
        include_unresolved=args.include_unresolved,
    )


def archive_computation_state(
    computation: dict[str, Any],
    computation_file: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    archive = ResearchArchive(archive_index_path(args))
    result = archive.archive_computation(
        computation,
        computation_file=str(computation_file),
        reason=args.archive_reason,
    )
    computation.setdefault("archive_writes", []).append(
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "archive_index": str(archive_index_path(args)),
            "reason": args.archive_reason,
            **result,
        }
    )
    computation["status"] = "archived"
    save_run(computation, computation_file)
    return result


def archive_unreviewed_computation(
    computation: dict[str, Any],
    computation_file: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    archive = ResearchArchive(archive_index_path(args))
    uncovered = []
    for lead in computation.get("leads", []):
        if has_reusable_research(lead):
            continue
        entry_id = clean_text(lead.get("archive_entry_id"))
        if entry_id and archive.get(entry_id):
            continue
        uncovered.append(
            {
                "lead_id": clean_text(lead.get("lead_id")),
                "company": clean_text(lead.get("company")),
                "status": clean_text(lead.get("status")),
            }
        )
    if uncovered:
        raise ValueError(
            f"Refusing to remove the unreviewed group because {len(uncovered)} leads "
            "do not yet have reusable research."
        )
    result = archive_computation_state(computation, computation_file, args)
    run_file_value = clean_text(computation.get("source_run_file"))
    if not run_file_value:
        raise ValueError("Computation is missing source_run_file; cannot remove its review group.")
    run_file = Path(run_file_value)
    run = load_run(run_file)
    removal = remove_review_group_for_run(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
        run,
    )
    if not removal.get("removed"):
        raise ValueError(f"Archive was saved but review group removal was blocked: {removal}")
    run["status"] = "archived_unreviewed"
    run["archive_result"] = result
    run["review_group_removal"] = removal
    save_run(run, run_file)
    computation["status"] = "archived_unreviewed"
    computation["review_group_removal"] = removal
    save_run(computation, computation_file)
    return {**result, "review_group_removal": removal}


def consume_reused_archive_entries(
    computation: dict[str, Any],
    rows: list[dict[str, Any]],
    computation_file: Path,
    args: argparse.Namespace,
) -> int:
    written_ids = {clean_text(row.get("ID")) for row in rows if clean_text(row.get("ID"))}
    entry_ids = [
        clean_text(lead.get("archive_entry_id"))
        for lead in computation.get("leads", [])
        if clean_text(lead.get("lead_id")) in written_ids
        and clean_text(lead.get("archive_entry_id"))
    ]
    if not entry_ids:
        return 0
    archive = ResearchArchive(archive_index_path(args))
    consumed = archive.mark_consumed(
        entry_ids,
        computation_file=str(computation_file),
        destination=args.destination_tab,
    )
    computation.setdefault("post_review_reconciliation", {})["archive_entries_consumed"] = consumed
    save_run(computation, computation_file)
    return consumed


def verify_destination_rows(
    credentials_path: Path,
    sheet_url: str,
    destination_tab: str,
    rows: list[dict[str, Any]],
    start_row: int,
) -> bool:
    if not rows:
        return True
    client = get_client(str(credentials_path))
    worksheet = get_worksheet(open_sheet(client, sheet_url), destination_tab)
    headers = worksheet.row_values(1)
    end_row = start_row + len(rows) - 1
    values = worksheet.get(f"A{start_row}:{gspread.utils.rowcol_to_a1(end_row, len(headers))}")
    observed = []
    for value_row in values:
        padded = value_row + [""] * (len(headers) - len(value_row))
        observed.append({header: padded[index] for index, header in enumerate(headers)})
    return len(observed) == len(rows) and rows_fingerprint(observed) == rows_fingerprint(rows)


def write_computation_rows(
    computation: dict[str, Any],
    args: argparse.Namespace,
    computation_file: Path | None = None,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if computation.get("publication_mode") == "archive_only":
        raise ValueError(
            "This computation came from an all-leads/unreviewed selection and is archive-only. "
            "Run archive-computation instead of publishing it to Pre-final."
        )
    rows, skipped = computation_rows_for_write(computation, args)
    queue_batch: dict[str, Any] = {}
    if args.destination_tab != DEFAULT_DESTINATION_TAB or not rows:
        rows_written = write_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            start_row=args.write_start_row,
        )
        return rows_written, rows, skipped, queue_batch

    # This is the durable boundary: persist the exact validated rows before the
    # Pre-final overwrite, so later activity work never depends on the live tab.
    queue_batch = enqueue_batch(rows, str(computation_file or ""))
    if queue_batch.get("status") == "prospects_bridged":
        raise ValueError(
            f"Queue batch {queue_batch['fingerprint']} is already completed; refusing to republish it."
        )
    try:
        rows_written = write_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            start_row=args.write_start_row,
        )
        verified = verify_destination_rows(
            Path(args.credentials),
            destination_sheet_url(args),
            args.destination_tab,
            rows,
            args.write_start_row,
        )
        if not verified:
            record_prefinal_publish(
                queue_batch["fingerprint"],
                start_row=args.write_start_row,
                verified=False,
                error="readback_fingerprint_mismatch",
            )
            raise RuntimeError("Pre-final write readback did not match the queued batch")
        queue_batch = record_prefinal_publish(
            queue_batch["fingerprint"], start_row=args.write_start_row, verified=True
        )
    except Exception as exc:
        record_prefinal_publish(
            queue_batch["fingerprint"],
            start_row=args.write_start_row,
            verified=False,
            error=str(exc),
        )
        raise
    return rows_written, rows, skipped, queue_batch


def write_destination_rows(
    credentials_path: Path,
    sheet_url: str,
    destination_tab: str,
    rows: list[dict[str, Any]],
    start_row: int = 2,
) -> int:
    if not rows:
        return 0
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, destination_tab)
    headers = worksheet.row_values(1)
    require_columns(headers, DESTINATION_COLUMNS, destination_tab)
    missing_optional = [column for column in OPTIONAL_DESTINATION_COLUMNS if column not in headers]
    if missing_optional:
        raise ValueError(
            f"{destination_tab} is missing required manually-created columns for this workflow: "
            f"{', '.join(missing_optional)}"
        )
    payload = [[row.get(header, "") for header in headers] for row in rows]
    start_cell = gspread.utils.rowcol_to_a1(start_row, 1)
    end_cell = gspread.utils.rowcol_to_a1(start_row + len(payload) - 1, len(headers))
    worksheet.update(
        range_name=f"{start_cell}:{end_cell}", values=payload, value_input_option="USER_ENTERED"
    )
    return len(payload)


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


def get_or_create_worksheet(spreadsheet, tab_name: str, rows: int = 1000, cols: int = 26):
    try:
        return spreadsheet.worksheet(tab_name)
    except WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)


def ensure_review_tab(credentials_path: Path, sheet_url: str, review_tab: str):
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    headers = worksheet.row_values(1)
    require_columns(headers, REVIEW_COLUMNS, review_tab)
    if "Research Source" in headers and "Overlap Status" not in headers:
        column_index = headers.index("Research Source") + 1
        worksheet.update_cell(1, column_index, "Overlap Status")
        headers[column_index - 1] = "Overlap Status"
    missing_overlap_columns = [column for column in REVIEW_OVERLAP_COLUMNS if column not in headers]
    if missing_overlap_columns:
        required_column_count = len(headers) + len(missing_overlap_columns)
        if worksheet.col_count < required_column_count:
            worksheet.add_cols(required_column_count - worksheet.col_count)
        start_column = len(headers) + 1
        end_column = start_column + len(missing_overlap_columns) - 1
        worksheet.update(
            range_name=(
                f"{gspread.utils.rowcol_to_a1(1, start_column)}:"
                f"{gspread.utils.rowcol_to_a1(1, end_column)}"
            ),
            values=[missing_overlap_columns],
            value_input_option="USER_ENTERED",
        )
        headers.extend(missing_overlap_columns)
    return worksheet


def build_review_row(run: dict[str, Any], run_file: Path, lead: dict[str, Any]) -> dict[str, Any]:
    employees = lead.get("employees_from_sheet", [])
    archive_match = lead.get("archive_match", {})
    overlap_status = {
        MATCH_AVAILABLE: "Archive Match",
        MATCH_CONFLICT: "Possible Match",
        MATCH_CONSUMED: "Already Consumed",
        MATCH_FRESH: "Fresh",
    }.get(archive_match.get("status"), "Fresh")
    return {
        "Run ID": lead.get("id", ""),
        "Primary Lane": lead.get("primary_lane", ""),
        "Company Name": lead.get("company", {}).get("name", ""),
        "Company Website": lead.get("company", {}).get("website", ""),
        "Emp Count": lead.get("company", {}).get("employee_count") or len(employees),
        "Approved": False,
        "Use": "",
        "Prep Wave": lead.get("prep_wave", "Base"),
        "Overlap Status": overlap_status,
        "Archive Entry ID": archive_match.get("archive_entry_id", ""),
    }


def review_group_date_value(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.month}/{now.day}/{now.year}"


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


def last_nonempty_review_row(headers: list[str], values: list[list[Any]]) -> int:
    content_columns = ["Date", "Run ID", "Company Name", "Company Website"]
    indexes = [headers.index(column) for column in content_columns if column in headers]
    last_row = 1
    for row_number, row in enumerate(values, start=1):
        padded = row + [""] * (len(headers) - len(row))
        if any(clean_text(padded[index]) for index in indexes):
            last_row = row_number
    return last_row


def existing_successful_review_group_row(
    headers: list[str],
    values: list[list[Any]],
    target_date,
) -> int | None:
    date_idx = headers.index("Date")
    run_id_idx = headers.index("Run ID")
    group_row: int | None = None
    saw_lead_in_group = False

    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        row_date = parse_review_group_date(padded[date_idx])
        row_run_id = clean_text(padded[run_id_idx])
        if row_date:
            if group_row is not None and saw_lead_in_group:
                return group_row
            group_row = row_number if row_date == target_date else None
            saw_lead_in_group = False
            continue
        if group_row is not None and row_run_id:
            saw_lead_in_group = True

    if group_row is not None and saw_lead_in_group:
        return group_row
    return None


def find_successful_review_group_row_for_today(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
) -> int | None:
    worksheet = ensure_review_tab(credentials_path, sheet_url, review_tab)
    headers = worksheet.row_values(1)
    if "Date" not in headers or "Run ID" not in headers:
        return None
    return existing_successful_review_group_row(
        headers, worksheet.get_all_values(), datetime.now().date()
    )


def write_review_rows(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run: dict[str, Any],
    run_file: Path,
) -> int:
    worksheet = ensure_review_tab(credentials_path, sheet_url, review_tab)
    rows = [build_review_row(run, run_file, lead) for lead in run.get("leads", [])]
    if not rows:
        return 0
    headers = worksheet.row_values(1)
    if "Date" not in headers:
        raise ValueError(f"{review_tab} is missing required Date column for daily group rows.")
    if "Run ID" not in headers:
        raise ValueError(f"{review_tab} is missing required Run ID column for daily group rows.")
    payload = [[row.get(header, "") for header in headers] for row in rows]
    group_date = review_group_date_value()
    values = worksheet.get_all_values()
    existing_group_row = existing_successful_review_group_row(
        headers, values, datetime.now().date()
    )
    if existing_group_row is not None:
        raise ValueError(
            f"{review_tab} already has a successful review queue for {group_date} "
            f"at group row {existing_group_row}. Refusing to prepare a second queue for the same day."
        )
    group_row = last_nonempty_review_row(headers, values) + 1
    start_row = group_row + 1
    end_row = start_row + len(payload) - 1
    if worksheet.row_count < end_row:
        raise ValueError(
            f"{review_tab} has {worksheet.row_count} total rows, but writing the daily group at row "
            f"{group_row} plus {len(payload)} leads needs through row {end_row}. "
            "Add rows manually before running prepare-review."
        )
    worksheet.update(
        range_name=gspread.utils.rowcol_to_a1(group_row, headers.index("Date") + 1),
        values=[[group_date]],
        value_input_option="USER_ENTERED",
    )
    worksheet.update(range_name=f"A{start_row}", values=payload, value_input_option="USER_ENTERED")
    run["review_write"] = {
        "group_row": group_row,
        "start_row": start_row,
        "end_row": end_row,
        "date": group_date,
    }
    return len(payload)


def read_review_rows(
    credentials_path: Path, sheet_url: str, review_tab: str, run_id: str
) -> list[dict[str, Any]]:
    headers, rows = read_worksheet(credentials_path, sheet_url, review_tab)
    require_columns(headers, REVIEW_COLUMNS, review_tab)
    return [row for row in rows if clean_text(row.get("Run ID"))]


def remove_review_group_for_run(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    values = worksheet.get_all_values()
    if not values:
        return {"removed": False, "reason": "review_tab_empty"}
    headers = values[0]
    require_columns(headers, ["Date", "Run ID"], review_tab)
    date_index = headers.index("Date")
    run_id_index = headers.index("Run ID")
    review_complete_index = (
        headers.index("Design Review Complete") if "Design Review Complete" in headers else None
    )
    expected_ids = {
        clean_text(lead.get("id")) for lead in run.get("leads", []) if clean_text(lead.get("id"))
    }
    target_date = parse_review_group_date(
        run.get("review", {}).get("write", {}).get("date")
        or run.get("review_write", {}).get("date")
    )
    groups = []
    current = None
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        parsed_date = parse_review_group_date(padded[date_index])
        if parsed_date:
            if current:
                current["end_row"] = row_number - 1
                groups.append(current)
            current = {
                "group_row": row_number,
                "end_row": row_number,
                "date": parsed_date,
                "review_complete": (
                    checkbox_truthy(padded[review_complete_index])
                    if review_complete_index is not None
                    else False
                ),
                "lead_ids": set(),
            }
            continue
        if current and clean_text(padded[run_id_index]):
            current["lead_ids"].add(clean_text(padded[run_id_index]))
            current["end_row"] = row_number
    if current:
        groups.append(current)

    candidates = [
        group
        for group in groups
        if (target_date is None or group["date"] == target_date)
        and expected_ids
        and expected_ids.issubset(group["lead_ids"])
    ]
    if len(candidates) != 1:
        return {
            "removed": False,
            "reason": "review_group_not_uniquely_resolved",
            "candidate_count": len(candidates),
            "target_date": target_date.isoformat() if target_date else "",
        }
    group = candidates[0]
    if group["review_complete"]:
        raise ValueError("Refusing to remove a review-complete date group as unreviewed.")
    start_index = group["group_row"] - 1
    end_index = group["end_row"]
    spreadsheet.batch_update(
        {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "ROWS",
                            "startIndex": start_index,
                            "endIndex": end_index,
                        }
                    }
                }
            ]
        }
    )
    return {
        "removed": True,
        "group_row": group["group_row"],
        "end_row": group["end_row"],
        "rows_removed": end_index - start_index,
        "date": group["date"].isoformat(),
        "lead_count": len(group["lead_ids"]),
    }


def checkbox_truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "y", "1", "checked"}


def update_review_statuses(
    credentials_path: Path,
    sheet_url: str,
    review_tab: str,
    run_id: str,
    status_by_id: dict[str, str],
) -> None:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_worksheet(spreadsheet, review_tab)
    values = worksheet.get_all_values()
    if not values:
        return
    headers = values[0]
    if "Run ID" not in headers or "ID" not in headers or "Status" not in headers:
        return
    run_idx = headers.index("Run ID")
    id_idx = headers.index("ID")
    status_idx = headers.index("Status") + 1
    updates = []
    for row_number, row in enumerate(values[1:], start=2):
        row += [""] * (len(headers) - len(row))
        if clean_text(row[run_idx]) != run_id:
            continue
        lead_id = clean_text(row[id_idx])
        if lead_id in status_by_id:
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(row_number, status_idx),
                    "values": [[status_by_id[lead_id]]],
                }
            )
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")


def latest_run_file() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    candidates = sorted(
        RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def latest_review_run_file() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    candidates = sorted(
        RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in candidates:
        try:
            run = load_run(path)
        except Exception:
            continue
        if run.get("status") in {"awaiting_review", "approved_for_processing"} and run.get(
            "review", {}
        ).get("rows_written", 0):
            return path
    return None


def latest_computation_file() -> Path | None:
    if not COMPUTATIONS_DIR.exists():
        return None
    candidates = sorted(
        COMPUTATIONS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def lead_id_fingerprint(lead_ids: Sequence[str]) -> str:
    normalized = sorted(clean_text(lead_id) for lead_id in lead_ids if clean_text(lead_id))
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()[:16]


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


def computation_fingerprint(computation: dict[str, Any]) -> str:
    if clean_text(computation.get("approved_fingerprint")):
        return clean_text(computation.get("approved_fingerprint"))
    return lead_id_fingerprint([lead.get("lead_id", "") for lead in computation.get("leads", [])])


def latest_computation_file_for_fingerprint(fingerprint: str) -> Path | None:
    if not fingerprint or not COMPUTATIONS_DIR.exists():
        return None
    candidates = sorted(
        COMPUTATIONS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in candidates:
        try:
            computation = load_run(path)
        except Exception:
            continue
        if computation_fingerprint(computation) == fingerprint:
            return path
    return None


def latest_search_result_file() -> Path | None:
    if not SEARCH_RESULTS_DIR.exists():
        return None
    candidates = sorted(
        SEARCH_RESULTS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def approved_gate_status(args: argparse.Namespace) -> dict[str, Any]:
    import automation_gate  # Local script import keeps gate logic in one place.

    gate_args = argparse.Namespace(
        sheet_url=source_sheet_url(args),
        review_tab=args.review_tab,
        credentials=args.credentials,
        threshold=args.approval_threshold,
        all_leads=resolve_ignore_review_approval(args),
        lane_scope=args.review_lane_scope,
        review_slice=args.review_slice,
    )
    return automation_gate.status_payload(gate_args)


def claim_approved_gate(args: argparse.Namespace) -> dict[str, Any]:
    import automation_gate

    return automation_gate.claim(
        argparse.Namespace(
            sheet_url=source_sheet_url(args),
            review_tab=args.review_tab,
            credentials=args.credentials,
            threshold=args.approval_threshold,
            all_leads=resolve_ignore_review_approval(args),
            lane_scope=args.review_lane_scope,
            review_slice=args.review_slice,
        )
    )


def mark_approved_gate(args: argparse.Namespace, fingerprint: str, status: str) -> dict[str, Any]:
    import automation_gate

    return automation_gate.mark(
        argparse.Namespace(
            fingerprint=fingerprint,
            note=f"lead_exec_research {status}",
        ),
        status,
    )


def computation_status_counts(
    computation: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    leads = computation.get("leads", [])
    rows, skipped = computation_rows_for_write(computation, args)
    search_tasks = computation.get("search_tasks", [])
    pending_tasks = [task for task in search_tasks if task.get("status", "pending") == "pending"]
    selected_tasks = [task for task in search_tasks if task.get("status") == "selected"]
    latest_batch = (
        computation.get("search_result_batches", [{}])[-1]
        if computation.get("search_result_batches")
        else {}
    )
    latest_write = computation.get("writes", [{}])[-1] if computation.get("writes") else {}
    p1_linkedin = 0
    p2_linkedin = 0
    for lead in leads:
        row = build_destination_row_from_computation(lead)
        if is_linkedin_profile_url(row.get("P1 LinkedIn", "")):
            p1_linkedin += 1
        if is_linkedin_profile_url(row.get("P2 LinkedIn", "")):
            p2_linkedin += 1
    return {
        "lead_count": len(leads),
        "research_done": len([lead for lead in leads if lead.get("executives")]),
        "research_pending": len(
            [
                lead
                for lead in leads
                if lead.get("status") == "research_pending" and not lead.get("executives")
            ]
        ),
        "reconciliation_pending": len(
            [
                lead
                for lead in leads
                if lead.get("status") == "reconciliation_pending" and lead.get("executives")
            ]
        ),
        "archive_conflicts": len(
            [lead for lead in leads if lead.get("status") == "archive_conflict"]
        ),
        "archive_consumed": len(
            [lead for lead in leads if lead.get("status") == "archive_consumed"]
        ),
        "search_tasks_total": len(search_tasks),
        "search_tasks_pending": len(pending_tasks),
        "search_tasks_selected": len(selected_tasks),
        "search_tasks_remaining": computation.get("search_tasks_remaining", len(pending_tasks)),
        "p1_linkedin": p1_linkedin,
        "p2_linkedin": p2_linkedin,
        "rows_ready_to_write": len(rows),
        "rows_skipped_unresolved": len(skipped),
        "skipped_unresolved": skipped,
        "latest_search_batch": {
            "source_file": latest_batch.get("source_file", ""),
            "status": latest_batch.get("status", ""),
            "task_count": latest_batch.get("task_count", 0),
            "selected": len(
                [row for row in latest_batch.get("results", []) if row.get("status") == "selected"]
            ),
            "needs_review": len(
                [
                    row
                    for row in latest_batch.get("results", [])
                    if row.get("status") == "needs_review"
                ]
            ),
            "errors": len(
                [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
            ),
            "pending_tasks": len(latest_batch.get("pending_tasks", [])),
            "captcha_events": latest_batch.get("captcha_events", 0),
            "hot_profiles": len(latest_batch.get("hot_profiles", [])),
        },
        "latest_write": {
            "created_at": latest_write.get("created_at", ""),
            "rows_written": latest_write.get("rows_written", 0),
            "skipped_unresolved_count": latest_write.get("skipped_unresolved_count", 0),
        },
    }


def next_approved_action(
    gate: dict[str, Any], computation: dict[str, Any] | None, counts: dict[str, Any]
) -> str:
    claim_status = gate.get("claim_status", "")
    if claim_status == "processed":
        return "skip_processed"
    if not gate.get("ready"):
        return "skip_below_threshold"
    if computation is None:
        return "claim_and_freeze" if not claim_status else "freeze_approved"
    if counts.get("research_pending", 0):
        return "research_batch"
    if counts.get("reconciliation_pending", 0) or (
        computation.get("status") == "reconciliation_pending"
        and not computation.get("search_tasks")
    ):
        return "reconcile_existing"
    if counts.get("search_tasks_pending", 0) and counts.get("rows_skipped_unresolved", 0):
        return "run_playwright_search"
    if counts.get("archive_conflicts", 0):
        return "resolve_archive_conflicts"
    if computation.get("publication_mode") == "archive_only":
        if computation.get("status") == "archived_unreviewed":
            return "skip_archived_unreviewed"
        return "archive_unreviewed"
    if not computation.get("writes"):
        return "write_computation"
    if claim_status != "processed":
        return "mark_processed"
    return "skip_processed"


def approved_workflow_status(args: argparse.Namespace) -> dict[str, Any]:
    gate = approved_gate_status(args)
    computation_file = (
        Path(args.computation_file)
        if args.computation_file
        else latest_computation_file_for_fingerprint(gate.get("fingerprint", ""))
    )
    computation: dict[str, Any] | None = None
    counts: dict[str, Any] = {}
    if computation_file and computation_file.exists():
        computation = load_run(computation_file)
        counts = computation_status_counts(computation, args)
    action = next_approved_action(gate, computation, counts)
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "gate": gate,
        "computation_file": str(computation_file) if computation_file else "",
        "computation_status": computation.get("status", "") if computation else "",
        "counts": counts,
        "next_action": action,
        "resumable": action
        in {
            "claim_and_freeze",
            "freeze_approved",
            "research_batch",
            "reconcile_existing",
            "run_playwright_search",
            "archive_unreviewed",
            "write_computation",
            "mark_processed",
        },
    }


def export_pending_search_tasks(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> tuple[Path, list[dict[str, Any]]]:
    tasks = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    tasks = tasks[: args.max_search_tasks]
    export_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_file = SEARCH_TASKS_DIR / f"{export_id}_search_tasks.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "computation_file": str(computation_file),
        "task_count": len(tasks),
        "tasks": tasks,
        "search_tasks": tasks,
    }
    task_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    computation.setdefault("search_task_exports", []).append(
        {
            "created_at": payload["created_at"],
            "file": str(task_file),
            "task_count": len(tasks),
        }
    )
    save_run(computation, computation_file)
    return task_file, tasks


def send_review_email(
    run: dict[str, Any], run_file: Path, review_tab: str, to_email: str
) -> dict[str, Any]:
    if not to_email:
        return {"sent": False, "reason": "No notification email configured."}
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM", user)
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not host or not user or not password or not from_email:
        return {
            "sent": False,
            "reason": "SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM/SMTP_USER are required.",
        }

    subject = f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"
    body = "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
    return {"sent": True, "to": to_email}


def review_email_subject(run: dict[str, Any]) -> str:
    return f"Lead review ready: {run.get('source', {}).get('selected_count', 0)} leads"


def review_email_body(run: dict[str, Any], run_file: Path, review_tab: str) -> str:
    return "\n".join(
        [
            "A new lead review queue is ready.",
            "",
            f"Run ID: {run.get('run_id', '')}",
            f"Review tab: {review_tab}",
            f"Selected leads: {run.get('source', {}).get('selected_count', 0)}",
            f"Run file: {run_file}",
            "",
            "Open the sheet, review the rows, tick Approved for leads to process, then start the approved-leads processing workflow.",
        ]
    )


def enqueue_review_notification(
    credentials_path: Path,
    sheet_url: str,
    queue_tab: str,
    run: dict[str, Any],
    run_file: Path,
    review_tab: str,
    to_email: str,
) -> dict[str, Any]:
    client = get_client(str(credentials_path))
    spreadsheet = open_sheet(client, sheet_url)
    worksheet = get_or_create_worksheet(
        spreadsheet, queue_tab, rows=1000, cols=len(NOTIFICATION_QUEUE_COLUMNS)
    )
    headers = worksheet.row_values(1)
    if headers[: len(NOTIFICATION_QUEUE_COLUMNS)] != NOTIFICATION_QUEUE_COLUMNS:
        worksheet.update(
            range_name="A1",
            values=[NOTIFICATION_QUEUE_COLUMNS],
            value_input_option="USER_ENTERED",
        )
        try:
            worksheet.freeze(rows=1)
        except Exception:
            pass
    row = {
        "Created At": datetime.now().isoformat(timespec="seconds"),
        "Status": "Pending",
        "To": to_email,
        "Subject": review_email_subject(run),
        "Body": review_email_body(run, run_file, review_tab),
        "Run ID": run.get("run_id", ""),
        "Run File": str(run_file),
        "Sent At": "",
        "Error": "",
    }
    worksheet.append_row(
        [row.get(header, "") for header in NOTIFICATION_QUEUE_COLUMNS],
        value_input_option="USER_ENTERED",
    )
    return {"queued": True, "tab": queue_tab, "to": to_email}


def notify_review_with_fallback(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    try:
        notification = send_review_email(run, run_file, args.review_tab, args.notify_email)
    except Exception as exc:
        notification = {"sent": False, "reason": str(exc)}
    if not notification.get("sent") and args.queue_notification:
        try:
            queued = enqueue_review_notification(
                Path(args.credentials),
                source_sheet_url(args),
                args.notification_queue_tab,
                run,
                run_file,
                args.review_tab,
                args.notify_email,
            )
            notification["fallback_queue"] = queued
        except Exception as exc:
            notification["fallback_queue"] = {"queued": False, "reason": str(exc)}
    return notification


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    source_headers, source_rows = read_worksheet(
        Path(args.credentials), source_sheet_url(args), args.source_tab
    )
    source_rows = canonicalize_source_rows(source_headers, source_rows, args.source_tab)
    destination_headers, _destination_rows = read_worksheet(
        Path(args.credentials), destination_sheet_url(args), args.destination_tab
    )
    require_columns(destination_headers, DESTINATION_COLUMNS, args.destination_tab)
    groups = group_source_rows(source_rows, args.source_tab)
    return {
        "source_tab": args.source_tab,
        "destination_tab": args.destination_tab,
        "review_tab": args.review_tab,
        "source_headers": source_headers,
        "destination_headers": destination_headers,
        "source_groups": len(groups),
        "first_group": groups[0] if groups else None,
    }


def save_run(run: dict[str, Any], path: Path) -> None:
    ensure_dirs()
    path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")


def load_run(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def source_sheet_url(args: argparse.Namespace) -> str:
    return args.source_sheet_url or args.sheet_url


def destination_sheet_url(args: argparse.Namespace) -> str:
    return args.destination_sheet_url or args.sheet_url


def archive_index_path(args: argparse.Namespace) -> Path:
    configured = clean_text(getattr(args, "archive_index", ""))
    return Path(configured) if configured else DEFAULT_RESEARCH_ARCHIVE_INDEX


def annotate_overlap_scan(
    lead: dict[str, Any],
    archive: ResearchArchive,
    *,
    overlap_scan_enabled: bool,
) -> dict[str, Any]:
    match = (
        archive.match(lead)
        if overlap_scan_enabled
        else {
            "status": MATCH_FRESH,
            "archive_entry_id": "",
            "matched_on": [],
            "confidence": "disabled",
        }
    )
    lead["archive_match"] = match
    lead["overlap_status"] = match.get("status", MATCH_FRESH)
    return match


def collect_overlap_top_up_waves(
    initial_shortfall: int,
    collect_wave: Callable[[str, int], list[dict[str, Any]]],
    *,
    enabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    waves: list[dict[str, Any]] = []
    shortfall = max(0, int(initial_shortfall))
    wave_number = 1
    if not enabled:
        return selected, waves, shortfall

    while shortfall > 0:
        label = f"Top-up {wave_number}"
        requested = shortfall
        wave = collect_wave(label, requested)
        if not wave:
            break
        selected.extend(wave)
        archive_matches = [
            lead for lead in wave if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
        ]
        conflicts = [
            lead for lead in wave if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT
        ]
        waves.append(
            {
                "label": label,
                "requested": requested,
                "selected": len(wave),
                "archive_matches": len(archive_matches),
                "conflicts": len(conflicts),
            }
        )
        shortfall = len(archive_matches) + len(conflicts)
        wave_number += 1
        if len(wave) < requested:
            break

    return selected, waves, shortfall


def build_run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    headers, rows = read_worksheet(Path(args.credentials), source_sheet_url(args), args.source_tab)
    rows = canonicalize_source_rows(headers, rows, args.source_tab)
    destination_company_by_id = load_destination_company_by_id(
        Path(args.credentials), destination_sheet_url(args), args.destination_tab
    )
    reviewed_ids = load_reviewed_ids(
        Path(args.credentials), source_sheet_url(args), args.review_tab
    )
    groups = group_source_rows(rows, args.source_tab)
    archive = ResearchArchive(archive_index_path(args))
    archive_stats = archive.stats()
    overlap_scan_enabled = (
        not getattr(args, "disable_overlap_scan", False) and archive_stats.get("available", 0) > 0
    )
    overlap_top_ups_enabled = overlap_scan_enabled and not getattr(
        args, "disable_overlap_top_ups", False
    )
    selected: list[dict[str, Any]] = []
    prep_waves: list[dict[str, Any]] = []
    destination_conflicts = []
    skipped_destination_ids = set()
    skipped_reviewed_ids = set()
    skipped_consumed_archive_ids = set()
    group_index = 0

    def next_eligible_group() -> dict[str, Any] | None:
        nonlocal group_index
        while group_index < len(groups):
            group = groups[group_index]
            group_index += 1
            lead_id = group.get("id", "")
            if lead_id in reviewed_ids:
                skipped_reviewed_ids.add(lead_id)
                continue
            source_company = group.get("company", {}).get("name", "")
            destination_company = destination_company_by_id.get(lead_id)
            if destination_company:
                if normalize_key(destination_company) == normalize_key(source_company):
                    skipped_destination_ids.add(lead_id)
                    continue
                destination_conflicts.append(
                    {
                        "id": lead_id,
                        "source_company": source_company,
                        "destination_company": destination_company,
                    }
                )
            match = annotate_overlap_scan(
                group,
                archive,
                overlap_scan_enabled=overlap_scan_enabled,
            )
            if match.get("status") == MATCH_CONSUMED:
                skipped_consumed_archive_ids.add(lead_id)
                continue
            return group
        return None

    def collect_wave(label: str, target: int) -> list[dict[str, Any]]:
        wave = []
        while len(wave) < target:
            group = next_eligible_group()
            if group is None:
                break
            group["prep_wave"] = label
            wave.append(group)
        return wave

    base_wave = collect_wave("Base", args.limit)
    selected.extend(base_wave)
    base_nonfresh = [
        lead
        for lead in base_wave
        if lead.get("archive_match", {}).get("status") in {MATCH_AVAILABLE, MATCH_CONFLICT}
    ]
    prep_waves.append(
        {
            "label": "Base",
            "requested": args.limit,
            "selected": len(base_wave),
            "archive_matches": len(
                [
                    lead
                    for lead in base_wave
                    if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
                ]
            ),
            "conflicts": len(
                [
                    lead
                    for lead in base_wave
                    if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT
                ]
            ),
        }
    )

    top_up_rows, top_up_waves, unresolved_shortfall = collect_overlap_top_up_waves(
        len(base_nonfresh),
        collect_wave,
        enabled=overlap_top_ups_enabled,
    )
    selected.extend(top_up_rows)
    prep_waves.extend(top_up_waves)

    fresh_count = len(
        [lead for lead in selected if lead.get("archive_match", {}).get("status") == MATCH_FRESH]
    )
    archive_match_count = len(
        [
            lead
            for lead in selected
            if lead.get("archive_match", {}).get("status") == MATCH_AVAILABLE
        ]
    )
    archive_conflict_count = len(
        [lead for lead in selected if lead.get("archive_match", {}).get("status") == MATCH_CONFLICT]
    )
    lane_counts = assign_primary_lanes(selected)
    run_id = now_run_id()
    run_file = RUNS_DIR / f"{run_id}.json"
    run = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pulled",
        "config": {
            "source_sheet_url": source_sheet_url(args),
            "destination_sheet_url": destination_sheet_url(args),
            "source_tab": args.source_tab,
            "destination_tab": args.destination_tab,
            "limit": args.limit,
            "batch_size": args.batch_size,
            "dry_run": args.dry_run,
            "archive_index": str(archive_index_path(args)),
            "overlap_scan_enabled": overlap_scan_enabled,
            "overlap_top_ups_enabled": overlap_top_ups_enabled,
        },
        "source": {
            "total_groups": len(groups),
            "reviewed_ids_seen": len(reviewed_ids),
            "reviewed_ids_skipped": len(skipped_reviewed_ids),
            "destination_ids_seen": len(destination_company_by_id),
            "destination_ids_skipped": len(skipped_destination_ids),
            "destination_id_conflicts": destination_conflicts,
            "consumed_archive_ids_skipped": len(skipped_consumed_archive_ids),
            "base_selected_count": len(base_wave),
            "selected_count": len(selected),
            "lane_counts": lane_counts,
        },
        "overlap_scan": {
            "phase": "pre_review_detection",
            "detection_only": True,
            "archive_reuse_applied": False,
            "archive_entries_consumed": 0,
            "enabled": overlap_scan_enabled,
            "automatic": not getattr(args, "disable_overlap_scan", False),
            "top_ups_enabled": overlap_top_ups_enabled,
            "top_ups_automatic": not getattr(args, "disable_overlap_top_ups", False),
            "archive": archive_stats,
            "fresh_target": args.limit,
            "fresh_count": fresh_count,
            "archive_match_count": archive_match_count,
            "conflict_count": archive_conflict_count,
            "top_up_count": max(0, len(selected) - len(base_wave)),
            "top_up_suppressed_count": (
                len(base_nonfresh) if overlap_scan_enabled and not overlap_top_ups_enabled else 0
            ),
            "unresolved_fresh_shortfall": unresolved_shortfall,
            "target_met": fresh_count >= args.limit,
            "settled_for_day": fresh_count >= args.limit or not overlap_top_ups_enabled,
            "source_exhausted": group_index >= len(groups) and fresh_count < args.limit,
            "waves": prep_waves,
        },
        "batches": [
            {"index": index + 1, "lead_ids": [lead["id"] for lead in batch], "status": "pending"}
            for index, batch in enumerate(chunks(selected, args.batch_size))
        ],
        "leads": selected,
        "destination_rows": [],
        "errors": [],
    }
    save_run(run, run_file)
    return run, run_file


def prepare_review(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    existing_group_row = find_successful_review_group_row_for_today(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
    )
    if existing_group_row is not None:
        run_id = now_run_id()
        run_file = RUNS_DIR / f"{run_id}.json"
        run = {
            "run_id": run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "review_skipped_already_prepared_today",
            "config": {
                "source_sheet_url": source_sheet_url(args),
                "destination_sheet_url": destination_sheet_url(args),
                "source_tab": args.source_tab,
                "destination_tab": args.destination_tab,
                "limit": args.limit,
                "batch_size": args.batch_size,
                "dry_run": args.dry_run,
            },
            "source": {"selected_count": 0},
            "batches": [],
            "leads": [],
            "destination_rows": [],
            "errors": [],
            "review": {
                "tab": args.review_tab,
                "rows_written": 0,
                "write": {
                    "date": review_group_date_value(),
                    "existing_group_row": existing_group_row,
                    "skipped": True,
                },
                "notification": {
                    "sent": False,
                    "reason": f"Review queue already prepared today at group row {existing_group_row}.",
                },
            },
        }
        save_run(run, run_file)
        return run, run_file

    run, run_file = build_run(args)
    if args.dry_run:
        run["status"] = "review_dry_run"
        run["review"] = {
            "tab": args.review_tab,
            "rows_written": 0,
            "notification": {"sent": False, "reason": "Dry run."},
        }
        save_run(run, run_file)
        return run, run_file

    rows_written = write_review_rows(
        Path(args.credentials),
        source_sheet_url(args),
        args.review_tab,
        run,
        run_file,
    )
    notification = {"sent": False, "reason": "Email notification disabled."}
    if not args.no_email:
        notification = notify_review_with_fallback(run, run_file, args)

    run["status"] = "awaiting_review"
    run["review"] = {
        "tab": args.review_tab,
        "rows_written": rows_written,
        "write": run.get("review_write", {}),
        "notification": notification,
    }
    save_run(run, run_file)
    return run, run_file


def notify_review(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> dict[str, Any]:
    notification = notify_review_with_fallback(run, run_file, args)
    run.setdefault("review", {})
    run["review"]["tab"] = run["review"].get("tab") or args.review_tab
    run["review"]["notification"] = notification
    save_run(run, run_file)
    return run


def filter_run_to_approved(run: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    rows = read_review_rows(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run.get("run_id", "")
    )
    approved_ids = {
        clean_text(row.get("Run ID"))
        for row in rows
        if (ignore_review_approval or checkbox_truthy(row.get("Approved")))
        and clean_text(row.get("Run ID"))
    }
    use_by_id = {
        clean_text(row.get("Run ID")): clean_text(row.get("Use"))
        for row in rows
        if clean_text(row.get("Run ID"))
    }
    lead_by_id = {lead.get("id"): lead for lead in run.get("leads", [])}
    approved_leads = [lead_by_id[lead_id] for lead_id in approved_ids if lead_id in lead_by_id]
    approved_leads.sort(key=lambda lead: lead.get("source_rows", {}).get("company_row") or 0)
    if not approved_leads:
        selected_label = "leads" if ignore_review_approval else "approved leads"
        raise ValueError(
            f"No {selected_label} found in {args.review_tab} for run {run.get('run_id', '')}."
        )

    for lead in approved_leads:
        lead["review_use"] = use_by_id.get(lead.get("id", ""), "")
        if lead.get("status") != "completed":
            lead["status"] = "pending"

    run["leads"] = approved_leads
    run["batches"] = [
        {"index": index + 1, "lead_ids": [lead["id"] for lead in batch], "status": "pending"}
        for index, batch in enumerate(chunks(approved_leads, args.batch_size))
    ]
    run["source"]["approved_count"] = len(approved_leads)
    run["source"]["selection_mode"] = (
        "approval_disabled" if ignore_review_approval else "approved_only"
    )
    run["status"] = "approved_for_processing"
    return run


def approved_leads_from_review(
    run: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    rows = read_review_rows(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run.get("run_id", "")
    )
    approved_rows = [
        row
        for row in rows
        if (ignore_review_approval or checkbox_truthy(row.get("Approved")))
        and clean_text(row.get("Run ID"))
    ]
    lane_scope = clean_text(getattr(args, "review_lane_scope", "all")).lower()
    if lane_scope in {"design", "automation"}:
        approved_rows = [
            row
            for row in approved_rows
            if clean_text(row.get("Primary Lane")).lower() == lane_scope
        ]
    approved_rows = apply_review_slice(approved_rows, getattr(args, "review_slice", ""))
    use_by_id = {clean_text(row.get("Run ID")): clean_text(row.get("Use")) for row in approved_rows}
    approved_by_id = {
        clean_text(row.get("Run ID")): checkbox_truthy(row.get("Approved")) for row in approved_rows
    }
    review_row_by_id = {
        clean_text(row.get("Run ID")): row.get("_row_number") for row in approved_rows
    }
    lead_by_id = {lead.get("id"): lead for lead in run.get("leads", [])}
    approved = []
    for row in approved_rows:
        lead_id = clean_text(row.get("Run ID"))
        lead = lead_by_id.get(lead_id)
        if not lead:
            continue
        frozen = json.loads(json.dumps(lead))
        frozen["review_use"] = use_by_id.get(lead_id, "")
        frozen["review_approved"] = approved_by_id.get(lead_id, False)
        frozen["review_row_number"] = review_row_by_id.get(lead_id)
        approved.append(frozen)
    approved.sort(key=lambda lead: lead.get("source_rows", {}).get("company_row") or 0)
    return approved


def freeze_approved(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    ignore_review_approval = resolve_ignore_review_approval(args)
    approved = approved_leads_from_review(run, args)
    if not approved:
        selected_label = "leads" if ignore_review_approval else "approved leads"
        raise ValueError(f"No {selected_label} found in {args.review_tab}.")

    snapshot_id = now_run_id()
    snapshot_file = SNAPSHOTS_DIR / f"{snapshot_id}_snapshot.json"
    computation_file = COMPUTATIONS_DIR / f"{snapshot_id}_computation.json"

    snapshot = {
        "snapshot_id": snapshot_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_id": run.get("run_id", ""),
        "source_run_file": str(run_file),
        "approved_count": len(approved),
        "selection_mode": "approval_disabled" if ignore_review_approval else "approved_only",
        "review_lane_scope": args.review_lane_scope,
        "review_slice": args.review_slice,
        "leads": approved,
    }

    archive = ResearchArchive(archive_index_path(args))
    computation_leads = []
    archive_reused_count = 0
    for lead in approved:
        computation_lead = {
            "lead_id": lead.get("id", ""),
            "company": lead.get("company", {}).get("name", ""),
            "website": lead.get("company", {}).get("website", ""),
            "company_linkedin": lead.get("company", {}).get("linkedin", ""),
            "emp_count": lead.get("company", {}).get("employee_count")
            or len(lead.get("employees_from_sheet", [])),
            "source_tab": lead.get("source_tab", ""),
            "use": lead.get("review_use", ""),
            "review_approved": bool(lead.get("review_approved")),
            "review_row_number": lead.get("review_row_number"),
            "primary_lane": lead.get("primary_lane", ""),
            "prep_wave": lead.get("prep_wave", "Base"),
            "source_rows": lead.get("source_rows", {}),
            "employees_from_sheet": lead.get("employees_from_sheet", []),
            "executives": [],
            "search_tasks": [],
            "search_results": [],
            "destination_row": {},
            "status": "research_pending",
            "notes": [],
        }
        match = lead.get("archive_match") or archive.match(lead)
        computation_lead["archive_match"] = match
        if match.get("status") == MATCH_AVAILABLE and match.get("archive_entry_id"):
            entry = archive.get(match["archive_entry_id"])
            if entry:
                computation_lead = hydrate_computation_lead(computation_lead, entry)
                computation_lead["archive_match"] = match
                archive_reused_count += 1
        elif match.get("status") == MATCH_CONFLICT:
            computation_lead["status"] = "archive_conflict"
            computation_lead["notes"].append(
                "Archive identity conflict requires manual resolution."
            )
        elif match.get("status") == MATCH_CONSUMED:
            computation_lead["status"] = "archive_consumed"
            computation_lead["notes"].append(
                "Archive research was already consumed by an earlier publication."
            )
        computation_leads.append(computation_lead)

    selection_mode = "approval_disabled" if ignore_review_approval else "approved_only"
    computation = {
        "computation_id": snapshot_id,
        "created_at": snapshot["created_at"],
        "snapshot_file": str(snapshot_file),
        "source_run_file": str(run_file),
        "approved_fingerprint": lead_id_fingerprint([lead.get("id", "") for lead in approved]),
        "selection_mode": selection_mode,
        "review_lane_scope": args.review_lane_scope,
        "review_slice": args.review_slice,
        "publication_mode": "publish_all"
        if selection_mode == "approval_disabled"
        else "publish_approved",
        "status": "research_pending"
        if archive_reused_count < len(computation_leads)
        else "archive_reused",
        "lead_count": len(computation_leads),
        "archive_reused_count": archive_reused_count,
        "fresh_research_count": len(computation_leads) - archive_reused_count,
        "post_review_reconciliation": {
            "owner_flow": "process_approved_leads",
            "selection_mode": selection_mode,
            "review_lane_scope": args.review_lane_scope,
            "review_slice": args.review_slice,
            "archive_reuse_applied": archive_reused_count,
            "archive_entries_consumed": 0,
        },
        "leads": computation_leads,
        "errors": [],
    }

    save_run(snapshot, snapshot_file)
    save_run(computation, computation_file)
    return snapshot, snapshot_file, computation, computation_file


def build_research_prompt(
    computation: dict[str, Any], args: argparse.Namespace
) -> tuple[str, list[dict[str, Any]]]:
    pending = [
        lead
        for lead in computation.get("leads", [])
        if lead.get("status") == "research_pending" and not lead.get("executives")
    ]
    batch = pending[: args.research_batch_size]
    lines = [
        "For each company below, find the names of the top 3 executives or most senior team members.",
        "",
        "Prioritize founders, owners, CEOs, managing directors, partners, directors, and other clear senior decision makers.",
        "Do not choose generic visible employees just because they are easy to find if a more senior founder/owner/executive is publicly identifiable.",
        "LinkedIn is still important: include the LinkedIn URL when publicly available, but do not invent or guess a URL.",
        "",
        "Return one table with these columns:",
        "- Company",
        "- Names",
        "- Titles",
        "- Linkedin url if publicly available",
        "",
        "Only include people who are clearly connected to the company.",
        "",
        "Companies:",
    ]
    for index, lead in enumerate(batch, start=1):
        lines.append(f"{index}. {lead.get('company', '')} - {lead.get('website', '')}")
    return "\n".join(lines), batch


def write_research_prompt(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> Path:
    prompt, batch = build_research_prompt(computation, args)
    prompt_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompt_file = PROMPTS_DIR / f"{prompt_id}_research_prompt.md"
    payload = [
        f"# Research Prompt {prompt_id}",
        "",
        f"Computation file: `{computation_file}`",
        "",
        prompt,
        "",
        "Lead IDs in this batch:",
    ]
    for lead in batch:
        payload.append(f"- {lead.get('lead_id', '')}: {lead.get('company', '')}")
    prompt_file.write_text("\n".join(payload) + "\n")
    return prompt_file


def normalize_research_executives(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("executives", [])
    if not isinstance(raw, list):
        return []
    executives = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = clean_text(
            item.get("name") or item.get("Name") or item.get("names") or item.get("Names")
        )
        title = clean_text(
            item.get("title") or item.get("Title") or item.get("titles") or item.get("Titles")
        )
        linkedin = normalize_url(
            item.get("linkedin_url")
            or item.get("linkedin")
            or item.get("Linkedin url if publicly available")
            or item.get("LinkedIn")
            or ""
        )
        if not name:
            continue
        executives.append(
            {
                "name": name,
                "title": title,
                "linkedin_url": linkedin,
                "linkedin_source": "research" if linkedin else "",
                "email": "",
                "research_source": "codex",
                "needs_linkedin_search": not bool(linkedin),
                "reconciliation_notes": [],
            }
        )
    return executives[:3]


def apply_research_results(computation: dict[str, Any], research_file: Path) -> dict[str, Any]:
    data = json.loads(research_file.read_text())
    if isinstance(data, dict) and "results" in data:
        rows = data["results"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("Research file must be a list or an object with a 'results' list.")

    lead_by_company = {
        normalize_key(lead.get("company", "")): lead for lead in computation.get("leads", [])
    }
    applied = 0
    unmatched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = clean_text(row.get("company") or row.get("Company"))
        lead = lead_by_company.get(normalize_key(company))
        if not lead:
            unmatched.append(company)
            continue
        executives = normalize_research_executives(row)
        lead["executives"] = executives
        lead["status"] = "reconciliation_pending"
        applied += 1

    computation["status"] = "reconciliation_pending"
    computation.setdefault("research_imports", []).append(
        {
            "file": str(research_file),
            "applied": applied,
            "unmatched_companies": unmatched,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return computation


def reconcile_computation_existing_data(computation: dict[str, Any]) -> dict[str, Any]:
    all_search_tasks = []
    for lead in computation.get("leads", []):
        executives = lead.get("executives", [])
        if not executives:
            continue
        employees = lead.get("employees_from_sheet", [])
        lead_tasks = []
        for executive in executives:
            if executive.get("linkedin_url"):
                executive["needs_linkedin_search"] = False
                continue

            matched = match_employee({"name": executive.get("name", "")}, employees)
            if matched:
                executive["email"] = matched.get("email", "") or executive.get("email", "")
                if not executive.get("title") and matched.get("role"):
                    executive["title"] = matched.get("role", "")
                score = linkedin_url_name_score(
                    executive.get("name", ""), matched.get("linkedin", "")
                )
                if matched.get("linkedin") and score >= 0.34:
                    executive["linkedin_url"] = matched.get("linkedin", "")
                    executive["linkedin_source"] = "source_employee_row"
                    executive["needs_linkedin_search"] = False
                    executive.setdefault("reconciliation_notes", []).append(
                        "LinkedIn matched on same employee row."
                    )
                    continue
                executive.setdefault("reconciliation_notes", []).append(
                    "Name matched source employee row."
                )

            best_url = ""
            best_score = 0.0
            for employee in employees:
                url = employee.get("linkedin", "")
                score = linkedin_url_name_score(executive.get("name", ""), url)
                if url and score > best_score:
                    best_url = url
                    best_score = score
            if best_url and best_score >= 0.34:
                executive["linkedin_url"] = best_url
                executive["linkedin_source"] = "source_group_url_match"
                executive["needs_linkedin_search"] = False
                executive.setdefault("reconciliation_notes", []).append(
                    "LinkedIn matched by name parts across source group URLs."
                )
                continue

            task = {
                "lead_id": lead.get("lead_id", ""),
                "company": lead.get("company", ""),
                "person_name": executive.get("name", ""),
                "title": executive.get("title", ""),
                "query": search_query_variants(executive.get("name", ""), lead.get("company", ""))[
                    0
                ],
                "queries": search_query_variants(
                    executive.get("name", ""), lead.get("company", "")
                ),
                "status": "pending",
            }
            executive["needs_linkedin_search"] = True
            executive["search_task_query"] = task["query"]
            lead_tasks.append(task)
            all_search_tasks.append(task)
        lead["search_tasks"] = lead_tasks
        lead["status"] = "linkedin_search_pending" if lead_tasks else "reconciled"

    computation["search_tasks"] = all_search_tasks
    computation["status"] = "linkedin_search_pending" if all_search_tasks else "reconciled"
    computation["reconciled_at"] = datetime.now().isoformat(timespec="seconds")
    return computation


def score_task_candidate(task: dict[str, Any], result: dict[str, str]) -> float:
    exec_item = {"name": task.get("person_name", ""), "role": task.get("title", "")}
    company = {"name": task.get("company", "")}
    return score_search_candidate(exec_item, company, result)


def find_executive_for_task(
    computation: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any] | None:
    for lead in computation.get("leads", []):
        if lead.get("lead_id") != task.get("lead_id"):
            continue
        for executive in lead.get("executives", []):
            if normalize_key(executive.get("name", "")) == normalize_key(
                task.get("person_name", "")
            ):
                return executive
    return None


def run_search_tasks(
    computation: dict[str, Any], computation_file: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], Path]:
    tasks = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    tasks = tasks[: args.max_search_tasks]
    search_client = SearchClient(delay_min=args.delay_min, delay_max=args.delay_max)
    result_batch = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "computation_file": str(computation_file),
        "task_count": len(tasks),
        "results": [],
    }

    for task in tasks:
        item = dict(task)
        try:
            queries = task.get("queries") or [task.get("query", "")]
            results = []
            best_scored = []
            search_errors = []
            for query in queries:
                try:
                    results = search_client.search(query, limit=5)
                except Exception as exc:
                    search_errors.append(f"{query}: {exc}")
                    continue
                if results:
                    item["query_used"] = query
                scored_for_query = []
                for result in results:
                    scored_item = dict(result)
                    scored_item.setdefault("query", query)
                    scored_item["score"] = score_task_candidate(task, result)
                    scored_for_query.append(scored_item)
                scored_for_query.sort(key=lambda row: row.get("score", 0), reverse=True)
                if scored_for_query and not best_scored:
                    best_scored = scored_for_query
                if (
                    scored_for_query
                    and scored_for_query[0].get("score", 0) >= args.search_accept_threshold
                ):
                    best_scored = scored_for_query
                    break
            if not results and search_errors:
                raise RuntimeError("; ".join(search_errors))
            scored = best_scored
            selected = (
                scored[0]
                if scored and scored[0].get("score", 0) >= args.search_accept_threshold
                else None
            )
            item["results"] = scored
            item["selected"] = selected
            item["status"] = "selected" if selected else "needs_review"
            if selected:
                executive = find_executive_for_task(computation, task)
                if executive is not None:
                    executive["linkedin_url"] = normalize_url(selected.get("url", ""))
                    executive["linkedin_source"] = "search_task"
                    executive["needs_linkedin_search"] = False
                    executive.setdefault("reconciliation_notes", []).append(
                        "LinkedIn selected from search task results."
                    )
        except Exception as exc:
            item["results"] = []
            item["selected"] = None
            item["status"] = "error"
            item["error"] = str(exc)
        result_batch["results"].append(item)

    completed_status_by_key = {
        (row.get("lead_id"), row.get("person_name")): row.get("status", "needs_review")
        for row in result_batch["results"]
    }
    for task in computation.get("search_tasks", []):
        key = (task.get("lead_id"), task.get("person_name"))
        if key in completed_status_by_key:
            task["status"] = completed_status_by_key[key]

    remaining_pending = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    computation.setdefault("search_result_batches", []).append(result_batch)
    computation["status"] = (
        "linkedin_search_pending" if remaining_pending else "search_tasks_completed"
    )
    computation["search_tasks_remaining"] = len(remaining_pending)
    result_file = (
        SEARCH_RESULTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_search_results.json"
    )
    result_file.write_text(json.dumps(result_batch, indent=2, ensure_ascii=False) + "\n")
    save_run(computation, computation_file)
    return computation, result_file


def apply_search_result_batch(
    computation: dict[str, Any], result_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    batch = json.loads(result_file.read_text())
    normalized_batch = {
        "created_at": batch.get("created_at", datetime.now().isoformat(timespec="seconds")),
        "source_file": str(result_file),
        "status": batch.get("status", ""),
        "task_count": batch.get("task_count", len(batch.get("results", []))),
        "completed_tasks": batch.get("completed_tasks", len(batch.get("results", []))),
        "remaining_tasks": batch.get("remaining_tasks", len(batch.get("pending_tasks", []))),
        "pending_tasks": batch.get("pending_tasks", []),
        "captcha_events": batch.get("captcha_events", 0),
        "hot_profiles": batch.get("hot_profiles", []),
        "profile_transfers": batch.get("profile_transfers", []),
        "results": [],
    }
    completed_status_by_key = {}
    for item in batch.get("results", []):
        scored = []
        for result in item.get("results", []):
            scored_item = dict(result)
            scored_item["score"] = score_task_candidate(item, result)
            scored.append(scored_item)
        scored.sort(key=lambda row: row.get("score", 0), reverse=True)
        selected = (
            scored[0]
            if scored and scored[0].get("score", 0) >= args.search_accept_threshold
            else None
        )
        out = dict(item)
        out["results"] = scored
        out["selected"] = selected
        source_status = item.get("status", "")
        if selected:
            out["status"] = "selected"
        elif source_status in {"error", "captcha", "paused", "paused_due_to_all_profiles_hot"}:
            out["status"] = source_status
        else:
            out["status"] = "needs_review"
        normalized_batch["results"].append(out)
        completed_status_by_key[(item.get("lead_id"), item.get("person_name"))] = out["status"]
        if selected:
            executive = find_executive_for_task(computation, item)
            if executive is not None:
                executive["linkedin_url"] = normalize_url(selected.get("url", ""))
                executive["linkedin_source"] = "playwright_search_task"
                executive["needs_linkedin_search"] = False
                executive.setdefault("reconciliation_notes", []).append(
                    "LinkedIn selected from Playwright search task results."
                )

    for task in computation.get("search_tasks", []):
        key = (task.get("lead_id"), task.get("person_name"))
        if key in completed_status_by_key:
            task["status"] = completed_status_by_key[key]

    remaining_pending = [
        task
        for task in computation.get("search_tasks", [])
        if task.get("status", "pending") == "pending"
    ]
    computation.setdefault("search_result_batches", []).append(normalized_batch)
    computation["search_tasks_remaining"] = len(remaining_pending)
    if normalized_batch.get("status") == "paused_due_to_all_profiles_hot" or normalized_batch.get(
        "pending_tasks"
    ):
        computation["status"] = "playwright_search_paused"
    else:
        computation["status"] = (
            "linkedin_search_pending" if remaining_pending else "search_tasks_completed"
        )
    return computation


def process_run(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> dict[str, Any]:
    search_client = SearchClient(delay_min=args.delay_min, delay_max=args.delay_max)
    lead_by_id = {lead["id"]: lead for lead in run.get("leads", [])}
    for batch in run.get("batches", []):
        if batch.get("status") == "completed":
            continue
        batch["status"] = "in_progress"
        save_run(run, run_file)
        for lead_id in batch.get("lead_ids", []):
            group = lead_by_id[lead_id]
            if group.get("status") == "completed":
                continue
            try:
                execs = search_execs_for_company(search_client, group["company"], max_execs=3)
                if not execs:
                    execs = fallback_execs_from_employees(
                        group.get("employees_from_sheet", []), max_execs=3
                    )
                group["execs_found"] = execs
                group["finalized_execs"] = [
                    reconcile_exec(search_client, group, exec_item) for exec_item in execs
                ]
                group["destination_row"] = build_destination_row(group)
                group["status"] = "completed"
            except Exception as exc:
                group["status"] = "error"
                group.setdefault("errors", []).append(str(exc))
                run.setdefault("errors", []).append({"lead_id": lead_id, "error": str(exc)})
            save_run(run, run_file)
        batch["status"] = "completed"
        save_run(run, run_file)

    run["destination_rows"] = [
        lead.get("destination_row", {})
        for lead in run.get("leads", [])
        if lead.get("status") == "completed" and lead.get("destination_row")
    ]
    run["status"] = "processed"
    save_run(run, run_file)
    return run


def process_approved_run(
    run: dict[str, Any], run_file: Path, args: argparse.Namespace
) -> dict[str, Any]:
    run = filter_run_to_approved(run, args)
    save_run(run, run_file)
    status_by_id = {lead.get("id", ""): "Processing" for lead in run.get("leads", [])}
    update_review_statuses(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run["run_id"], status_by_id
    )
    run = process_run(run, run_file, args)
    final_status = {
        lead.get("id", ""): ("Processed" if lead.get("status") == "completed" else "Error")
        for lead in run.get("leads", [])
    }
    update_review_statuses(
        Path(args.credentials), source_sheet_url(args), args.review_tab, run["run_id"], final_status
    )
    return run


def push_run(run: dict[str, Any], run_file: Path, args: argparse.Namespace) -> int:
    if args.dry_run:
        run["rows_written"] = 0
        run["status"] = "dry_run_completed"
        save_run(run, run_file)
        return 0
    rows_written = write_destination_rows(
        Path(args.credentials),
        destination_sheet_url(args),
        args.destination_tab,
        run.get("destination_rows", []),
    )
    run["rows_written"] = rows_written
    run["status"] = "completed"
    save_run(run, run_file)
    return rows_written


def print_summary(run: dict[str, Any], run_file: Path, rows_written: int, dry_run: bool) -> None:
    print("Lead exec research summary")
    print(f"Run file: {run_file}")
    print(
        f"Selected leads: {run.get('source', {}).get('selected_count', len(run.get('leads', [])))}"
    )
    if "approved_count" in run.get("source", {}):
        print(f"Approved leads: {run.get('source', {}).get('approved_count', 0)}")
    print(f"Batches: {len(run.get('batches', []))}")
    if run.get("review"):
        print(f"Review tab: {run['review'].get('tab', '')}")
        print(f"Review rows written: {run['review'].get('rows_written', 0)}")
        if run["review"].get("write"):
            write = run["review"].get("write", {})
            print(f"Review group date: {write.get('date', '')}")
            print(f"Review group row: {write.get('group_row', '')}")
        notification = run["review"].get("notification", {})
        print(f"Email notification: {notification.get('sent', False)}")
        if notification.get("reason"):
            print(f"Email note: {notification.get('reason')}")
    print(f"Destination rows ready: {len(run.get('destination_rows', []))}")
    print(f"Rows written: {rows_written}")
    print(f"Dry run: {dry_run}")
    print(f"Errors: {len(run.get('errors', []))}")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sheet-url",
        default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_SHEET_URL),
        help="Google Sheet URL when source and destination are the same.",
    )
    parser.add_argument(
        "--source-sheet-url",
        default=os.environ.get("LEAD_RESEARCH_SOURCE_SHEET_URL", ""),
        help="Source Google Sheet URL.",
    )
    parser.add_argument(
        "--destination-sheet-url",
        default=os.environ.get("LEAD_RESEARCH_DESTINATION_SHEET_URL", ""),
        help="Destination Google Sheet URL.",
    )
    parser.add_argument(
        "--source-tab", default=os.environ.get("LEAD_RESEARCH_SOURCE_TAB", DEFAULT_SOURCE_TAB)
    )
    parser.add_argument(
        "--destination-tab",
        default=os.environ.get("LEAD_RESEARCH_DESTINATION_TAB", DEFAULT_DESTINATION_TAB),
    )
    parser.add_argument(
        "--final-tab", default=os.environ.get("LEAD_RESEARCH_FINAL_TAB", DEFAULT_FINAL_TAB)
    )
    parser.add_argument(
        "--prospects-sheet-url", default=os.environ.get("OBF_SHEET_URL", DEFAULT_OBF_SHEET_URL)
    )
    parser.add_argument(
        "--prospects-tab", default=os.environ.get("OBF_PROSPECTS_TAB", DEFAULT_PROSPECTS_TAB)
    )
    parser.add_argument("--target-date", default=os.environ.get("BRIDGE_TARGET_DATE", "today"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--review-tab", default=os.environ.get("LEAD_RESEARCH_REVIEW_TAB", DEFAULT_REVIEW_TAB)
    )
    parser.add_argument(
        "--notification-queue-tab",
        default=os.environ.get(
            "LEAD_RESEARCH_NOTIFICATION_QUEUE_TAB", DEFAULT_NOTIFICATION_QUEUE_TAB
        ),
    )
    parser.add_argument(
        "--notify-email",
        default=os.environ.get(
            "LEAD_RESEARCH_NOTIFY_EMAIL", os.environ.get("NOTIFY_TO", DEFAULT_NOTIFY_EMAIL)
        ),
    )
    parser.add_argument(
        "--no-email", action="store_true", help="Skip review-ready email notification."
    )
    parser.add_argument(
        "--no-queue-notification",
        dest="queue_notification",
        action="store_false",
        default=True,
        help="Do not write to the Notification Queue tab when SMTP fails.",
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument(
        "--archive-index",
        default=os.environ.get("LEAD_RESEARCH_ARCHIVE_INDEX", str(DEFAULT_RESEARCH_ARCHIVE_INDEX)),
        help="Local reusable research archive index.",
    )
    parser.add_argument(
        "--disable-overlap-scan",
        "--disable-archive-reconciliation",
        dest="disable_overlap_scan",
        action="store_true",
        default=os.environ.get(
            "LEAD_OVERLAP_SCAN_ENABLED",
            os.environ.get("LEAD_ARCHIVE_RECONCILIATION_ENABLED", "1"),
        )
        .strip()
        .lower()
        in {"0", "false", "no", "off"},
        help="Pause the detection-only archive overlap scan.",
    )
    parser.add_argument(
        "--disable-overlap-top-ups",
        dest="disable_overlap_top_ups",
        action="store_true",
        default=os.environ.get("LEAD_OVERLAP_TOP_UPS_ENABLED", "1").strip().lower()
        in {"0", "false", "no", "off"},
        help="Keep the overlap scan but stop after the single base review batch.",
    )
    parser.add_argument(
        "--archive-reason",
        default="unreviewed_processing",
        help="Audit reason recorded when archiving a computation.",
    )
    parser.add_argument("--delay-min", type=float, default=2.0)
    parser.add_argument("--delay-max", type=float, default=5.0)
    parser.add_argument("--snapshot-file", default="")
    parser.add_argument("--computation-file", default="")
    parser.add_argument("--research-file", default="")
    parser.add_argument("--search-result-file", default="")
    parser.add_argument("--research-batch-size", type=int, default=5)
    parser.add_argument("--max-search-tasks", type=int, default=50)
    parser.add_argument("--search-accept-threshold", type=float, default=0.45)
    parser.add_argument("--approval-threshold", type=int, default=20)
    approval_mode = parser.add_mutually_exclusive_group()
    approval_mode.add_argument(
        "--ignore-review-approval",
        dest="ignore_review_approval",
        action="store_true",
        default=None,
        help="Process every Lead Review row, overriding the shared approval-gate setting.",
    )
    approval_mode.add_argument(
        "--require-review-approval",
        dest="ignore_review_approval",
        action="store_false",
        help="Process approved Lead Review rows only, overriding the shared approval-gate setting.",
    )
    parser.add_argument(
        "--review-lane-scope",
        choices=("all", "design", "automation"),
        default="all",
        help="Limit Lead Review status/freeze selection to a primary lane.",
    )
    parser.add_argument(
        "--review-slice",
        default="",
        help="Process a stable slice of selected Lead Review rows as N/M, for example 1/3.",
    )
    parser.add_argument("--write-limit", type=int, default=0)
    parser.add_argument("--write-start-row", type=int, default=2)
    parser.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Allow write-computation to write rows that fail readiness checks.",
    )
    parser.add_argument(
        "--require-p2",
        action="store_true",
        help="Require P2 name and LinkedIn before writing/bridging rows.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-file", default="")


def validate_args(args: argparse.Namespace) -> None:
    if not source_sheet_url(args):
        raise SystemExit("Missing --source-sheet-url or --sheet-url.")
    if not destination_sheet_url(args):
        raise SystemExit("Missing --destination-sheet-url or --sheet-url.")
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1.")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1.")
    if args.research_batch_size < 1:
        raise SystemExit("--research-batch-size must be >= 1.")
    if args.max_search_tasks < 1:
        raise SystemExit("--max-search-tasks must be >= 1.")
    if args.approval_threshold < 1:
        raise SystemExit("--approval-threshold must be >= 1.")
    try:
        parse_review_slice(args.review_slice)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.write_limit < 0:
        raise SystemExit("--write-limit must be >= 0.")
    if args.write_start_row < 2:
        raise SystemExit("--write-start-row must be >= 2.")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Lead executive research workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "preflight",
        "pull",
        "prepare-review",
        "notify-review",
        "status-approved",
        "resume-approved",
        "freeze-approved",
        "research-prompt",
        "apply-research",
        "reconcile-existing",
        "export-search-tasks",
        "run-search-tasks",
        "apply-search-results",
        "write-computation",
        "archive-status",
        "archive-computation",
        "archive-unreviewed",
        "bridge-prefinal-to-prospects",
        "process",
        "process-approved",
        "push",
        "run",
    ):
        sub = subparsers.add_parser(command)
        add_common_args(sub)
    args = parser.parse_args(argv)
    if args.command == "bridge-prefinal-to-prospects" and "--source-tab" not in raw_argv:
        # Bridge reads from Pre-final by default even if the workflow's global DEFAULT_SOURCE_TAB differs.
        args.source_tab = "Pre-final"
    validate_args(args)
    ensure_dirs()

    if args.command == "preflight":
        print(json.dumps(preflight(args), indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-status":
        archive = ResearchArchive(archive_index_path(args))
        print(json.dumps(archive.stats(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-computation":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit("No computation file found. Pass --computation-file.")
        computation = load_run(computation_file)
        result = archive_computation_state(computation, computation_file, args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "archive-unreviewed":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit("No computation file found. Pass --computation-file.")
        computation = load_run(computation_file)
        if computation.get("publication_mode") != "archive_only":
            raise SystemExit(
                "Refusing to archive and remove a reviewed group. "
                "archive-unreviewed only accepts archive_only computations."
            )
        result = archive_unreviewed_computation(computation, computation_file, args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    run_file: Path
    if args.command == "pull":
        run, run_file = build_run(args)
        print_summary(run, run_file, rows_written=0, dry_run=args.dry_run)
        return 0

    if args.command == "prepare-review":
        run, run_file = prepare_review(args)
        print_summary(run, run_file, rows_written=0, dry_run=args.dry_run)
        return 0

    if args.command == "status-approved":
        print(json.dumps(approved_workflow_status(args), indent=2, ensure_ascii=False))
        return 0

    if args.command == "resume-approved":
        status = approved_workflow_status(args)
        print(json.dumps(status, indent=2, ensure_ascii=False))
        action = status.get("next_action")
        computation_file = (
            Path(status.get("computation_file", "")) if status.get("computation_file") else None
        )
        if action in {"claim_and_freeze", "freeze_approved"}:
            if action == "claim_and_freeze":
                claim = claim_approved_gate(args)
                if not claim.get("claimed"):
                    raise SystemExit(
                        f"Could not claim approved group: {claim.get('reason', 'unknown reason')}"
                    )
            latest = latest_review_run_file()
            if latest is None:
                raise SystemExit("No review run file found for freeze-approved.")
            run = load_run(latest)
            _snapshot, snapshot_file, _computation, new_computation_file = freeze_approved(
                run, latest, args
            )
            print(f"Snapshot file: {snapshot_file}")
            print(f"Computation file: {new_computation_file}")
        elif action == "research_batch":
            if not computation_file:
                raise SystemExit("No computation file available for research prompt.")
            computation = load_run(computation_file)
            prompt_file = write_research_prompt(computation, computation_file, args)
            print(f"Prompt file: {prompt_file}")
        elif action == "reconcile_existing":
            if not computation_file:
                raise SystemExit("No computation file available for reconcile-existing.")
            computation = reconcile_computation_existing_data(load_run(computation_file))
            save_run(computation, computation_file)
            print(f"Reconciled existing data: {computation_file}")
            print(f"Search tasks: {len(computation.get('search_tasks', []))}")
        elif action == "run_playwright_search":
            if not computation_file:
                raise SystemExit("No computation file available for search task export.")
            computation = load_run(computation_file)
            task_file, tasks = export_pending_search_tasks(computation, computation_file, args)
            print(f"Search tasks file: {task_file}")
            print(f"Search tasks: {len(tasks)}")
        elif action == "write_computation":
            if not computation_file:
                raise SystemExit("No computation file available for write-computation.")
            computation = load_run(computation_file)
            if args.dry_run:
                rows, skipped = computation_rows_for_write(computation, args)
                rows_written = 0
                queue_batch = {
                    "dry_run": True,
                    "fingerprint": rows_fingerprint(rows) if rows else "",
                    "row_count": len(rows),
                }
            else:
                rows_written, rows, skipped, queue_batch = write_computation_rows(
                    computation, args, computation_file
                )
                consume_reused_archive_entries(computation, rows, computation_file, args)
            computation.setdefault("writes", []).append(
                {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "destination_tab": args.destination_tab,
                    "write_limit": args.write_limit,
                    "include_unresolved": args.include_unresolved,
                    "require_p2": args.require_p2,
                    "dry_run": args.dry_run,
                    "rows_written": rows_written,
                    "lead_ids": [row.get("ID", "") for row in rows],
                    "queue_batch": queue_batch,
                    "skipped_unresolved_count": len(skipped),
                    "skipped_unresolved": skipped,
                }
            )
            computation["status"] = "written" if rows_written and not skipped else "write_partial"
            save_run(computation, computation_file)
            print(f"Rows written: {rows_written}")
            print(f"Skipped unresolved: {len(skipped)}")
        elif action == "archive_unreviewed":
            if not computation_file:
                raise SystemExit("No computation file available for archive-unreviewed.")
            computation = load_run(computation_file)
            result = archive_unreviewed_computation(computation, computation_file, args)
            print(f"Archived leads: {result.get('archived_count', 0)}")
            print(f"Skipped leads: {result.get('skipped_count', 0)}")
            removal = result.get("review_group_removal", {})
            print(
                "Review group removed: "
                f"{removal.get('start_row', '?')}–{removal.get('end_row', '?')}"
            )
        elif action == "mark_processed":
            fingerprint = clean_text(status.get("gate", {}).get("fingerprint"))
            if not fingerprint:
                raise SystemExit("No approved-group fingerprint available to mark processed.")
            result = mark_approved_gate(args, fingerprint, "processed")
            print(f"Marked processed: {result.get('fingerprint', fingerprint)}")
        else:
            print(f"No automatic local resume step for action: {action}")
        return 0

    if args.command == "research-prompt":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        prompt_file = write_research_prompt(computation, computation_file, args)
        print(prompt_file.read_text())
        print(f"Prompt file: {prompt_file}")
        return 0

    if args.command == "apply-research":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        if not args.research_file:
            raise SystemExit("Missing --research-file.")
        computation = load_run(computation_file)
        computation = apply_research_results(computation, Path(args.research_file))
        save_run(computation, computation_file)
        latest_import = computation.get("research_imports", [{}])[-1]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Research file: {args.research_file}")
        print(f"Companies applied: {latest_import.get('applied', 0)}")
        print(f"Unmatched companies: {len(latest_import.get('unmatched_companies', []))}")
        return 0

    if args.command == "reconcile-existing":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        computation = reconcile_computation_existing_data(computation)
        save_run(computation, computation_file)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Status: {computation.get('status')}")
        print(f"Search tasks: {len(computation.get('search_tasks', []))}")
        return 0

    if args.command == "export-search-tasks":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        task_file, tasks = export_pending_search_tasks(computation, computation_file, args)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search tasks file: {task_file}")
        print(f"Search tasks: {len(tasks)}")
        return 0

    if args.command == "run-search-tasks":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        computation, result_file = run_search_tasks(computation, computation_file, args)
        latest_batch = computation.get("search_result_batches", [{}])[-1]
        selected = [
            row for row in latest_batch.get("results", []) if row.get("status") == "selected"
        ]
        needs_review = [
            row for row in latest_batch.get("results", []) if row.get("status") == "needs_review"
        ]
        errors = [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search result file: {result_file}")
        print(f"Tasks processed: {latest_batch.get('task_count', 0)}")
        print(f"Selected: {len(selected)}")
        print(f"Needs review: {len(needs_review)}")
        print(f"Errors: {len(errors)}")
        print(f"Remaining pending: {computation.get('search_tasks_remaining', 0)}")
        return 0

    if args.command == "apply-search-results":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        if not args.search_result_file:
            raise SystemExit("Missing --search-result-file.")
        computation = load_run(computation_file)
        computation = apply_search_result_batch(computation, Path(args.search_result_file), args)
        save_run(computation, computation_file)
        latest_batch = computation.get("search_result_batches", [{}])[-1]
        selected = [
            row for row in latest_batch.get("results", []) if row.get("status") == "selected"
        ]
        needs_review = [
            row for row in latest_batch.get("results", []) if row.get("status") == "needs_review"
        ]
        errors = [row for row in latest_batch.get("results", []) if row.get("status") == "error"]
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Search result file: {args.search_result_file}")
        print(f"Selected: {len(selected)}")
        print(f"Needs review: {len(needs_review)}")
        print(f"Errors: {len(errors)}")
        print(f"Remaining pending: {computation.get('search_tasks_remaining', 0)}")
        return 0

    if args.command == "write-computation":
        computation_file = (
            Path(args.computation_file) if args.computation_file else latest_computation_file()
        )
        if computation_file is None:
            raise SystemExit(
                "No computation file found. Run freeze-approved first or pass --computation-file."
            )
        computation = load_run(computation_file)
        if args.dry_run:
            rows, skipped = computation_rows_for_write(computation, args)
            rows_written = 0
            queue_batch = {
                "dry_run": True,
                "fingerprint": rows_fingerprint(rows) if rows else "",
                "row_count": len(rows),
            }
        else:
            rows_written, rows, skipped, queue_batch = write_computation_rows(
                computation, args, computation_file
            )
            consume_reused_archive_entries(computation, rows, computation_file, args)
        computation.setdefault("writes", []).append(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "destination_tab": args.destination_tab,
                "write_limit": args.write_limit,
                "include_unresolved": args.include_unresolved,
                "require_p2": args.require_p2,
                "dry_run": args.dry_run,
                "rows_written": rows_written,
                "lead_ids": [row.get("ID", "") for row in rows],
                "queue_batch": queue_batch,
                "skipped_unresolved_count": len(skipped),
                "skipped_unresolved": skipped,
            }
        )
        computation["status"] = "written" if rows_written and not skipped else "write_partial"
        save_run(computation, computation_file)
        print("Lead exec research summary")
        print(f"Computation file: {computation_file}")
        print(f"Destination tab: {args.destination_tab}")
        print(f"Rows ready: {len(rows)}")
        print(f"Rows written: {rows_written}")
        print(f"Queue batch: {queue_batch.get('fingerprint', '')}")
        print(f"Skipped unresolved: {len(skipped)}")
        for item in skipped:
            reasons = ", ".join(item.get("reasons", []))
            print(f"- {item.get('lead_id', '')} {item.get('company', '')}: {reasons}")
        return 0

    if args.command == "bridge-prefinal-to-prospects":
        result = bridge_prefinal_to_prospects(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1

    if args.run_file:
        run_file = Path(args.run_file)
        run = load_run(run_file)
    else:
        if args.command in {"process-approved", "notify-review", "freeze-approved"}:
            latest = latest_review_run_file()
            if latest is None:
                raise SystemExit("No run file found. Run prepare-review first or pass --run-file.")
            run_file = latest
            run = load_run(run_file)
        else:
            run, run_file = build_run(args)

    rows_written = int(run.get("rows_written", 0) or 0)
    if args.command == "freeze-approved":
        snapshot, snapshot_file, computation, computation_file = freeze_approved(
            run, run_file, args
        )
        print("Lead exec research summary")
        print(f"Run file: {run_file}")
        print(f"Snapshot file: {snapshot_file}")
        print(f"Computation file: {computation_file}")
        print(f"Approved leads frozen: {snapshot.get('approved_count', 0)}")
        return 0
    if args.command == "notify-review":
        run = notify_review(run, run_file, args)
    elif args.command == "process-approved":
        if not args.force:
            raise SystemExit(
                "process-approved is deprecated for normal operation because it bypasses the Codex research flow. "
                "Use freeze-approved -> research-prompt -> apply-research -> reconcile/search -> write-computation, "
                "or pass --force if you intentionally need the legacy shortcut."
            )
        run = process_approved_run(run, run_file, args)
    elif args.command in {"process", "run"}:
        run = process_run(run, run_file, args)
    if args.command in {"push", "run", "process-approved"}:
        rows_written = push_run(run, run_file, args)
    print_summary(run, run_file, rows_written=rows_written, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
