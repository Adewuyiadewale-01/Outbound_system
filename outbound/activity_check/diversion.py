"""Diversion execution wrapper for the activity-check workflow.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S11). Pure move.
"""

from typing import Any

from outbound.activity_check.text import clean_text
from outbound.shared.diversion import _run_diversion


def apply_diversion(
    session: Any | None, plan_item: dict[str, Any], activity_detail: dict[str, Any], dry_run: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "diversion_executed": False}
    if dry_run:
        result["dry_run"] = True
        return result
    diversion = clean_text(plan_item.get("lead_diversion")).lower() or "none"
    diversion_sec = plan_item.get("activity_diversion_sec")
    if session is not None and diversion != "none":
        diversion_result = _run_diversion(
            session=session,
            diversion=diversion,
            diversion_sec=diversion_sec,
            activity=activity_detail,
        )
        result["diversion"] = diversion_result
        result["diversion_executed"] = bool(diversion_result.get("executed"))
        if not diversion_result.get("ok", True):
            result["ok"] = False
            result["error"] = diversion_result.get("error", "diversion_failed")
            return result
    return result
