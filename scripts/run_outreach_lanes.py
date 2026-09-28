#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.outreach.lane_runner."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.outreach.lane_runner import (  # noqa: F401
    OUTREACH_RUNNER,
    ROOT,
    STATE_DIR,
    WORKERS_PATH,
    active_workers,
    cdp_healthy,
    ensure_worker_cdp,
    lane_counts,
    main,
    managed_chrome_pids,
    parse_result,
    prepared_path,
    read_json,
    run_lane,
)

if __name__ == "__main__":
    from outbound.outreach.lane_runner import main

    raise SystemExit(main())
