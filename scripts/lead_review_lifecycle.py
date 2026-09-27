#!/usr/bin/env python3
"""Compatibility entry point for the lead review lifecycle.

All logic lives in outbound/leads/lifecycle.py (the leads carve — see
docs/CARVE-LEADS.md). This preserves the historical import surface for the
lifecycle test and the CLI.

    python3 scripts/lead_review_lifecycle.py --help
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (str(ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.lifecycle import (  # noqa: F401
    archive_promotion_plan,
    classify_computation_leads,
    classify_review_rows,
    computation_next_action,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
