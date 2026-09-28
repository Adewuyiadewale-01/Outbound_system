#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.leads.review_sync."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.review_sync import (  # noqa: F401
    build_value_updates,
    main,
    normalize_rows,
    normalized_live_bool,
    parse_payload,
)

if __name__ == "__main__":
    from outbound.leads.review_sync import main

    raise SystemExit(main())
