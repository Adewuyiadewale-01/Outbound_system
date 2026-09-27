"""Pre-flight checks — CDP health, LinkedIn session, quotas, acceptance gate.

Extracted from helpers/linkedin_helper.py during the linkedin_helper carve
(see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S25). Pure move, plus a helpers/
bootstrap so the outreach_helper import survives direct (non-shim) imports.
"""

import sys
import time
from pathlib import Path
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.stealth import inject_stealth
from outbound.shared.danger.detection import detect_page
from outbound.shared.quota import (
    ACCEPTANCE_RATE_CRITICAL,
    ACCEPTANCE_RATE_WARN,
    MAX_CONN_REQ_PER_DAY,
    MAX_CONN_REQ_PER_WEEK,
    MAX_PROFILE_VIEWS_PER_DAY,
    get_counter,
    get_weekly_counter,
    load_state,
)

# outreach_helper still lives in helpers/ (out of scope for this carve); keep it
# importable regardless of how this module was reached.
_HELPERS_DIR = Path(__file__).resolve().parents[3] / "helpers"
if str(_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPERS_DIR))

try:
    from outreach_helper import (
        count_outreach_log_connection_requests,
        count_pipeline_connected_leads,
    )
except Exception:  # pragma: no cover - keep LinkedIn-only helpers usable
    count_outreach_log_connection_requests = None
    count_pipeline_connected_leads = None


