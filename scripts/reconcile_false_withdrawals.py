#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.withdrawals.reconcile."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.withdrawals.reconcile import main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
