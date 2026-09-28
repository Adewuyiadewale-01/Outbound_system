#!/usr/bin/env python3
"""Thin entry point: job-discovery CLI (see ``outbound/job_discovery/cli.py``)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outbound.job_discovery.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
