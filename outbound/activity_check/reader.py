"""Activity reader (live CDP reader + fixture/synthetic readers).

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S9). Pure move.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from outbound.activity_check.analysis import activity_evidence, activity_level, activity_score
from outbound.activity_check.danger import hard_read_failure_reason
from outbound.activity_check.text import (
    canonical_linkedin_profile_url,
    clean_text,
    navigation_type_key,
    normalized_profile_key,
)
from outbound.shared.session.manager import LinkedInSession


class LiveActivityReader:
    def __init__(self, timeout: float, retries: int):
        self.timeout = timeout
        self.retries = retries
        self.session = LinkedInSession()
        self.connected = False
        self.force_selector_on_next_call = False
        self.last_recovery: dict[str, Any] = {}

    def connect(self) -> dict[str, Any]:
        if self.connected:
            return {"ok": True, "status": "already_connected"}
        result = self.session.connect(skip_rate_check=True)
        if not result.get("ok"):
            return result
        self.connected = True
        return result

    def close(self) -> None:
        if self.connected:
            self.session.disconnect()
            self.connected = False

    def recover_connection(self) -> dict[str, Any]:
        """Discard a frozen page target and reconnect through a clean tab."""
        cdp = self.session.cdp
        health = cdp.health_check()
        if health.get("status") == "ok":
            try:
                replacement = cdp.replace_page_target("about:blank")
            except Exception as exc:
                self.connected = False
                return {
                    "ok": False,
                    "action": "fresh_tab_replacement_failed",
                    "cdp_health": health,
                    "error": str(exc),
                }
            self.connected = False
            self.session = LinkedInSession()
            result = self.connect()
            self.force_selector_on_next_call = bool(result.get("ok"))
            self.last_recovery = {
                "ok": bool(result.get("ok")),
                "action": "frozen_tab_replaced",
                "cdp_health": health,
                "replacement": replacement,
                "preflight": result,
                "next_navigation_type": "selector_based",
            }
            return self.last_recovery

        # A worker must not restart Chrome itself.  The lane coordinator owns
        # handoffs between profiles and decides when a resting profile returns.
        return {"ok": False, "action": "cdp_unhealthy", "cdp_health": health}

    def __call__(self, profile_url: str, plan_item: dict[str, Any] | None = None) -> dict[str, Any]:
        original_profile_url = clean_text(profile_url)
        profile_url = canonical_linkedin_profile_url(profile_url)
        if not profile_url:
            return {
                "error": True,
                "danger": "invalid_profile_url",
                "reason": "invalid_profile_url",
                "original_profile_url": original_profile_url,
            }
        attempts: list[dict[str, Any]] = []
        best_detail: dict[str, Any] = {}
        best_score = -2
        forced_selector = self.force_selector_on_next_call
        self.force_selector_on_next_call = False
        navigation_type = (
            "selector_based"
            if forced_selector
            else navigation_type_key((plan_item or {}).get("navigation_type"))
        )
        connect_result = self.connect()
        if not connect_result.get("ok"):
            return {
                "error": True,
                "danger": connect_result.get("block_reason", "LinkedIn preflight failed"),
            }
        for _ in range(max(0, self.retries) + 1):
            detail = self.session.read_activity_detail(
                profile_url, max_seconds=self.timeout, navigation_type=navigation_type
            )
            detail = {
                **(detail if isinstance(detail, dict) else {}),
                "original_profile_url": original_profile_url,
                "canonical_profile_url": profile_url,
                "profile_url_normalized": profile_url != original_profile_url.rstrip("/"),
                "navigation_type_used": navigation_type,
                "fresh_tab_recovery": forced_selector,
            }
            post_read_danger = self.session._check_danger()
            if post_read_danger and not activity_level(detail):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": True,
                    "danger": post_read_danger,
                    "reason": post_read_danger,
                }
            if navigation_type == "direct_url" and self._should_try_selector_fallback(detail):
                selector_detail = self.session.read_activity_detail(
                    profile_url,
                    max_seconds=self.timeout,
                    navigation_type="selector_based",
                )
                selector_detail = {
                    **(selector_detail if isinstance(selector_detail, dict) else {}),
                    "fallback_from_navigation_type": "direct_url",
                    "fallback_navigation_type": "selector_based",
                    "fallback_trigger": clean_text(detail.get("reason"))
                    or clean_text(detail.get("danger")),
                    "direct_url_attempt": activity_evidence(profile_url, detail),
                }
                if activity_level(selector_detail) or not hard_read_failure_reason(selector_detail):
                    detail = selector_detail
                else:
                    detail = {
                        **detail,
                        "selector_fallback_attempt": activity_evidence(
                            profile_url, selector_detail
                        ),
                    }
            try:
                page_state = self.session.cdp and self.session.cdp.evaluate(
                    "JSON.stringify({url: window.location.href, text: (document.body && document.body.innerText || '').slice(0, 500)})"
                )
                page_state = json.loads(page_state) if page_state else {}
            except Exception:
                page_state = {}
            page_url = clean_text(page_state.get("url"))
            page_text_raw = clean_text(page_state.get("text"))
            page_text = page_text_raw.lower()
            if not activity_level(detail) and (
                "/404" in page_url
                or "this page doesn't exist" in page_text
                or "this page doesn’t exist" in page_text
            ):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": True,
                    "danger": "invalid_profile_or_404",
                    "reason": "invalid_profile_or_404",
                    "page_url": page_url,
                }
            elif (
                not activity_level(detail)
                and "/recent-activity/" in page_url
                and len(page_text_raw) < 20
            ):
                detail = {
                    **(detail if isinstance(detail, dict) else {}),
                    "error": False,
                    "danger": "",
                    "reason": "activity_blank_page_not_ready",
                    "page_url": page_url,
                    "page_text_length": len(page_text_raw),
                    "deferred_empty_extract": True,
                }
            attempts.append(detail)
            level = activity_level(detail)
            score = activity_score(level) if level else -1
            if score > best_score:
                best_detail = detail
                best_score = score
            if score > 0:
                break
        if best_detail:
            best_detail["_attempts"] = [activity_evidence(profile_url, item) for item in attempts]
        return best_detail

    @staticmethod
    def _should_try_selector_fallback(detail: dict[str, Any]) -> bool:
        if not isinstance(detail, dict) or activity_level(detail):
            return False
        reason = clean_text(detail.get("reason"))
        danger = clean_text(detail.get("danger"))
        if reason == "activity_blank_page_not_ready":
            return True
        if danger == "activity_read_timeout" and reason in {
            "navigation_failed_or_timed_out",
            "activity_page_not_ready",
            "activity_feed_not_hydrated",
            "activity_extract_failed_or_timed_out",
            "insufficient_budget_before_tab",
        }:
            return True
        return False


def build_fixture_reader(fixture_path: str) -> Callable[..., dict[str, Any]]:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8")) if fixture_path else {}
    normalized = {normalized_profile_key(key): value for key, value in fixture.items()}
    return lambda profile_url, _plan_item=None: normalized.get(
        normalized_profile_key(profile_url), {}
    )


def build_synthetic_activity_reader() -> Callable[..., dict[str, Any]]:
    """Deterministic test-only activity evidence, varied by profile URL."""

    def reader(profile_url: str, _plan_item: dict[str, Any] | None = None) -> dict[str, Any]:
        bucket = sum(ord(char) for char in normalized_profile_key(profile_url)) % 3

        def entry(time_text):
            return {
                "time_text": time_text,
                "card_text_sample": "Synthetic test activity",
            }

        if bucket == 0:
            return {
                "tabs": {
                    "posts": {"activities": [entry("1d")], "total_visible": 1},
                    "comments": {"activities": []},
                    "reactions": {"activities": []},
                }
            }
        if bucket == 1:
            return {
                "tabs": {
                    "posts": {"activities": []},
                    "comments": {"activities": [entry("10d")] * 5, "total_visible": 5},
                    "reactions": {"activities": []},
                }
            }
        return {
            "tabs": {
                "posts": {"activities": []},
                "comments": {"activities": [entry("180d")], "total_visible": 1},
                "reactions": {"activities": []},
            }
        }

    return reader
