#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.leads.review_use."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.review_use import (  # noqa: F401
    ALLOWED_USE_VALUES,
    main,
    parse_use,
)

if __name__ == "__main__":
    from outbound.leads.review_use import main

    raise SystemExit(main())
