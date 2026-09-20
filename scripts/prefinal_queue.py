#!/usr/bin/env python3
"""Durable local queue for finalized batches waiting on activity checks."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QUEUE_DIR = ROOT / "state" / "prefinal_queue"

QUEUE_ROW_COLUMNS = [
    "ID",
    "Company",
    "Website",
    "Company LinkedIn",
    "Emp Count",
    "Source Tab",
    "Primary Lane",
    "Use",
    "P1 Name",
    "P1 Title",
    "P1 LinkedIn",
    "P1 Email",
    "P2 Name",
    "P2 Title",
    "P2 LinkedIn",
    "P2 Email",
]

OPEN_STATUSES = {"queued", "activity_in_progress", "activity_blocked", "final_bridged"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def queue_dir() -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR


def canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{column: clean_text(row.get(column)) for column in QUEUE_ROW_COLUMNS} for row in rows]


def rows_fingerprint(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        canonical_rows(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def batch_path(fingerprint: str) -> Path:
    return queue_dir() / f"{fingerprint}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_batch(path_or_fingerprint: str | Path) -> dict[str, Any]:
    path = Path(path_or_fingerprint)
    if path.suffix != ".json":
        path = batch_path(str(path_or_fingerprint))
    return json.loads(path.read_text(encoding="utf-8"))


def save_batch(batch: dict[str, Any]) -> dict[str, Any]:
    batch["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write(batch_path(batch["fingerprint"]), batch)
    return batch


def enqueue_batch(rows: list[dict[str, Any]], source_computation_file: str = "") -> dict[str, Any]:
    if not rows:
        raise ValueError("Cannot enqueue an empty Pre-final batch")
    fingerprint = rows_fingerprint(rows)
    path = batch_path(fingerprint)
    if path.exists():
        return load_batch(path)
    now = datetime.now().isoformat(timespec="seconds")
    canonical = canonical_rows(rows)
    batch = {
        "queue_version": 1,
        "fingerprint": fingerprint,
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "rows": canonical,
        "lead_ids": [row["ID"] for row in canonical],
        "row_count": len(canonical),
        "source_computation_file": source_computation_file,
        "prefinal_publish": {"status": "not_published"},
        "activity": {},
        "final_bridge": {},
        "prospects_bridge": {},
    }
    _atomic_write(path, batch)
    return batch


def record_prefinal_publish(
    fingerprint: str,
    *,
    start_row: int,
    verified: bool,
    error: str = "",
) -> dict[str, Any]:
    batch = load_batch(fingerprint)
    batch["prefinal_publish"] = {
        "status": "published" if verified and not error else "failed",
        "start_row": start_row,
        "verified": verified,
        "published_at": datetime.now().isoformat(timespec="seconds")
        if verified and not error
        else "",
        "error": error,
    }
    return save_batch(batch)


def list_batches() -> list[dict[str, Any]]:
    batches = []
    for path in queue_dir().glob("*.json"):
        try:
            batches.append(load_batch(path))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(
        batches, key=lambda batch: (batch.get("created_at", ""), batch.get("fingerprint", ""))
    )


def next_activity_batch() -> dict[str, Any] | None:
    for batch in list_batches():
        if batch.get("status") in {
            "queued",
            "activity_in_progress",
            "activity_blocked",
            "final_bridged",
        }:
            return batch
    return None


def update_batch_status(fingerprint: str, status: str, **details: Any) -> dict[str, Any]:
    batch = load_batch(fingerprint)
    batch["status"] = status
    if details:
        batch.update(details)
    return save_batch(batch)