def preflight_check(state: dict | None = None) -> dict[str, Any]:
    """Run pre-flight checks before any automation session.

    Returns dict with 'ok' boolean and details.
    """
    if state is None:
        state = load_state()

    results = {
        "ok": True,
        "checks": {},
    }

    # 1. Check Chrome CDP is responsive
    cdp = CDPConnection()
    health = cdp.health_check()
    results["checks"]["chrome_cdp"] = health
    if health["status"] != "ok":
        results["ok"] = False
        results["block_reason"] = f"Chrome CDP not responding on port {cdp.port}"
        return results

    # 2. Connect and check LinkedIn session
    try:
        cdp.connect()
        # Preflight must be quick and bounded.  A Chrome page-load event is
        # not reliable enough to gate the entire activity workflow: when it
        # never arrives, the old watcher could sit here for hours before it
        # ever opened a target profile.
        inject_stealth(cdp)
        page = detect_page(cdp, timeout=10)

        # CDP being reachable is not proof that LinkedIn is available. A newly
        # launched automation profile opens on Chrome's New Tab page, which
        # used to pass preflight and leave the first real workflow navigation
        # as the point of failure. Open LinkedIn deliberately and verify the
        # resulting page before allowing any workflow to proceed.
        # A profile reader can legitimately leave the active tab on LinkedIn's
        # 404 page after quarantining an invalid profile.  That is target-level
        # state, not evidence that the signed-in account or CDP lane is unsafe.
        # Re-establish a neutral LinkedIn page before applying the account-level
        # danger checks.  Login, checkpoint, captcha and restriction pages must
        # still block immediately and are intentionally not navigated away from.
        if page.get("page_type") in {"unknown", "not_found"}:
            cdp.navigate("https://www.linkedin.com/feed/", wait_load=False, timeout=8)
            time.sleep(2)
            page = detect_page(cdp, timeout=10)
        results["checks"]["linkedin_session"] = page

        if page.get("is_danger"):
            results["ok"] = False
            results["block_reason"] = f"Danger detected: {page.get('danger_type')}"
            cdp.disconnect()
            return results

        if page["page_type"] == "login":
            results["ok"] = False
            results["block_reason"] = "LinkedIn is logged out — manual login required"
            cdp.disconnect()
            return results

        if page.get("page_type") == "unknown":
            results["ok"] = False
            results["block_reason"] = (
                "LinkedIn session could not be verified after opening LinkedIn"
            )
            cdp.disconnect()
            return results

        cdp.disconnect()
    except Exception as e:
        results["ok"] = False
        results["block_reason"] = f"CDP connection failed: {e}"
        return results

    # 3. Check daily quotas
    conn_req_today = get_counter(state, "conn_req_sent")
    conn_req_week = get_weekly_counter(state, "conn_req_sent")
    profile_views_today = get_counter(state, "profile_views")

    results["checks"]["quotas"] = {
        "conn_req_today": conn_req_today,
        "conn_req_limit": MAX_CONN_REQ_PER_DAY,
        "conn_req_week": conn_req_week,
        "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
        "profile_views_today": profile_views_today,
        "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
    }

    if conn_req_today >= MAX_CONN_REQ_PER_DAY:
        results["checks"]["quotas"]["conn_req_exhausted"] = True
    if conn_req_week >= MAX_CONN_REQ_PER_WEEK:
        results["checks"]["quotas"]["conn_req_week_exhausted"] = True
        results["ok"] = False
        results["block_reason"] = "Weekly connection request limit reached"
    if profile_views_today >= MAX_PROFILE_VIEWS_PER_DAY:
        results["checks"]["quotas"]["profile_views_exhausted"] = True

    # 4. Check acceptance rate from sheet sources of truth.
    sheet_sent = {"ok": False, "source": "op_bruteforce_outreach_log"}
    if count_outreach_log_connection_requests is None:
        sheet_sent["error"] = "Outreach Log sent helper unavailable"
    else:
        try:
            sheet_sent = count_outreach_log_connection_requests()
        except Exception as e:
            sheet_sent["error"] = str(e)

    if not sheet_sent.get("ok"):
        results["ok"] = False
        results["block_reason"] = (
            "Unable to verify sent connection requests from Op Bruteforce Outreach Log: "
            f"{sheet_sent.get('error', 'unknown error')}"
        )
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": "op_bruteforce_pipeline",
            "sent": None,
            "accepted": None,
            "error": sheet_sent.get("error", "unknown error"),
        }
        return results

    sent_count = int(sheet_sent.get("sent", 0) or 0)
    if sent_count >= 10:  # Only check with sufficient sample
        pipeline_acceptance = {"ok": False, "source": "op_bruteforce_pipeline"}
        if count_pipeline_connected_leads is None:
            pipeline_acceptance["error"] = "Pipeline acceptance helper unavailable"
        else:
            try:
                pipeline_acceptance = count_pipeline_connected_leads()
            except Exception as e:
                pipeline_acceptance["error"] = str(e)

        if not pipeline_acceptance.get("ok"):
            results["ok"] = False
            results["block_reason"] = (
                "Unable to verify acceptance rate from Op Bruteforce Pipeline: "
                f"{pipeline_acceptance.get('error', 'unknown error')}"
            )
            results["checks"]["acceptance_rate"] = {
                "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
                "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
                "sent": sent_count,
                "accepted": None,
                "error": pipeline_acceptance.get("error", "unknown error"),
            }
            return results

        pipeline_connected = int(pipeline_acceptance.get("connected", 0) or 0)
        accepted_count = min(pipeline_connected, sent_count)
        acceptance_rate = accepted_count / sent_count
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
            "rate": round(acceptance_rate, 3),
            "sent": sent_count,
            "accepted": accepted_count,
            "outreach_log_sent": sent_count,
            "outreach_log_sent_rows": sheet_sent.get("sent_rows"),
            "pipeline_connected": pipeline_connected,
            "pipeline_connected_rows": pipeline_acceptance.get("connected_rows"),
        }
        if acceptance_rate < ACCEPTANCE_RATE_CRITICAL:
            results["ok"] = False
            results["block_reason"] = (
                f"Acceptance rate critically low ({acceptance_rate:.1%}). "
                "Connection requests paused until manual override."
            )
        elif acceptance_rate < ACCEPTANCE_RATE_WARN:
            results["checks"]["acceptance_rate"]["warning"] = True

    return results
