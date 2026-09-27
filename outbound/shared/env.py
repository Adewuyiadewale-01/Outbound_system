"""Load optional local environment settings without adding secrets to source control.

Extracted from helpers/runtime_environment.py during the leads Stage 3 carve
(docs/CARVE-LEADS.md). Pure move with the path anchor rewritten (parents[2]).
"""

from __future__ import annotations

import os
from pathlib import Path


def load_repo_env() -> None:
    """Load root .env values without overriding explicit process environment values."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and not key.startswith("#"):
            os.environ.setdefault(key, value)
