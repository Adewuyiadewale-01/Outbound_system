"""Run/path helpers and file discovery for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S7). Pure move.
"""

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.leads.config import (
    BRIDGES_DIR,
    COMPUTATIONS_DIR,
    DEFAULT_RESEARCH_ARCHIVE_INDEX,
    LEAD_PREP_CONFIG_PATH,
    PROMPTS_DIR,
    RESEARCH_ARCHIVE_DIR,
    RUNS_DIR,
    SEARCH_RESULTS_DIR,
    SEARCH_TASKS_DIR,
    SNAPSHOTS_DIR,
)
from outbound.leads.text import clean_text


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
