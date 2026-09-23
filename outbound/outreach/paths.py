"""Filesystem layout and runtime endpoints for the outreach workflow."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT_DIR / "state" / "outreach_sequences"
JOURNAL_DIR = ROOT_DIR / "state" / "outreach_journal"
ACCEPTANCE_STATE_DIR = ROOT_DIR / "state" / "acceptance_monitoring"
ACCEPTANCE_STATE_FILE = ACCEPTANCE_STATE_DIR / "state.json"
OUTREACH_WORKERS_CONFIG_PATH = ROOT_DIR / "config" / "outreach_workers.json"


def _resolve_default_creds() -> str:
    """Resolve credentials path, checking workspace symlink as fallback."""
    native = Path("~/.openclaw/credentials/google-sheets.json").expanduser()
    if native.exists():
        return str(native)
    workspace_fallback = ROOT_DIR / ".openclaw" / "credentials" / "google-sheets.json"
    if workspace_fallback.exists():
        return str(workspace_fallback)
    return str(native)


DEFAULT_CREDS = _resolve_default_creds()
TASK_MANAGER_URL = os.environ.get("TASK_MANAGER_URL", "")
OBF_SHEET_URL = os.environ.get("OBF_SHEET_URL", "")
