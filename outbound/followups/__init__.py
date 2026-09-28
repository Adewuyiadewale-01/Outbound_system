"""Followups workflow package (relocated from scripts/)."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _name in ("helpers", "scripts"):
    _p = _ROOT / _name
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
