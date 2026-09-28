#!/usr/bin/env python3
"""Compatibility entry point: logic lives in outbound.activity_check.lanes."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "helpers"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.activity_check.lanes import (  # noqa: F401
    ACTIVITY_SCRIPT,
    ROOT,
    STATE_DIR,
    WORKER_CONFIG_PATH,
    active_workers,
    assign_prepared_targets,
    cdp_http_healthy,
    close_worker_cdp,
    ensure_worker_cdp,
    extract_runner_result,
    finalize,
    main,
    managed_chrome_pids,
    pending_404_retry_counts,
    port_open,
    read_json,
    run_lane,
    session_path,
    workers_for_prepared_session,
    write_json_atomic,
)

if __name__ == "__main__":
    from outbound.activity_check.lanes import main

    raise SystemExit(main())
