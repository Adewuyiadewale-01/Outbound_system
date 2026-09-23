"""Failure guards, UI preflight, and session health for outreach.

Four guards watch the result dict for failure patterns — send-error streaks,
browser-error streaks, sampled profile readiness, and CDP health — tripping
before damage compounds. Every guard resets on success; recovery is bounded.
"""

from __future__ import annotations

import argparse
import random
import re
import time
from datetime import datetime
from typing import Any

from outbound.outreach.policy import (
    CDP_HEALTH_EVAL_TIMEOUT_SEC,
    EMAIL_REQUIRED_TO_CONNECT,
    FAILURE_BACKOFF_MAX_SEC,
    FAILURE_BACKOFF_MIN_SEC,
    MAX_BROWSER_RECOVERY_ATTEMPTS,
    MAX_CONSECUTIVE_SEND_FAILURES,
    MAX_SAME_BROWSER_ERROR_STREAK,
    MAX_SAME_SEND_ERROR_STREAK,
    PROFILE_UI_GUARD_ERRORS,
)


def _normalize_failure_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "unknown_send_failure"
    if "| diagnostics=" in raw:
        raw = raw.split("| diagnostics=", 1)[0].strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw[:180]


def _register_send_failure(
    result: dict[str, Any], prospect_id: Any, error_value: Any
) -> str | None:
    guard = result.setdefault("failure_guard", {})
    if not guard.get("enabled", False):
        return None

    error_key = _normalize_failure_key(error_value)
    last_error = str(guard.get("last_error") or "")
    if error_key == last_error:
        guard["same_error_streak"] = int(guard.get("same_error_streak", 0)) + 1
    else:
        guard["same_error_streak"] = 1
        guard["last_error"] = error_key

    guard["consecutive_send_failures"] = int(guard.get("consecutive_send_failures", 0)) + 1

    recent = guard.setdefault("recent_failures", [])
    if isinstance(recent, list):
        recent.append(
            {
                "prospect_id": str(prospect_id or "").strip(),
                "error": error_key,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(recent) > 10:
            del recent[:-10]

    if int(guard.get("same_error_streak", 0)) >= int(
        guard.get("max_same_error_streak", MAX_SAME_SEND_ERROR_STREAK)
    ):
        reason = (
            f"Failure guard tripped: same send error repeated "
            f"{guard['same_error_streak']}x ({error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    if int(guard.get("consecutive_send_failures", 0)) >= int(
        guard.get("max_consecutive_send_failures", MAX_CONSECUTIVE_SEND_FAILURES)
    ):
        reason = (
            f"Failure guard tripped: {guard['consecutive_send_failures']} consecutive send failures "
            f"(last={error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason

    return None


def _register_browser_failure(
    result: dict[str, Any],
    prospect_id: Any,
    error_value: Any,
    step: str,
) -> str | None:
    guard = result.setdefault(
        "browser_guard",
        {
            "enabled": True,
            "max_same_error_streak": MAX_SAME_BROWSER_ERROR_STREAK,
            "same_error_streak": 0,
            "last_error": None,
            "tripped": False,
            "trip_reason": None,
            "recent_failures": [],
        },
    )
    if not guard.get("enabled", False):
        return None

    error_key = _normalize_failure_key(error_value)
    if error_key == str(guard.get("last_error") or ""):
        guard["same_error_streak"] = int(guard.get("same_error_streak", 0)) + 1
    else:
        guard["same_error_streak"] = 1
        guard["last_error"] = error_key

    recent = guard.setdefault("recent_failures", [])
    if isinstance(recent, list):
        recent.append(
            {
                "prospect_id": str(prospect_id or "").strip(),
                "step": step,
                "error": error_key,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(recent) > 10:
            del recent[:-10]

    if int(guard.get("same_error_streak", 0)) >= int(
        guard.get("max_same_error_streak", MAX_SAME_BROWSER_ERROR_STREAK)
    ):
        reason = (
            f"Browser guard tripped: same browser/CDP error repeated "
            f"{guard['same_error_streak']}x during {step} ({error_key})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    return None


def _reset_browser_failure_guard(result: dict[str, Any]) -> None:
    guard = result.get("browser_guard")
    if not isinstance(guard, dict):
        return
    guard["same_error_streak"] = 0
    guard["last_error"] = None


def _outreach_ui_sample_size(selected_count: int) -> int:
    if selected_count <= 0:
        return 0
    rounded_ten_percent = int(((selected_count * 10) + 50) // 100)
    return min(selected_count, max(3, rounded_ten_percent))


def _is_profile_ui_failure(live_state: str, error_value: Any) -> bool:
    if str(live_state or "").strip().lower() != "unknown":
        return False
    error_key = _normalize_failure_key(error_value)
    return (
        error_key in PROFILE_UI_GUARD_ERRORS
        or "selector" in error_key
        or "topcard" in error_key
        or "modal_not_ready" in error_key
    )


def _record_early_profile_guard(
    result: dict[str, Any],
    prospect_id: Any,
    live_state: str,
    profile_state: dict[str, Any],
    selected_count: int,
) -> str | None:
    sample_size = _outreach_ui_sample_size(selected_count)
    guard = result.setdefault(
        "early_profile_guard",
        {
            "enabled": True,
            "sample_size": sample_size,
            "inspected": 0,
            "failures": [],
            "non_failures": 0,
            "tripped": False,
            "trip_reason": None,
        },
    )
    if not guard.get("enabled", False) or guard.get("tripped") or sample_size <= 0:
        return None
    if int(guard.get("inspected", 0)) >= sample_size:
        return None

    guard["sample_size"] = sample_size
    guard["inspected"] = int(guard.get("inspected", 0)) + 1
    error_key = _normalize_failure_key(profile_state.get("error"))
    if _is_profile_ui_failure(live_state, profile_state.get("error")):
        failures = guard.setdefault("failures", [])
        if isinstance(failures, list):
            failures.append(
                {
                    "prospect_id": str(prospect_id or "").strip(),
                    "state": str(live_state or "").strip(),
                    "error": error_key,
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
    else:
        guard["non_failures"] = int(guard.get("non_failures", 0)) + 1

    failures_count = (
        len(guard.get("failures", [])) if isinstance(guard.get("failures"), list) else 0
    )
    inspected = int(guard.get("inspected", 0))
    if inspected >= sample_size and failures_count == inspected:
        first_error = error_key
        failures = guard.get("failures", [])
        if isinstance(failures, list) and failures:
            first_error = str(failures[0].get("error") or first_error)
        reason = (
            f"Early profile UI guard tripped: first {sample_size} inspected leads "
            f"all failed profile readiness ({first_error})"
        )
        guard["tripped"] = True
        guard["trip_reason"] = reason
        return reason
    return None


def _run_outreach_ui_preflight(
    *,
    session: Any,
    selected: list[dict[str, Any]],
    check_connect_modal: bool = True,
) -> dict[str, Any]:
    sample_size = _outreach_ui_sample_size(len(selected))
    payload: dict[str, Any] = {
        "ok": True,
        "sample_size": sample_size,
        "inspected": [],
        "connectable_checked": False,
        "selectors": {
            "profile_topcard": '[componentkey*="profile.card"][componentkey*="Topcard"]',
            "more_connect": 'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]',
            "send_without_note": 'button[aria-label="Send without a note"]',
            "shadow_dom_modal": True,
        },
    }
    if sample_size <= 0:
        payload["ok"] = False
        payload["blocker"] = "No selected leads available for UI preflight"
        return payload

    profile_readiness_failures: list[dict[str, Any]] = []
    profile_readiness_non_failures = 0
    profile_readiness_candidates = 0

    for index, prospect in enumerate(selected[:sample_size]):
        profile_url = str(prospect.get("contact_linkedin", "") or "").strip()
        item: dict[str, Any] = {
            "index": index,
            "prospect_id": prospect.get("id"),
            "profile_url": profile_url,
        }
        payload["inspected"].append(item)
        if not profile_url:
            item["state"] = "missing_linkedin_url"
            continue

        profile_readiness_candidates += 1
        try:
            profile_state = session.inspect_profile_action_state(profile_url)
        except Exception as exc:
            item["state"] = "exception"
            item["error"] = _normalize_failure_key(exc)
            payload["ok"] = False
            payload["blocker"] = f"UI preflight profile inspection exception: {item['error']}"
            return payload

        live_state = str(profile_state.get("state", "unknown") or "unknown")
        item["state"] = live_state
        item["error"] = profile_state.get("error")
        item["more_button_seen"] = profile_state.get("more_button_seen")
        item["debug_dump"] = profile_state.get("debug_dump")

        if _is_profile_ui_failure(live_state, profile_state.get("error")):
            failure = {
                "prospect_id": prospect.get("id"),
                "profile_url": profile_url,
                "error": _normalize_failure_key(profile_state.get("error")),
            }
            profile_readiness_failures.append(failure)
            payload.setdefault("profile_readiness_failures", []).append(failure)
            continue

        profile_readiness_non_failures += 1

        if profile_readiness_failures:
            payload["warning"] = (
                "One or more sampled leads failed profile readiness, but another sampled "
                "profile loaded successfully; continuing preflight"
            )

        if (
            live_state in {"connect_direct", "connect_in_more"}
            and not payload["connectable_checked"]
        ):
            if not check_connect_modal:
                payload["connectable_checked"] = True
                payload["modal_check_deferred"] = True
                continue
            try:
                no_note_check = session.verify_no_note_send_ui(profile_url, profile_state)
            except Exception as exc:
                no_note_check = {"ok": False, "error": _normalize_failure_key(exc)}
            item["no_note_modal_check"] = no_note_check
            if not no_note_check.get("ok"):
                if _normalize_failure_key(no_note_check.get("error")) == EMAIL_REQUIRED_TO_CONNECT:
                    item["soft_skip"] = EMAIL_REQUIRED_TO_CONNECT
                    payload.setdefault("soft_skips", []).append(
                        {
                            "prospect_id": prospect.get("id"),
                            "profile_url": profile_url,
                            "reason": EMAIL_REQUIRED_TO_CONNECT,
                        }
                    )
                    payload["warning"] = (
                        "At least one sampled lead requires email to connect; lead will be reported and skipped"
                    )
                    continue
                payload["ok"] = False
                payload["blocker"] = (
                    f"UI preflight failed no-note invite modal check for prospect {prospect.get('id')}: "
                    f"{_normalize_failure_key(no_note_check.get('error'))}"
                )
                return payload
            payload["connectable_checked"] = True

    if (
        profile_readiness_candidates > 0
        and profile_readiness_failures
        and profile_readiness_non_failures == 0
    ):
        first_failure = profile_readiness_failures[0]
        payload["ok"] = False
        payload["blocker"] = (
            "UI preflight failed on profile readiness for all "
            f"{profile_readiness_candidates} sampled prospects; first failure "
            f"{first_failure.get('prospect_id')}: {first_failure.get('error')}"
        )
        return payload

    if not payload["connectable_checked"]:
        if payload.get("soft_skips"):
            payload["warning"] = (
                "Only email-required connectable leads were found in UI preflight sample"
            )
        else:
            payload["warning"] = (
                "No connectable lead found in UI preflight sample; send modal was not opened"
            )
    return payload


def _run_connection_modal_checks(*, session: Any, selected: list[dict[str, Any]]) -> dict[str, Any]:
    """Run fail-closed no-send checks and stop at the first unsafe result."""
    checks: list[dict[str, Any]] = []
    for prospect in selected:
        profile_url = str(prospect.get("contact_linkedin", "") or "").strip()
        profile_state: dict[str, Any] = {}
        try:
            profile_state = session.inspect_profile_action_state(profile_url)
            modal = session.verify_no_note_send_ui(profile_url, profile_state)
        except Exception as exc:
            modal = {"ok": False, "closed": False, "error": _normalize_failure_key(exc)}
        check = {
            "prospect_id": prospect.get("id"),
            "company": prospect.get("company"),
            "profile_state": profile_state,
            "modal": modal,
        }
        checks.append(check)
        if not modal.get("ok"):
            return {
                "ok": False,
                "checks": checks,
                "error": (
                    f"No-send modal check failed for prospect {prospect.get('id')}: "
                    f"{_normalize_failure_key(modal.get('error'))}"
                ),
            }
    return {"ok": True, "checks": checks}


def _session_cdp_health_check(session: Any, label: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": label,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "ok": False,
    }
    try:
        cdp = getattr(session, "cdp", None)
        if cdp is None:
            payload["error"] = "missing_cdp"
            return payload
        http_health = cdp.health_check()
        payload["http"] = http_health
        if http_health.get("status") != "ok":
            payload["error"] = http_health.get("error", "cdp_http_unhealthy")
            return payload
        eval_result = cdp.evaluate(
            "(() => ({readyState: document.readyState, href: window.location.href, ok: true}))()",
            timeout=CDP_HEALTH_EVAL_TIMEOUT_SEC,
        )
        payload["eval"] = eval_result
        payload["ok"] = True
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        return payload


def _ensure_session_healthy(
    *,
    result: dict[str, Any],
    session: Any,
    label: str,
    args: argparse.Namespace,
    allow_reconnect: bool = True,
) -> bool:
    health = _session_cdp_health_check(session, label)
    result.setdefault("browser_health", []).append(health)
    if health.get("ok"):
        _reset_browser_failure_guard(result)
        return True
    if not allow_reconnect:
        return False

    recoveries = result.setdefault("browser_recoveries", [])
    if len(recoveries) >= MAX_BROWSER_RECOVERY_ATTEMPTS:
        result["ok"] = False
        result["status"] = "blocked_browser_health"
        result["blockers"].append(
            f"Chrome/CDP unhealthy during {label}; recovery limit reached: {health.get('error', 'unknown')}"
        )
        return False

    recovery: dict[str, Any] = {
        "label": label,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "initial_error": health.get("error", "unknown"),
    }
    recoveries.append(recovery)
    try:
        try:
            session.disconnect()
        except Exception as exc:
            recovery["disconnect_error"] = str(exc)
        connect_result = session.connect(
            skip_rate_check=getattr(args, "skip_acceptance_rate_check", False)
        )
        recovery["connect_result"] = connect_result
        if not connect_result.get("ok"):
            raise RuntimeError(connect_result.get("block_reason", "LinkedIn reconnect failed"))
        post = _session_cdp_health_check(session, f"{label}_post_reconnect")
        result.setdefault("browser_health", []).append(post)
        recovery["post_health"] = post
        if not post.get("ok"):
            raise RuntimeError(post.get("error", "post_reconnect_health_failed"))
        recovery["ok"] = True
        _reset_browser_failure_guard(result)
        return True
    except Exception as exc:
        recovery["ok"] = False
        recovery["error"] = str(exc)
        result["ok"] = False
        result["status"] = "blocked_browser_health"
        result["blockers"].append(f"Chrome/CDP recovery failed during {label}: {exc}")
        return False


def _reset_send_failure_guard(result: dict[str, Any]) -> None:
    guard = result.get("failure_guard")
    if not isinstance(guard, dict):
        return
    guard["consecutive_send_failures"] = 0
    guard["same_error_streak"] = 0
    guard["last_error"] = None


def _sleep_failure_backoff(result: dict[str, Any], is_last: bool) -> None:
    if is_last:
        return
    guard = result.get("failure_guard")
    if not isinstance(guard, dict) or not guard.get("enabled", False):
        return
    min_sec = int(guard.get("backoff_min_sec", FAILURE_BACKOFF_MIN_SEC) or FAILURE_BACKOFF_MIN_SEC)
    max_sec = int(guard.get("backoff_max_sec", FAILURE_BACKOFF_MAX_SEC) or FAILURE_BACKOFF_MAX_SEC)
    if max_sec < min_sec:
        max_sec = min_sec
    seconds = random.randint(min_sec, max_sec)
    time.sleep(seconds)
    guard["backoff_count"] = int(guard.get("backoff_count", 0)) + 1
    guard["backoff_total_seconds"] = int(guard.get("backoff_total_seconds", 0)) + seconds
