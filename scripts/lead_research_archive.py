#!/usr/bin/env python3
"""Compatibility entry point for the lead research archive.

All logic lives in outbound/leads/archive.py (the leads carve — see
docs/CARVE-LEADS.md). This preserves the historical import surface for the
migration script and the archive tests.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (str(ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from outbound.leads.archive import (  # noqa: F401
    ARCHIVE_SCHEMA_VERSION,
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    archive_entry_id_for,
    canonical_domain,
    canonical_linkedin_company,
    clean_text,
    empty_archive,
    has_reusable_research,
    hydrate_computation_lead,
    normalize_name,
    normalized_lead,
)
