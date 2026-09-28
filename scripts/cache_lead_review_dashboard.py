#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.leads.dashboard_cache."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.dashboard_cache import (  # noqa: F401
    DEFAULT_CACHE_FILE,
    ROOT,
    build_dashboard_cache,
    checkbox_truthy,
    clean,
    main,
    normalized_use,
    row_value,
    write_cache,
)

if __name__ == "__main__":
    from outbound.leads.dashboard_cache import main

    raise SystemExit(main())
