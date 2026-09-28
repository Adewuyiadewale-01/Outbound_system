#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.leads.review_group_status."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.review_group_status import (  # noqa: F401
    main,
)

if __name__ == "__main__":
    from outbound.leads.review_group_status import main

    raise SystemExit(main())
