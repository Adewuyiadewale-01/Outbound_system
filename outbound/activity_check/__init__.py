"""Activity-check workflow package (carved from scripts/check_prefinal_activity.py).

Self-contained bootstrap: this workflow depends on loose modules that still live
in helpers/ (sheets_helper) and scripts/ (prefinal_queue), so make them importable
regardless of how the package is reached.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _name in ("helpers", "scripts"):
    _path = _ROOT / _name
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
