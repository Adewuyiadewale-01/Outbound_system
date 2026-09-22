"""Filesystem layout for the engagement workflow's state."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state" / "post_engagement"
