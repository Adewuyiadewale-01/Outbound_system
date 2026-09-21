"""Generic state machinery shared by all workflows.

Locking, atomic JSON persistence, the daily action ledger, the deferred
final-actions queue, and deterministic per-day randomness. Extracted from
post_engagement during the engagement carve (architecture §5.1).
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import inspect
import json
import os
import random
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        temp = Path(handle.name)
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def daily_rng(day: str, salt: str) -> random.Random:
    digest = hashlib.sha256(f"post-engagement:{day}:{salt}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))