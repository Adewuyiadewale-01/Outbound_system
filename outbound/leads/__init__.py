"""Leads workflow package (carved from scripts/lead_exec_research.py and siblings).

Self-contained bootstrap: depends on loose modules still in helpers/ (sheets_helper)
and scripts/ (prefinal_queue, lead_research_archive).
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _name in ("helpers", "scripts"):
    _path = _ROOT / _name
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
