#!/usr/bin/env python3
"""Resumable LinkedIn source-post engagement workflow.

The default `run` is a read-only dry run. Real Likes, Follows and connection
requests require the explicit `--execute` switch.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import inspect
import json
import os
import random
import re
import sys
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "helpers"))
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_environment import load_repo_env
from outbound.shared.state import daily_rng, read_json, write_json
from outbound.engagement.parsing import canonical_profile_url, parse_relative_age_hours, parse_follower_count, classify_location

load_repo_env()

STATE_DIR = ROOT / "state" / "post_engagement"
CAMPAIGNS_DIR = STATE_DIR / "campaigns"
CONFIG_PATH = STATE_DIR / "config.json"
HIGH_SIGNAL_PATH = STATE_DIR / "high_signal_follows.json"
HISTORY_PATH = STATE_DIR / "history.jsonl"
CONTROL_PATH = STATE_DIR / "control.json"
ARCHIVE_DIR = STATE_DIR / "archive"
LEDGER_PATH = STATE_DIR / "daily_action_ledger.json"
PENDING_ACTIONS_PATH = STATE_DIR / "pending_final_actions.json"
# GEO_PATH = ROOT / "config" / "post_engagement_geography.json"
TZ = ZoneInfo("Africa/Lagos")
ACTION_ACCOUNT = ContextVar("post_engagement_account", default=None)
RUNNER_LOCK_PATH = STATE_DIR / "runner.lock"


def action_account() -> str:
    return ACTION_ACCOUNT.get() or load_config()["cdp_account"]


@contextmanager
def campaign_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with RUNNER_LOCK_PATH.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Post Engagement is already running. Pause it before changing the campaign."
            )
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def exclusive_campaign(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with campaign_lock():
            params = inspect.signature(function).bind_partial(*args, **kwargs).arguments
            day = params.get("day") or now().date().isoformat()
            saved = read_json(campaign_path(day), {})
            account = (
                params.get("cdp_account")
                or saved.get("cdp_account")
                or load_config()["cdp_account"]
            )
            token = ACTION_ACCOUNT.set(account)
            try:
                return function(*args, **kwargs)
            finally:
                ACTION_ACCOUNT.reset(token)

    return wrapped


def runner_active() -> bool:
    try:
        with campaign_lock():
            return False
    except RuntimeError:
        return True


def execution_event(campaign, candidate=None, *, action, method=None, reason=None):
    previous = campaign.get("execution", {})
    changed = candidate is not None and previous.get("profile_url") != candidate.get("profile_url")
    campaign["execution"] = {
        "profile_name": (candidate or {}).get("name")
        or ("LinkedIn member" if candidate else previous.get("profile_name", "")),
        "profile_url": (candidate or {}).get("profile_url", previous.get("profile_url", "")),
        "action": action,
        "navigation_method": method
        if method is not None
        else ("" if changed else previous.get("navigation_method", "")),
        "fallback_reason": reason
        if reason is not None
        else ("" if changed else previous.get("fallback_reason", "")),
        "updated_at": now().isoformat(),
    }
    save_campaign(campaign)


DEFAULT_CONFIG = {
    "enabled": False,
    "cdp_account": "design",
    "engagement_min": 45,
    "engagement_max": 60,
    "connection_target": 10,
    "follow_target": 10,
    "likes_min": 1,
    "likes_max": 3,
    "like_weights": [50, 35, 15],
    "newest_post_max_hours": 48,
    "activity_window_days": 5,
    "reaction_threshold": 5,
    "comment_threshold": 3,
    "post_threshold": 3,
    "follower_connection_limit": 5000,
    "reactor_min_coverage": 0.9,
    "source_collection_max_attempts": 3,
    "max_attempts": 4,
    "source_max_age_days": 5,
    "action_delay_min_seconds": 15,
    "action_delay_max_seconds": 28,
    "final_action_delay_min_seconds": 20,
    "final_action_delay_max_seconds": 45,
    # The live campaign is intentionally spread across the day.  These are
    # local runtime controls: no campaign state or humanisation data is stored
    # in Google Sheets.
    "engagement_batch_count": 3,
    "inter_batch_delay_minutes": 90,
    "obf_style_diversions": True,
}

PROFILE_PARSER_VERSION = 4
ACTIVITY_ASSESSMENT_VERSION = 1

CDP_ACCOUNTS = {
    "design": {"host": "127.0.0.1", "port": 18800},
    "automation": {"host": "127.0.0.1", "port": 18801},
}


def now() -> datetime:
    return datetime.now(TZ)


def append_history(value: dict[str, Any]) -> None:
    path = (
        STATE_DIR / "navigation.jsonl"
        if value.get("type") in {"navigation", "navigation_failed"}
        else HISTORY_PATH
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_daily_ledger() -> dict[str, Any]:
    value = read_json(LEDGER_PATH, {"events": []})
    return (
        value
        if isinstance(value, dict) and isinstance(value.get("events"), list)
        else {"events": []}
    )


def ledger_has(day: str, action: str, profile_url: str, post_urn: str = "") -> bool:
    return any(
        event.get("day") == day
        and event.get("account") == action_account()
        and event.get("action") == action
        and event.get("profile_url") == profile_url
        and event.get("post_urn", "") == post_urn
        for event in read_daily_ledger()["events"]
    )


def record_ledger_action(
    day: str, action: str, profile_url: str, *, post_urn: str = "", campaign_id: str = ""
) -> None:
    if ledger_has(day, action, profile_url, post_urn):
        return
    ledger = read_daily_ledger()
    ledger["events"].append(
        {
            "day": day,
            "action": action,
            "profile_url": profile_url,
            "account": action_account(),
            "post_urn": post_urn,
            "campaign_id": campaign_id,
            "recorded_at": now().isoformat(),
        }
    )
    write_json(LEDGER_PATH, ledger)


def ledger_count(day: str, action: str) -> int:
    return sum(
        1
        for event in read_daily_ledger()["events"]
        if event.get("day") == day
        and event.get("action") == action
        and event.get("account") == action_account()
    )


def read_pending_actions() -> dict[str, Any]:
    value = read_json(PENDING_ACTIONS_PATH, {"actions": []})
    return (
        value
        if isinstance(value, dict) and isinstance(value.get("actions"), list)
        else {"actions": []}
    )


def defer_final_action(candidate: dict[str, Any], action: str, campaign: dict[str, Any]) -> None:
    pending = read_pending_actions()
    profile_url = str(candidate.get("profile_url") or "")
    if not profile_url or any(
        item.get("action") == action
        and item.get("profile_url") == profile_url
        and item.get("account") == action_account()
        for item in pending["actions"]
    ):
        return
    payload = {
        key: candidate.get(key)
        for key in (
            "profile_url",
            "name",
            "location",
            "follower_count",
            "geography_tier",
            "region",
            "activity_counts",
            "recommendation",
        )
    }
    payload.update(
        {
            "action": action,
            "account": action_account(),
            "deferred_from_day": campaign["day"],
            "deferred_at": now().isoformat(),
        }
    )
    pending["actions"].append(payload)
    write_json(PENDING_ACTIONS_PATH, pending)


def resolve_deferred_action(action: str, profile_url: str) -> None:
    pending = read_pending_actions()
    remaining = [
        item
        for item in pending["actions"]
        if not (
            item.get("action") == action
            and item.get("profile_url") == profile_url
            and item.get("account") == action_account()
        )
    ]
    if len(remaining) != len(pending["actions"]):
        pending["actions"] = remaining
        write_json(PENDING_ACTIONS_PATH, pending)


def load_config() -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **read_json(CONFIG_PATH, {})}


@exclusive_campaign
def save_config(requested: dict[str, Any]) -> dict[str, Any]:
    current = load_config()
    allowed = set(DEFAULT_CONFIG)
    current.update({key: value for key, value in requested.items() if key in allowed})
    if current["cdp_account"] not in CDP_ACCOUNTS:
        raise ValueError("cdp_account must be design or automation")
    if int(current["engagement_min"]) > int(current["engagement_max"]):
        raise ValueError("engagement_min cannot exceed engagement_max")
    if int(current["connection_target"]) < 0 or int(current["follow_target"]) < 0:
        raise ValueError("connection_target and follow_target cannot be negative")
    if float(current["final_action_delay_min_seconds"]) > float(
        current["final_action_delay_max_seconds"]
    ):
        raise ValueError(
            "final_action_delay_min_seconds cannot exceed final_action_delay_max_seconds"
        )
    if not 1 <= int(current["engagement_batch_count"]) <= 6:
        raise ValueError("engagement_batch_count must be between 1 and 6")
    if not 0 <= int(current["inter_batch_delay_minutes"]) <= 360:
        raise ValueError("inter_batch_delay_minutes must be between 0 and 360")
    write_json(CONFIG_PATH, current)
    return current


def validate_profile_gate(gate: dict[str, Any]) -> None:
    """Reject global LinkedIn UI labels before they can affect ranking."""
    name = str(gate.get("name") or "").strip()
    location = str(gate.get("location") or "").strip()
    followers = str(gate.get("follower_text") or "").strip()
    follower_source = str(gate.get("follower_source") or "").strip()
    error = str(gate.get("error") or "").strip()
    if error:
        raise RuntimeError(f"profile_gate_{error}")
    if not name or re.search(r"\bnotifications?\b", name, re.IGNORECASE):
        raise RuntimeError("profile_gate_invalid_name")
    if not location or location.casefold() == "contact info":
        raise RuntimeError("profile_gate_invalid_location")
    if not parse_follower_count(followers):
        raise RuntimeError("profile_gate_missing_followers")
    if follower_source not in {"header", "activity"}:
        raise RuntimeError("profile_gate_unbounded_follower_source")


def choose_like_target(day: str, profile_url: str, config: dict[str, Any]) -> int:
    choices = list(range(int(config["likes_min"]), int(config["likes_max"]) + 1))
    weights = list(config.get("like_weights") or [])[: len(choices)]
    if len(weights) != len(choices):
        weights = [1] * len(choices)
    return daily_rng(day, profile_url).choices(choices, weights=weights, k=1)[0]


def ensure_engagement_batches(
    campaign: dict[str, Any], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Persist a balanced, stable batch plan for the campaign's daily target."""
    existing = campaign.get("engagement_batches")
    if isinstance(existing, list) and existing:
        return existing
    target = max(0, int(campaign.get("target", 0)))
    count = max(1, min(int(config.get("engagement_batch_count", 3)), target or 1))
    base, remainder = divmod(target, count)
    sizes = [base + (1 if index < remainder else 0) for index in range(count)]
    # Keep the batches balanced, but avoid making the largest batch predictably
    # the first one every day.
    daily_rng(campaign["day"], "engagement-batch-order").shuffle(sizes)
    completed = max(0, int(campaign.get("engaged", 0)))
    batches: list[dict[str, Any]] = []
    for index, size in enumerate(sizes, start=1):
        used = min(completed, size)
        completed -= used
        batches.append(
            {
                "number": index,
                "target": size,
                "engaged": used,
                "status": "completed" if used >= size else "pending",
                "started_at": "",
                "completed_at": "",
            }
        )
    active = next((item for item in batches if item["status"] != "completed"), batches[-1])
    if active["status"] == "pending" and active["engaged"]:
        active["status"] = "running"
    campaign["engagement_batches"] = batches
    campaign["current_batch_number"] = active["number"]
    return batches


def current_engagement_batch(campaign: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    batches = ensure_engagement_batches(campaign, config)
    for batch in batches:
        if batch.get("status") != "completed":
            campaign["current_batch_number"] = int(batch["number"])
            return batch
    campaign["current_batch_number"] = int(batches[-1]["number"])
    return batches[-1]


def ensure_obf_diversions(campaign: dict[str, Any], config: dict[str, Any]) -> None:
    """Assign OBF's persisted diversion plan to this campaign's profiles."""
    if not bool(config.get("obf_style_diversions", True)):
        return
    candidates = [
        candidate for candidate in campaign.get("candidates", []) if candidate.get("profile_url")
    ]
    for slot_id, candidate in enumerate(candidates, start=1):
        candidate.setdefault("diversion_slot", slot_id)
    missing = [candidate for candidate in candidates if not candidate.get("obf_diversion")]
    if not missing:
        return
    from linkedin_outreach_session import (
        generate_lead_diversion_plan,
        generate_lead_diversion_seconds_plan,
    )

    slots = [int(candidate["diversion_slot"]) for candidate in candidates]
    kinds = {
        int(entry["slot_id"]): entry["lead_diversion"]
        for entry in generate_lead_diversion_plan(campaign["day"], slots)["entries"]
    }
    seconds = {
        int(entry["slot_id"]): entry.get("lead_diversion_sec")
        for entry in generate_lead_diversion_seconds_plan(
            campaign["day"],
            [{"slot_id": slot, "lead_diversion": kinds[slot]} for slot in slots],
        )["entries"]
    }
    for candidate in candidates:
        slot = int(candidate["diversion_slot"])
        candidate.update(
            obf_diversion=kinds[slot],
            obf_diversion_seconds=seconds[slot],
            diversion_plan_version="obf-v1",
        )


def run_obf_diversion(session: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Execute the same diversion primitives OBF uses, without changing outcome."""
    from linkedin_outreach_session import _run_diversion

    activity = {
        "recent_items": [
            {"post_url": post.get("post_url", "")} for post in candidate.get("posts", [])
        ]
    }
    result = _run_diversion(
        session,
        str(candidate.get("obf_diversion") or "none"),
        candidate.get("obf_diversion_seconds"),
        activity=activity,
        company_linkedin=str(candidate.get("company_linkedin") or ""),
    )
    candidate["diversion_result"] = result
    candidate["diversion_completed_at"] = now().isoformat()
    return result


def wait_for_next_batch(campaign: dict[str, Any]) -> bool:
    """Wait in short, pause-aware intervals; returns false when paused."""
    raw = str(campaign.get("next_batch_at") or "")
    try:
        deadline = datetime.fromisoformat(raw)
    except ValueError:
        return True
    while True:
        if campaign_control(campaign["day"]).get("action") == "pause":
            campaign.update(status="paused", stage="paused", paused_at=now().isoformat())
            save_campaign(campaign)
            return False
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            return True
        time.sleep(min(30, max(1, remaining)))


def campaign_path(day: str) -> Path:
    return CAMPAIGNS_DIR / f"{day}.json"


def new_campaign(day: str, config: dict[str, Any]) -> dict[str, Any]:
    likes_used = ledger_count(day, "engagement")
    connections_used = ledger_count(day, "connect")
    follows_used = ledger_count(day, "follow")
    # A new source campaign has its own full Like target. The ledger prevents
    # overlapping people, rather than shrinking the fresh campaign's pool.
    target = daily_rng(day, "daily-target").randint(
        int(config["engagement_min"]), int(config["engagement_max"])
    )
    deferred_candidates = []
    for deferred in read_pending_actions()["actions"]:
        if deferred.get("account") != action_account():
            continue
        candidate = {
            key: deferred.get(key)
            for key in (
                "profile_url",
                "name",
                "location",
                "follower_count",
                "geography_tier",
                "region",
                "activity_counts",
                "recommendation",
            )
        }
        candidate.update(
            status="deferred_final_action",
            activity_assessment_status="complete",
            deferred_from_day=deferred.get("deferred_from_day", ""),
        )
        deferred_candidates.append(candidate)
    return {
        "schema_version": 1,
        "day": day,
        "cdp_account": action_account(),
        "status": "waiting_for_source",
        "stage": "source_posts",
        "created_at": now().isoformat(),
        "updated_at": now().isoformat(),
        "target": target,
        "connection_target": int(config["connection_target"]),
        "follow_target": int(config["follow_target"]),
        "connection_send_capacity": max(0, int(config["connection_target"]) - connections_used),
        "follow_send_capacity": max(0, int(config["follow_target"]) - follows_used),
        "sources": [],
        "candidates": deferred_candidates,
        "engaged": 0,
        "connections_sent": 0,
        "followed": 0,
        "errors": [],
        "target_id": "",
        "dry_run": True,
        "engagement_batches": [],
        "current_batch_number": 0,
        "next_batch_at": "",
        "daily_quota_start": {
            "engagements_used": likes_used,
            "connections_used": connections_used,
            "follows_used": follows_used,
        },
    }


def load_campaign(day: str | None = None) -> dict[str, Any]:
    selected = day or now().date().isoformat()
    config = load_config()
    value = read_json(campaign_path(selected), None)
    if not isinstance(value, dict):
        return new_campaign(selected, config)
    value.setdefault("connection_target", int(config["connection_target"]))
    value.setdefault("follow_target", int(config["follow_target"]))
    value.setdefault("followed", 0)
    return value


def save_campaign(campaign: dict[str, Any]) -> None:
    campaign["updated_at"] = now().isoformat()
    write_json(campaign_path(campaign["day"]), campaign)


class PauseRequested(Exception):
    """Raised only at a durable workflow checkpoint."""


def campaign_control(day: str) -> dict[str, Any]:
    return read_json(CONTROL_PATH, {}).get(day, {})


def pause_campaign(day: str | None = None) -> dict[str, Any]:
    campaign = load_campaign(day)
    controls = read_json(CONTROL_PATH, {})
    controls[campaign["day"]] = {"action": "pause", "requested_at": now().isoformat()}
    write_json(CONTROL_PATH, controls)
    # The worker owns campaign writes while active; only signal via control.json.
    campaign.update(status="pause_requested", pause_requested_at=now().isoformat())
    if not runner_active():
        campaign.update(status="paused", stage="paused")
        save_campaign(campaign)
    return campaign


@exclusive_campaign
def resume_campaign(day: str | None = None) -> dict[str, Any]:
    campaign = load_campaign(day)
    controls = read_json(CONTROL_PATH, {})
    controls.pop(campaign["day"], None)
    write_json(CONTROL_PATH, controls)
    if str(campaign.get("status", "")).startswith("pause") or campaign.get("status") == "failed":
        campaign.update(status="ready", stage="resume_pending")
        save_campaign(campaign)
    return campaign


@exclusive_campaign
def archive_and_start_fresh(
    day: str | None = None, reason: str = "fresh_validation_run"
) -> dict[str, Any]:
    """Archive a stopped campaign, seed the day ledger, and create a fresh active run."""
    campaign = load_campaign(day)
    if campaign.get("status") == "running":
        raise RuntimeError("Pause the campaign before archiving it.")
    campaign_id = f"{campaign['day']}-{campaign.get('created_at', '')}"
    if not campaign.get("dry_run"):
        for candidate in campaign.get("candidates", []):
            profile_url = str(candidate.get("profile_url") or "")
            if not profile_url:
                continue
            if candidate.get("status") == "engaged" or int(candidate.get("likes_completed", 0)) > 0:
                record_ledger_action(
                    campaign["day"], "engagement", profile_url, campaign_id=campaign_id
                )
            result = candidate.get("connection_result")
            if isinstance(result, dict) and result.get("success"):
                record_ledger_action(
                    campaign["day"], "connect", profile_url, campaign_id=campaign_id
                )
            if candidate.get("follow_result") == "followed":
                record_ledger_action(
                    campaign["day"], "follow", profile_url, campaign_id=campaign_id
                )
    campaign.update(
        status="archived", stage="archived", archived_at=now().isoformat(), archive_reason=reason
    )
    archive_path = (
        ARCHIVE_DIR / f"{campaign['day']}-{now().strftime('%H%M%S')}-fresh-run" / "campaign.json"
    )
    write_json(archive_path, campaign)
    active_path = campaign_path(campaign["day"])
    if active_path.exists():
        active_path.unlink()
    controls = read_json(CONTROL_PATH, {})
    controls.pop(campaign["day"], None)
    write_json(CONTROL_PATH, controls)
    fresh = new_campaign(campaign["day"], load_config())
    save_campaign(fresh)
    return {"archived": campaign, "campaign": fresh, "daily_ledger": read_daily_ledger()}


def raise_if_paused(campaign: dict[str, Any]) -> None:
    if campaign_control(campaign["day"]).get("action") == "pause":
        raise PauseRequested()


@exclusive_campaign
def add_source(url: str, day: str | None = None, cdp_account: str | None = None) -> dict[str, Any]:
    if not re.match(r"^https?://", url):
        raise ValueError("A full LinkedIn post URL is required")
    campaign = load_campaign(day)
    selected_account = str(
        cdp_account or campaign.get("cdp_account") or load_config()["cdp_account"]
    )
    if selected_account not in CDP_ACCOUNTS:
        raise ValueError("Choose either the Design or Automation CDP lane.")
    existing_account = str(campaign.get("cdp_account") or "")
    if campaign.get("sources") and existing_account and existing_account != selected_account:
        raise ValueError(
            f"This campaign is already assigned to the {existing_account.title()} CDP lane."
        )
    campaign["cdp_account"] = selected_account
    if not any(source.get("submitted_url") == url for source in campaign["sources"]):
        campaign["sources"].append(
            {
                "submitted_url": url,
                "resolved_url": "",
                "reaction_count": 0,
                "profiles_collected": 0,
                "coverage": 0,
                "status": "queued",
                "added_at": now().isoformat(),
            }
        )
    campaign["status"] = "ready"
    save_campaign(campaign)
    return campaign


def _evaluate_json(cdp: Any, expression: str, timeout: float = 30) -> Any:
    raw = cdp.evaluate(expression, await_promise=True, timeout=timeout)
    return json.loads(raw) if isinstance(raw, str) else raw


SOURCE_REACTOR_BOOTSTRAP_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const roots = []; const add = root => { roots.push(root); root.querySelectorAll?.('*').forEach(el => { if (el.shadowRoot) add(el.shadowRoot); }); }; add(document);
  const all = sel => roots.flatMap(root => Array.from(root.querySelectorAll?.(sel) || []));
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let dialog = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0];
  let trigger = null;
  if (!dialog) {
    // 1. MAIN METHOD: Structural
    const allEls = all('*');
    const commentsEl = allEls.find(el => {
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      return /^\d+\s+comments?/.test(t) && el.children.length === 0;
    });
    if (commentsEl) {
      let container = commentsEl;
      const commentsRect = commentsEl.getBoundingClientRect();
      for (let i = 0; i < 20; i++) {
        container = container.parentElement;
        if (!container) break;
        const clickables = Array.from(container.querySelectorAll('a, button, [role="button"]'))
          .filter(visible)
          .filter(el => {
            if (el.contains(commentsEl)) return false;
            if (!(el.compareDocumentPosition(commentsEl) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
            return Math.abs(el.getBoundingClientRect().top - commentsRect.top) < 20;
          });
        if (clickables.length > 0) { trigger = { el: clickables[0] }; break; }
      }
    }
    // 2. FALLBACK 1: Exact LinkedIn DOM selectors
    if (!trigger) {
      const exactSelectors = ['button.social-details-social-counts__reactions-count', 'button.social-details-social-counts__count-value', 'li.social-details-social-counts__reactions button', 'li.social-details-social-counts__reactions a'];
      for (const sel of exactSelectors) {
        const found = all(sel).filter(visible);
        if (found.length > 0) { trigger = { el: found[0] }; break; }
      }
    }
    // 3. FALLBACK 2: Original Text Regex (Updated to include aria-label)
    if (!trigger) {
      const reactionCount = (text, aria) => { 
        const full = (text + ' ' + (aria || '')).trim();
        const others = full.match(/\b([\d,]+)\s+others?\s+reacted\b/i); 
        if (others) return parseInt(others[1].replace(/,/g,'')) + 1; 
        const direct = full.match(/\b([\d,]+)\s+reactions?\b/i); 
        return direct ? parseInt(direct[1].replace(/,/g,'')) : 0; 
      };
      const candidates = all('a,button,[role="button"]').filter(visible).map(el => ({el, text: (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), aria: el.getAttribute('aria-label') || ''})).filter(x => reactionCount(x.text, x.aria) > 0);
      candidates.sort((a,b) => reactionCount(b.text, b.aria) - reactionCount(a.text, a.aria));
      if (candidates.length > 0) trigger = { el: candidates[0].el };
    }
    if (!trigger) return JSON.stringify({success:false,error:'reaction_trigger_not_found'});
    trigger.el.click(); await sleep(1200);
    for(let waitPass=0; waitPass<30 && !dialog; waitPass++) { roots.splice(0); add(document); dialog = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0]; if(!dialog) await sleep(300); }
  }
  if (!dialog) return JSON.stringify({success:false,error:'reactions_dialog_not_found'});
  // LinkedIn mounts the dialog shell first and hydrates the reaction rows a
  // moment later. Wait for the count/rows before handing control to Python.
  for (let waitPass=0; waitPass<20; waitPass++) {
    roots.splice(0); add(document);
    const liveDialogs = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height);
    if (liveDialogs[0]) dialog = liveDialogs[0];
    const liveRoots=[]; const liveWalk=root=>{liveRoots.push(root);if(root.shadowRoot)liveWalk(root.shadowRoot);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)liveWalk(el.shadowRoot);});}; liveWalk(document);
    const liveAll=sel=>liveRoots.flatMap(root=>Array.from(root.querySelectorAll?.(sel)||[]));
    const liveText=liveAll('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t))||'';
    if (/\bAll\s+[\d,]+/i.test(liveText) || /^[\d,]+\s+All/i.test(liveText)) break;
    const refreshVisible = liveAll('button,[role="button"]').some(el => /\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()));
    if (refreshVisible) return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
    await sleep(300);
  }
  const dr = dialog.getBoundingClientRect();
  const scoped=[]; const walk=root=>{ scoped.push(root); if(root.shadowRoot) walk(root.shadowRoot); root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)walk(el.shadowRoot);}); }; walk(dialog);
  const inDialog = el => { const r=el.getBoundingClientRect(); const cx=r.left+r.width/2, cy=r.top+r.height/2; return r.width>0&&r.height>0&&cx>=dr.left&&cx<=dr.right&&cy>=dr.top&&cy<=dr.bottom; };
  const dialogAll = sel => scoped.flatMap(root => Array.from(root.querySelectorAll?.(sel)||[])).filter(inDialog);
  if (dialogAll('button,[role="button"]').some(el => /\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()))) return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
  const countText = dialogAll('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t)) || '';
  const expected = parseInt((countText.match(/[\d,]+/)||['0'])[0].replace(/,/g,''));
  let sourceCard=trigger?.el; for(let i=0;i<10&&sourceCard;i++,sourceCard=sourceCard.parentElement){if(sourceCard.matches?.('[data-view-name="feed-full-update"],article[data-urn]'))break;}
  const sourceTimestamp=(sourceCard?.querySelector('.update-components-actor__sub-description,.feed-shared-actor__sub-description')?.innerText||'').trim();
  return JSON.stringify({success:true,resolved_url:location.href,source_timestamp:sourceTimestamp,expected,dialog_ready:true});
})()
"""

SOURCE_REACTOR_SNAPSHOT_JS = r"""
(() => {
  const roots=[]; const add=root=>{roots.push(root);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)add(el.shadowRoot);});}; add(document);
  const visible=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const dialogs=roots.flatMap(r=>Array.from(r.querySelectorAll?.('dialog,[role="dialog"]')||[])).filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height);
  const dialog=dialogs[0]; if(!dialog)return JSON.stringify({success:false,error:'reactions_dialog_not_found'});
  const dr=dialog.getBoundingClientRect();
  const scoped=[]; const walk=root=>{scoped.push(root);if(root.shadowRoot)walk(root.shadowRoot);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)walk(el.shadowRoot);});}; walk(dialog);
  const inDialog=el=>{const r=el.getBoundingClientRect();const cx=r.left+r.width/2,cy=r.top+r.height/2;return r.width>0&&r.height>0&&cx>=dr.left&&cx<=dr.right&&cy>=dr.top&&cy<=dr.bottom;};
  const all=sel=>scoped.flatMap(r=>Array.from(r.querySelectorAll?.(sel)||[]));
  if(all('button,[role="button"]').some(el=>/\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim())))return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
  const found=new Map();
  all('a[href*="linkedin.com/in/"],a[href^="/in/"]').filter(visible).forEach(a=>{const href=a.href||a.getAttribute('href');if(href){const url=href.split('?')[0];found.set(url,{url,name:(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim()});}});
  const text=all('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t))||'';
  const expected=parseInt((text.match(/[\d,]+/)||['0'])[0].replace(/,/g,''));
  const divs=all('div').filter(el=>{const s=getComputedStyle(el);return /(auto|scroll)/.test(s.overflowY)&&el.scrollHeight>el.clientHeight+20;});
  const scroller=divs.sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight))[0];
  const sr=scroller?.getBoundingClientRect();
  return JSON.stringify({success:true,expected,profiles:Array.from(found.values()),profiles_collected:found.size,scroller:scroller&&sr?{left:sr.left,top:sr.top,width:sr.width,height:sr.height,scrollTop:scroller.scrollTop,scrollHeight:scroller.scrollHeight,clientHeight:scroller.clientHeight}:null});
})()
"""


PROFILE_GATE_JS = r"""
(() => {
  // Keep each field in a deliberately bounded area. The header owns identity
  // data; Activity is the permitted follower-count fallback.
  const main=document.querySelector('main');
  const text=e=>(e?.innerText||e?.textContent||'').replace(/\s+/g,' ').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  if(!main)return JSON.stringify({error:'profile_main_missing',page_url:location.href});
  const contact=Array.from(main.querySelectorAll('a[href*="/overlay/contact-info/"]')).find(vis);
  if(!contact)return JSON.stringify({error:'profile_contact_info_missing',page_url:location.href});
  const contactP=contact.closest('p');
  const metadataRow=contactP?.parentElement;
  const locationEl=Array.from(metadataRow?.children||[]).find(e=>e.matches?.('p')&&e!==contactP&&vis(e)&&text(e));
  const actionPattern=/\b(follow|connect|message|more|save in sales navigator|visit my website|book an appointment)\b/i;
  const actions=root=>Array.from(root?.querySelectorAll('button,a,[role="button"]')||[]).filter(e=>vis(e)&&actionPattern.test(`${text(e)} ${e.getAttribute('aria-label')||''}`));
  const headings=root=>Array.from(root?.querySelectorAll('h1,h2')||[]).filter(e=>vis(e)&&text(e)&&!/\bnotifications?\b/i.test(text(e)));
  const followerMatches=root=>Array.from(root?.querySelectorAll('p,span')||[]).filter(e=>vis(e)&&/\bfollowers?\b/i.test(text(e))).sort((a,b)=>text(a).length-text(b).length);

  // Contact info belongs to the profile header section. Search no farther
  // than that section when deriving the smaller card shown in the UI.
  const headerSection=contact.closest('section');
  if(!headerSection||!main.contains(headerSection))return JSON.stringify({error:'profile_header_section_missing',page_url:location.href});
  let headerCard=metadataRow;
  while(headerCard){
    if(headings(headerCard).length&&actions(headerCard).length)break;
    if(headerCard===headerSection){headerCard=null;break;}
    headerCard=headerCard.parentElement;
  }
  if(!headerCard)return JSON.stringify({error:'profile_header_missing',page_url:location.href});
  const nameEl=headings(headerCard)[0];
  const headerFollower=followerMatches(headerCard)[0];

  // Some profiles omit followers from the header. Permit the explicit
  // Activity section only, excluding follower text inside an Activity post.
  const activityHeading=Array.from(main.querySelectorAll('h1,h2,h3,[role="heading"]')).find(e=>vis(e)&&text(e)==='Activity'&&e.closest('section')&&e.closest('section')!==headerSection);
  const activitySection=activityHeading?.closest('section');
  const activityFollower=Array.from(activitySection?.querySelectorAll('p,span')||[])
    .filter(e=>vis(e)&&/\bfollowers?\b/i.test(text(e))&&!e.closest('[data-view-name="feed-full-update"],article,[data-urn]'))
    .sort((a,b)=>a.getBoundingClientRect().top-b.getBoundingClientRect().top||text(a).length-text(b).length)[0];
  const followerEl=headerFollower||activityFollower;
  const followerSource=headerFollower?'header':activityFollower?'activity':'missing';
  return JSON.stringify({
    name:text(nameEl),
    location:text(locationEl),
    follower_text:text(followerEl),
    follower_source:followerSource,
    contact_info_text:text(contact),
    page_url:location.href
  });
})()
"""


POST_CARDS_JS = r"""
(() => JSON.stringify(Array.from(document.querySelectorAll('[data-view-name="feed-full-update"]')).map(card => {
  const scope=card.getAttribute('data-view-tracking-scope')||''; const urn=(scope.match(/urn:li:activity:\d+/)||[''])[0];
  const timeEl=card.querySelector('.update-components-actor__sub-description'); const like=card.querySelector('button[aria-label="React Like"]');
  const author=card.querySelector('a[href*="/in/"]');
  return {post_urn:urn,post_url:urn?'https://www.linkedin.com/feed/update/'+urn+'/':'',timestamp:(timeEl?.innerText||'').trim(),liked:like?.getAttribute('aria-pressed')==='true',like_available:!!like,author_url:(author?.href||'').split('?')[0]};
}).filter(x=>x.post_urn)))()
"""


def _connect_campaign_browser(
    campaign: dict[str, Any], config: dict[str, Any], execute: bool
) -> Any:
    from linkedin_helper import HumanSimulator, LinkedInSession, inject_stealth

    endpoint = CDP_ACCOUNTS[config["cdp_account"]]
    os.environ["LINKEDIN_CDP_HOST"] = endpoint["host"]
    os.environ["LINKEDIN_CDP_PORT"] = str(endpoint["port"])
    session = LinkedInSession()
    preflight = session.connect(skip_rate_check=not execute)
    if not preflight.get("ok"):
        raise RuntimeError(preflight.get("block_reason") or "LinkedIn preflight failed")
    cdp = session.cdp
    target = cdp.create_page_target("https://www.linkedin.com/feed/")
    inject_stealth(cdp)
    campaign["target_id"] = target["target_id"]
    campaign["cdp_account"] = config["cdp_account"]
    campaign["stage"] = "browser_ready"
    save_campaign(campaign)
    simulator = HumanSimulator(cdp)
    session.sim = simulator
    return cdp, simulator, session


def _navigate(cdp: Any, url: str, settle: float | None = None) -> None:
    """Use the shared readiness checks; deadlines are ceilings, not sleeps."""
    from linkedin_helper import _wait_for_activity_feed_state, _wait_for_linkedin_ready

    activity = "/recent-activity/" in url
    selector = (
        '[data-view-name="feed-full-update"], .feed-shared-update-v2, main'
        if activity
        else 'main h1, main h2, a[href*="/overlay/contact-info/"], main'
    )
    events = []
    deadline = time.monotonic() + 180
    for attempt in range(3):
        event = {"url": url, "attempt": attempt + 1, "started_at": now().isoformat()}
        events.append(event)
        try:
            try:
                result = cdp.navigate(url, wait_load=False, timeout=8)
                if result and result.get("errorText"):
                    raise RuntimeError(result["errorText"])
            except (TimeoutError, RuntimeError) as error:
                event["navigation_error"] = str(error)
                # A timed-out command may already have navigated successfully.
                current = str(cdp.evaluate("location.href", timeout=5) or "")
                if urlsplit(current).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                    cdp.evaluate("window.location.href = " + json.dumps(url), timeout=8)
                    event["fallback"] = "js_location"
            arrived = False
            until = min(deadline, time.monotonic() + 20)
            while time.monotonic() < until:
                current = str(cdp.evaluate("location.href", timeout=5) or "")
                if current.startswith("chrome-error:"):
                    raise RuntimeError("browser_network_error_page")
                if any(
                    part in urlsplit(current).path
                    for part in ("/checkpoint/", "/login", "/authwall")
                ):
                    raise PermissionError("LinkedIn authentication/checkpoint requires attention")
                requested = urlsplit(url)
                arrived_via_short_link = (
                    (requested.hostname or "").endswith("lnkd.in")
                    and (urlsplit(current).hostname or "").endswith("linkedin.com")
                    and urlsplit(current).path.startswith("/posts/")
                )
                if arrived_via_short_link or (
                    urlsplit(current).hostname == requested.hostname
                    and urlsplit(current).path.rstrip("/") == requested.path.rstrip("/")
                ):
                    arrived = True
                    break
                time.sleep(0.35)
            if not arrived:
                raise RuntimeError("requested_destination_not_reached")
            ready = _wait_for_linkedin_ready(
                cdp,
                expected_selector=selector,
                timeout=min(45, max(1, deadline - time.monotonic())),
                stable_for=0.5,
                ignored_overlays={".authentication-outlet"},
                ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
            )
            event["readiness"] = ready
            if not ready.get("ready"):
                raise RuntimeError("page_not_ready")
            if activity:
                feed = _wait_for_activity_feed_state(
                    cdp, timeout=min(45, max(1, deadline - time.monotonic()))
                )
                event["feed_readiness"] = feed
                if not feed.get("ready"):
                    raise RuntimeError("activity_feed_not_hydrated")
            event["ok"] = True
            cdp.post_engagement_navigation = events
            append_history({"type": "navigation", "events": events, "at": now().isoformat()})
            return
        except PermissionError:
            raise
        except Exception as error:
            event["error"] = str(error)
            if attempt == 2 or time.monotonic() >= deadline:
                break
            try:
                cdp.evaluate("document.readyState", timeout=5)
                cdp.send("Page.stopLoading", timeout=5)
                event["recovery"] = "stop_loading_and_retry"
            except Exception:
                # Preserve every other workflow's tab and stay on this account.
                target = cdp.create_page_target("about:blank")
                event["recovery"] = "fresh_workflow_tab"
                event["target_id"] = target.get("target_id")
    cdp.post_engagement_navigation = events
    append_history({"type": "navigation_failed", "events": events, "at": now().isoformat()})
    raise RuntimeError("Navigation failed after recovery: " + str(events[-1].get("error")))


def collect_sources(cdp: Any, campaign: dict[str, Any], config: dict[str, Any]) -> None:
    known = {c.get("profile_url") for c in campaign["candidates"]}
    for source in campaign["sources"]:
        if source.get("status") == "collected" and source.get("stop_reason") in {
            "exhausted",
            "stagnant",
        }:
            continue
        # A visible Refresh control is an explicitly detected transient loading
        # failure, not evidence that the source has no reactors. Reload the
        # source and reopen the modal a bounded number of times.
        result: dict[str, Any] = {}
        collection_attempt = 0
        max_collection_attempts = int(config.get("source_collection_max_attempts", 3))
        while collection_attempt < max_collection_attempts:
            collection_attempt += 1
            _navigate(cdp, source["submitted_url"])
            result = _evaluate_json(cdp, SOURCE_REACTOR_BOOTSTRAP_JS, timeout=90)
            if result.get("success") or result.get("error") != "reactions_modal_refresh_required":
                break
            source.update(
                status="retrying_network",
                collection_attempts=collection_attempt,
                last_retry_reason="reactions_modal_refresh_required",
            )
            save_campaign(campaign)
            if collection_attempt < max_collection_attempts:
                time.sleep(random.uniform(2.0, 4.0))
        source["collection_attempts"] = collection_attempt
        if not result.get("success"):
            source.update(status="failed", error=result.get("error", "source_collection_failed"))
            save_campaign(campaign)
            continue
        expected = int(result.get("expected") or 0)
        found: dict[str, dict[str, Any]] = {}
        stagnant = 0
        passes = 0
        stop_reason = "pass_limit"
        collection_error = ""
        while passes < 160 and stagnant < 5:
            passes += 1
            snapshot = _evaluate_json(cdp, SOURCE_REACTOR_SNAPSHOT_JS, timeout=12)
            if not snapshot.get("success"):
                collection_error = snapshot.get("error", "reactions_dialog_not_found")
                stop_reason = "collection_error"
                break
            before = len(found)
            for profile in snapshot.get("profiles", []):
                url = str(profile.get("url") or "").split("?")[0]
                if url:
                    found[url] = {"url": url, "name": profile.get("name", "")}
            stagnant = stagnant + 1 if len(found) == before else 0
            expected = expected or int(snapshot.get("expected") or 0)
            if expected and len(found) >= expected:
                stop_reason = "exhausted"
                break
            rect = snapshot.get("scroller") or {}
            # The modal shell can report a valid dialog for a few hundred ms
            # before LinkedIn mounts its virtualized scroll container. Do not
            # treat that hydration window as a completed/failed extraction.
            if not rect:
                time.sleep(random.uniform(0.45, 0.9))
                continue
            x = float(rect.get("left", 0)) + float(rect.get("width", 0)) / 2
            y = float(rect.get("top", 0)) + float(rect.get("height", 0)) / 2
            try:
                cdp.send(
                    "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=8
                )
                cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": 0,
                        "deltaY": max(100, int(float(rect.get("height", 495)) * 0.75)),
                    },
                    timeout=8,
                )
            except Exception as error:
                collection_error = str(error)
                stop_reason = "scroll_error"
                break
            time.sleep(min(3, 0.7 + stagnant * 0.5))
        if stagnant >= 5:
            stop_reason = "stagnant"
        source.update(
            stop_reason=stop_reason,
            extraction_error=collection_error,
            passes=passes,
            stagnant_passes=stagnant,
        )
        result.update(
            profiles=list(found.values()),
            profiles_collected=len(found),
            coverage=(len(found) / expected if expected else 0),
            expected=expected,
            passes=passes,
            stagnant_passes=stagnant,
        )
        try:
            _evaluate_json(
                cdp,
                "(() => { const b=Array.from(document.querySelectorAll('button')).find(x=>/dismiss/i.test(x.getAttribute('aria-label')||'')); if(b)b.click(); return true; })()",
                timeout=8,
            )
        except Exception:
            pass
        # The error can surface after the modal initially opened, while its
        # virtualized rows are loading. Use the remaining bounded attempts for
        # the same source before recording a partial result.
        if (
            collection_error == "reactions_modal_refresh_required"
            and collection_attempt < max_collection_attempts
        ):
            source.update(
                status="retrying_network",
                collection_attempts=collection_attempt,
                last_retry_reason="reactions_modal_refresh_required",
            )
            save_campaign(campaign)
            time.sleep(random.uniform(2.0, 4.0))
            return collect_sources(cdp, campaign, config)
        source_age = parse_relative_age_hours(result.get("source_timestamp", ""))
        if source_age is not None and source_age > int(config["source_max_age_days"]) * 24:
            source.update(
                status="rejected_too_old",
                source_timestamp=result.get("source_timestamp", ""),
                source_age_hours=source_age,
            )
            save_campaign(campaign)
            continue
        source.update(
            resolved_url=result.get("resolved_url", ""),
            source_timestamp=result.get("source_timestamp", ""),
            source_age_hours=source_age,
            reaction_count=result.get("expected", 0),
            profiles_collected=result.get("profiles_collected", 0),
            coverage=round(float(result.get("coverage", 0)), 4),
            status="collected"
            if float(result.get("coverage", 0)) >= float(config["reactor_min_coverage"])
            else "partial",
        )
        for profile in result.get("profiles", []):
            url = canonical_profile_url(profile.get("url", ""))
            if not url or url in known:
                continue
            known.add(url)
            campaign["candidates"].append(
                {
                    "profile_url": url,
                    "name": profile.get("name", ""),
                    "source_post": source.get("resolved_url") or source["submitted_url"],
                    "status": "discovered",
                    "attempts": 0,
                    "likes_assigned": choose_like_target(campaign["day"], url, config),
                    "likes_completed": 0,
                }
            )
        save_campaign(campaign)


def review_recent_activity(cdp: Any, simulator: Any) -> dict[str, int]:
    """Briefly review the recent-activity list before acting on its posts."""
    metrics_js = """(() => { const card=document.querySelector('[data-view-name=\"feed-full-update\"]'); let node=card; while(node){const s=getComputedStyle(node);if(node.scrollHeight>node.clientHeight+40&&/(auto|scroll)/.test(s.overflowY))break;node=node.parentElement;} node=node||document.scrollingElement; const r=node.getBoundingClientRect(); return JSON.stringify({left:r.left,top:r.top,width:r.width,height:r.height,scrollTop:node.scrollTop||0,scrollHeight:node.scrollHeight||0,clientHeight:node.clientHeight||0}); })()"""
    metrics = _evaluate_json(cdp, metrics_js, timeout=8)
    before = int(metrics.get("scrollTop", 0) or 0)
    x = float(metrics.get("left", 0)) + max(20, float(metrics.get("width", 0)) / 2)
    y = float(metrics.get("top", 0)) + min(
        max(40, float(metrics.get("height", 0)) / 2), max(40, float(metrics.get("height", 0)) - 30)
    )
    # The activity feed is often an internal scrolling region. Wheel events
    # target that visible region directly, then the current newest posts are
    # restored before collecting their IDs.
    for distance in (random.randint(260, 440), random.randint(140, 260)):
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=8)
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": distance},
            timeout=8,
        )
        time.sleep(random.uniform(0.8, 1.5))
    furthest = _evaluate_json(cdp, metrics_js, timeout=8)
    cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseWheel",
            "x": x,
            "y": y,
            "deltaX": 0,
            "deltaY": -max(int(furthest.get("scrollTop", 0) or 0), 1),
        },
        timeout=8,
    )
    time.sleep(random.uniform(0.5, 0.9))
    return {"before": before, "furthest": int(furthest.get("scrollTop", 0) or 0)}


def inspect_candidate(
    cdp: Any, simulator: Any, candidate: dict[str, Any], config: dict[str, Any]
) -> None:
    campaign = getattr(cdp, "engagement_campaign", None)
    if campaign is not None:
        execution_event(
            campaign, candidate, action="Opening profile", method="Direct URL", reason=""
        )
    _navigate(cdp, candidate["profile_url"])
    gate = _evaluate_json(cdp, PROFILE_GATE_JS)
    validate_profile_gate(gate)
    followers = parse_follower_count(gate.get("follower_text", ""))
    candidate.update(
        gate,
        follower_count=followers,
        profile_parser_version=PROFILE_PARSER_VERSION,
        profile_assessed_at=now().isoformat(),
        **classify_location(gate.get("location", "")),
    )
    from linkedin_helper import (
        _open_profile_activity_from_profile,
        _wait_for_activity_destination,
        _wait_for_activity_feed_state,
    )

    try:
        if campaign is not None:
            execution_event(campaign, candidate, action="Opening activity", method="DOM", reason="")
        navigation = _open_profile_activity_from_profile(cdp)
        destination = (
            _wait_for_activity_destination(
                cdp, candidate["profile_url"].rstrip("/"), "posts", timeout=8
            )
            if navigation.get("clicked")
            else {}
        )
        feed = _wait_for_activity_feed_state(cdp, timeout=15) if destination.get("arrived") else {}
        if not destination.get("arrived") or not feed.get("ready"):
            raise RuntimeError("Activity click did not reach a ready posts feed")
        candidate["posts_navigation"] = {"via": "selector_based", "feed_readiness": feed}
        cdp.post_engagement_navigation = [{"feed_readiness": feed}]
    except (RuntimeError, TimeoutError) as error:
        if campaign is not None:
            execution_event(
                campaign,
                candidate,
                action="Opening activity",
                method="Direct URL · fallback",
                reason=str(error),
            )
        _navigate(cdp, candidate["profile_url"].rstrip("/") + "/recent-activity/all/")
        candidate["posts_navigation"] = {"via": "direct_url_fallback", "reason": str(error)}
    candidate["activity_review"] = review_recent_activity(cdp, simulator)
    posts = _evaluate_json(cdp, POST_CARDS_JS)
    if not posts:
        checks = getattr(cdp, "post_engagement_navigation", [])
        feed = checks[-1].get("feed_readiness", {}) if checks else {}
        if feed.get("reason") != "explicit_empty_state":
            raise RuntimeError("post_parser_returned_no_cards_for_loaded_feed")
    for post in posts:
        post["age_hours"] = parse_relative_age_hours(post.get("timestamp", ""))
    eligible = [
        p
        for p in posts
        if p["age_hours"] is not None
        and p["age_hours"] <= int(config["activity_window_days"]) * 24
        and not p["liked"]
    ]
    newest = min((p["age_hours"] for p in posts if p["age_hours"] is not None), default=None)
    candidate.update(
        posts=posts,
        newest_post_age_hours=newest,
        engagement_eligible=newest is not None and newest <= int(config["newest_post_max_hours"]),
        available_unliked_posts=len(eligible),
        status="audited",
    )


def click_like(cdp: Any, urn: str) -> bool:
    expression = r"""(async () => {
      const urn = %s;
      const button = () => Array.from(document.querySelectorAll('[data-view-name="feed-full-update"]'))
        .find(c => (c.getAttribute('data-view-tracking-scope') || '').includes(urn))
        ?.querySelector('button[aria-label="React Like"]');
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      let b = button();
      if (!b) return {error:'like_button_missing'};
      if (b.getAttribute('aria-pressed') === 'true') return {already_liked:true};
      b.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
      // Give each post its own reading interval, then re-resolve the button
      // and verify visibility because the page may change during the wait.
      await sleep(5000 + Math.random() * 20000);
      b = button();
      if (!b) return {error:'like_button_disappeared'};
      const r = b.getBoundingClientRect(), style = getComputedStyle(b);
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const top = document.elementFromPoint(x, y);
      if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight ||
          style.visibility === 'hidden' || style.display === 'none' || !top || !b.contains(top) ||
          b.disabled || b.getAttribute('aria-disabled') === 'true') return {error:'like_button_not_visible_or_obstructed'};
      if (b.getAttribute('aria-pressed') === 'true') return {already_liked:true};
      b.click();
      for (let i=0; i<20; i++) {
        await sleep(250);
        if (button()?.getAttribute('aria-pressed') === 'true') return {liked:true};
      }
      return {error:'like_result_unconfirmed'};
    })()""" % json.dumps(urn)
    # Allow the full 25-second reading interval plus reaction verification.
    result = cdp.evaluate(expression, await_promise=True, timeout=40)
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(
            (result or {}).get("error", "like_result_invalid")
            if isinstance(result, dict)
            else "like_result_invalid"
        )
    return bool(result.get("liked"))


def follow_current_profile(cdp: Any, name: str) -> bool:
    expression = (
        """(() => { const wanted=%s.toLowerCase(); const b=Array.from(document.querySelectorAll('button')).find(x=>{const s=(x.getAttribute('aria-label')||x.innerText||'').trim().toLowerCase();return s.startsWith('follow')&&(wanted===''||s.includes(wanted));}); if(!b)return false;b.click();return true;})()"""
        % json.dumps(name)
    )
    return bool(cdp.evaluate(expression, timeout=10))


def activity_counts(detail: dict[str, Any], max_hours: float) -> dict[str, int]:
    tabs = detail.get("tabs", {}) if isinstance(detail, dict) else {}
    counts = {}
    for key in ("reactions", "comments", "posts"):
        activities = (
            tabs.get(key, {}).get("activities", []) if isinstance(tabs.get(key), dict) else []
        )
        counts[key] = sum(
            1
            for item in activities
            if (age := parse_relative_age_hours(item.get("time_text", ""))) is not None
            and age <= max_hours
        )
    return counts


def qualifies(counts: dict[str, int], config: dict[str, Any]) -> bool:
    return (
        counts.get("reactions", 0) >= int(config["reaction_threshold"])
        or counts.get("comments", 0) >= int(config["comment_threshold"])
        or counts.get("posts", 0) >= int(config["post_threshold"])
    )


def recommendation_for(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    counts = candidate.get("activity_counts", {})
    if not qualifies(counts, config):
        action = "ineligible"
        reason = "activity_below_threshold"
    elif int(candidate.get("follower_count") or 0) > int(config["follower_connection_limit"]):
        action = "follow"
        reason = "active_profile_above_follower_connection_limit"
    else:
        action = "connect"
        reason = "active_profile_within_follower_connection_limit"
    return {
        "action": action,
        "reason": reason,
        "assessed_at": now().isoformat(),
        "assessment_version": ACTIVITY_ASSESSMENT_VERSION,
    }


def assess_engaged_candidate(
    session: Any, candidate: dict[str, Any], config: dict[str, Any]
) -> bool:
    """Perform the expensive activity review once, after Likes qualify a profile."""
    recommendation = candidate.get("recommendation") or {}
    if (
        candidate.get("activity_assessment_status") == "complete"
        and int(recommendation.get("assessment_version") or 0) == ACTIVITY_ASSESSMENT_VERSION
    ):
        return True
    candidate["activity_assessment_status"] = "running"
    candidate["activity_assessment_started_at"] = now().isoformat()
    candidate["activity_assessment_attempts"] = (
        int(candidate.get("activity_assessment_attempts", 0)) + 1
    )
    try:
        cdp = getattr(session, "cdp", None)
        campaign = getattr(cdp, "engagement_campaign", None) if cdp else None
        if isinstance(campaign, dict):
            cdp.execution_observer = lambda **event: execution_event(campaign, candidate, **event)
        detail = session.read_activity_detail(
            candidate["profile_url"],
            max_seconds=75,
            navigation_type="selector_based",
            tab_order=[("reactions", "Reactions"), ("comments", "Comments"), ("posts", "Posts")],
            minimum_direct_comments=int(config["comment_threshold"]),
            disable_early_stop=True,
        )
        if detail.get("error") or detail.get("danger"):
            raise RuntimeError(
                f"Activity assessment incomplete: {detail.get('danger') or detail.get('reason') or detail.get('error')}"
            )
        candidate["activity_counts"] = activity_counts(
            detail, int(config["activity_window_days"]) * 24
        )
        candidate["very_active"] = qualifies(candidate["activity_counts"], config)
        candidate["recommendation"] = recommendation_for(candidate, config)
        candidate["activity_assessment_status"] = "complete"
        candidate["activity_assessed_at"] = now().isoformat()
        candidate.pop("activity_error", None)
        return True
    except Exception as error:
        candidate["activity_assessment_status"] = "failed"
        candidate["activity_error"] = str(error)[:500]
        return False
    finally:
        cdp = getattr(session, "cdp", None)
        if cdp is not None:
            cdp.execution_observer = None


def final_action_queue(
    candidates: Iterable[dict[str, Any]], campaign: dict[str, Any]
) -> list[dict[str, Any]]:
    """Interleave ranked Connect and Follow recommendations without scrambling rank."""
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("activity_assessment_status") == "complete"
        and int(candidate.get("profile_parser_version") or 0) == PROFILE_PARSER_VERSION
        and int((candidate.get("recommendation") or {}).get("assessment_version") or 0)
        == ACTIVITY_ASSESSMENT_VERSION
    ]
    ranked = sorted(eligible, key=ranking_key)
    connection_target = campaign.get("connection_target")
    connect = [
        candidate
        for candidate in ranked
        if (candidate.get("recommendation") or {}).get("action") == "connect"
        and not ledger_has(
            str(campaign.get("day", "")), "connect", candidate.get("profile_url", "")
        )
        and not (
            isinstance(candidate.get("connection_result"), dict)
            and candidate["connection_result"].get("success")
        )
    ][: int(connection_target) if connection_target is not None else None]
    follow_target = campaign.get("follow_target")
    follow = [
        candidate
        for candidate in ranked
        if (candidate.get("recommendation") or {}).get("action") == "follow"
        and not ledger_has(str(campaign.get("day", "")), "follow", candidate.get("profile_url", ""))
        and candidate.get("follow_result") != "followed"
    ][: int(follow_target) if follow_target is not None else None]
    rng = daily_rng(str(campaign.get("day", "")), "final-action-order")
    queue: list[dict[str, Any]] = []
    last_lane = ""
    same_lane_run = 0
    while connect or follow:
        choices = [lane for lane, items in (("connect", connect), ("follow", follow)) if items]
        alternatives = [lane for lane in choices if lane != last_lane]
        lane = rng.choice(alternatives if same_lane_run >= 2 and alternatives else choices)
        queue.append((connect if lane == "connect" else follow).pop(0))
        if lane == last_lane:
            same_lane_run += 1
        else:
            last_lane = lane
            same_lane_run = 1
    return queue


def ranking_key(candidate: dict[str, Any]) -> tuple:
    counts = candidate.get("activity_counts", {})
    thresholds = (
        counts.get("reactions", 0) >= 5,
        counts.get("comments", 0) >= 3,
        counts.get("posts", 0) >= 3,
    )
    return (
        int(candidate.get("geography_tier", 4)),
        -sum(thresholds),
        -sum(counts.values()),
        candidate.get("newest_post_age_hours", 10**9),
        candidate.get("profile_url", ""),
    )


def upsert_high_signal(candidate: dict[str, Any]) -> None:
    state = read_json(HIGH_SIGNAL_PATH, {"profiles": []})
    rows = state.setdefault("profiles", [])
    existing = next(
        (row for row in rows if row.get("profile_url") == candidate.get("profile_url")), None
    )
    payload = {
        key: candidate.get(key)
        for key in (
            "profile_url",
            "name",
            "location",
            "follower_count",
            "activity_counts",
            "source_post",
            "country_code",
            "region",
        )
    }
    payload["last_seen_at"] = now().isoformat()
    if existing:
        existing.update(payload)
    else:
        payload["first_seen_at"] = now().isoformat()
        rows.append(payload)
    write_json(HIGH_SIGNAL_PATH, state)


def begin_action(campaign, candidate, action, post_urn=""):
    campaign["pending_action"] = {
        "action": action,
        "profile_url": candidate["profile_url"],
        "post_urn": post_urn,
        "account": action_account(),
        "batch_number": campaign.get("current_batch_number", 1),
        "started_at": now().isoformat(),
    }
    save_campaign(campaign)


def reconcile_campaign(campaign):
    """Recover confirmed writes; ambiguous clicks never become assumed success."""
    events = read_daily_ledger()["events"]
    if any(e.get("day") == campaign["day"] and not e.get("account") for e in events):
        raise RuntimeError(
            "Legacy action ledger has unassigned account records for this day; reconcile account ownership before running."
        )
    pending = campaign.get("pending_action")
    if pending and ledger_has(
        campaign["day"], pending["action"], pending["profile_url"], pending.get("post_urn", "")
    ):
        campaign.pop("pending_action", None)
    scoped = [
        e
        for e in events
        if e.get("day") == campaign["day"]
        and e.get("account") == action_account()
        and e.get("campaign_id") == campaign.get("created_at")
    ]
    for candidate in campaign["candidates"]:
        matches = [e for e in scoped if e.get("profile_url") == candidate.get("profile_url")]
        likes = [e for e in matches if e.get("action") == "like"]
        if likes or any(e.get("action") == "engagement" for e in matches):
            candidate["status"] = "engaged"
            candidate["likes_completed"] = max(len(likes), int(candidate.get("likes_completed", 0)))
            candidate.setdefault(
                "engagement_batch",
                (pending or {}).get("batch_number", campaign.get("current_batch_number") or 1),
            )
            record_ledger_action(
                campaign["day"],
                "engagement",
                candidate["profile_url"],
                campaign_id=campaign.get("created_at", ""),
            )
        if any(e.get("action") == "connect" for e in matches):
            candidate["connection_result"] = {"success": True, "status": "recovered_from_ledger"}
        if any(e.get("action") == "follow" for e in matches):
            candidate["follow_result"] = "followed"
    campaign["engaged"] = sum(
        1 for c in campaign["candidates"] if int(c.get("likes_completed", 0)) > 0
    )
    campaign["connections_sent"] = sum(
        1 for c in campaign["candidates"] if (c.get("connection_result") or {}).get("success")
    )
    campaign["followed"] = sum(
        1 for c in campaign["candidates"] if c.get("follow_result") == "followed"
    )
    for batch in campaign.get("engagement_batches", []):
        batch["engaged"] = sum(
            1
            for c in campaign["candidates"]
            if int(c.get("likes_completed", 0)) > 0 and c.get("engagement_batch") == batch["number"]
        )
        if batch["engaged"] >= batch["target"]:
            batch["status"] = "completed"
            batch.setdefault("completed_at", now().isoformat())
    batches = campaign.get("engagement_batches", [])
    upcoming = next((b for b in batches if b.get("status") != "completed"), None)
    if upcoming and int(upcoming["number"]) > 1 and not upcoming.get("started_at"):
        previous = next(b for b in batches if int(b["number"]) == int(upcoming["number"]) - 1)
        if (
            not campaign.get("next_batch_at")
            or campaign.get("current_batch_number") != upcoming["number"]
        ):
            finished = previous.get("completed_at") or now().isoformat()
            campaign["next_batch_at"] = (
                datetime.fromisoformat(finished)
                + timedelta(minutes=int(load_config()["inter_batch_delay_minutes"]))
            ).isoformat()
            campaign["current_batch_number"] = upcoming["number"]
    save_campaign(campaign)


def run_campaign(day: str | None, execute: bool) -> dict[str, Any]:
    config = load_config()
    campaign = load_campaign(day)
    if not execute and (
        campaign.get("engaged") or campaign.get("connections_sent") or campaign.get("followed")
    ):
        raise RuntimeError(
            "Audit cannot overwrite a campaign with live outcomes; use a separate day for audit fixtures."
        )
    if campaign_control(campaign["day"]).get("action") == "pause":
        campaign.update(status="paused", stage="paused")
        save_campaign(campaign)
        return campaign
    if campaign.get("cdp_account"):
        config["cdp_account"] = campaign["cdp_account"]
    campaign["cdp_account"] = config["cdp_account"]
    ACTION_ACCOUNT.set(config["cdp_account"])
    if execute:
        reconcile_campaign(campaign)
        if campaign.get("pending_action"):
            campaign.update(status="needs_reconciliation", stage="uncertain_action")
            save_campaign(campaign)
            return campaign
    if not campaign["sources"]:
        raise RuntimeError("Add at least one source post first")
    campaign.update(status="running", dry_run=not execute, stage="source_collection")
    save_campaign(campaign)
    cdp = None
    try:
        cdp, sim, session = _connect_campaign_browser(campaign, config, execute)
        cdp.engagement_campaign = campaign
        execution_event(
            campaign,
            {"name": "Source posts", "profile_url": ""},
            action="Collecting source posts",
            method="Direct URL",
            reason="",
        )
        collect_sources(cdp, campaign, config)
        ensure_obf_diversions(campaign, config)
        batch = current_engagement_batch(campaign, config)
        if not batch.get("started_at"):
            batch["started_at"] = now().isoformat()
        batch["status"] = "running"
        campaign["current_batch_number"] = int(batch["number"])
        save_campaign(campaign)
        campaign["stage"] = "candidate_audit"
        save_campaign(campaign)
        order = list(campaign["candidates"])
        daily_rng(campaign["day"], "candidate-order").shuffle(order)
        engagement_progress = int(campaign.get("engaged", 0)) if execute else 0
        batch_progress = int(batch.get("engaged", 0)) if execute else 0
        paused_for_quota = False
        # Normally assessments are completed inline immediately after Likes.
        # This recovery pass is only for a checkpoint interrupted between the
        # saved engagement and its assessment; stale-parser records are never reused.
        for candidate in order:
            if (
                candidate.get("status") == "engaged"
                and int(candidate.get("profile_parser_version") or 0) == PROFILE_PARSER_VERSION
                and candidate.get("activity_assessment_status") != "complete"
            ):
                raise_if_paused(campaign)
                campaign["stage"] = "assessment_recovery"
                save_campaign(campaign)
                assess_engaged_candidate(session, candidate, config)
                save_campaign(campaign)
        campaign["stage"] = "candidate_audit"
        save_campaign(campaign)
        for candidate in order:
            raise_if_paused(campaign)
            if (
                batch_progress >= int(batch["target"])
                if execute
                else engagement_progress >= campaign["target"]
            ):
                break
            if candidate.get("status") in {
                "engaged",
                "deferred_final_action",
                "inactive",
                "no_eligible_unliked_posts",
                "skipped_after_retry_cap",
            }:
                continue
            if ledger_has(campaign["day"], "engagement", candidate.get("profile_url", "")):
                candidate["status"] = "skipped_already_engaged_today"
                save_campaign(campaign)
                continue
            if int(candidate.get("attempts", 0)) >= int(config["max_attempts"]):
                candidate["status"] = "skipped_after_retry_cap"
                continue
            try:
                quotas = session.get_quotas()
                if int(quotas.get("profile_views_today", 0)) >= int(
                    quotas.get("profile_views_limit", 100)
                ):
                    paused_for_quota = True
                    campaign["stage"] = "paused_profile_view_limit"
                    save_campaign(campaign)
                    break
                inspect_candidate(cdp, sim, candidate, config)
                campaign["target_id"] = cdp.target_id
                from linkedin_helper import increment_counter

                increment_counter(session.state, "profile_views")
                if not candidate["engagement_eligible"]:
                    candidate["status"] = "inactive"
                    save_campaign(campaign)
                    continue
                eligible_posts = [
                    p
                    for p in candidate["posts"]
                    if p.get("age_hours") is not None
                    and p["age_hours"] <= int(config["activity_window_days"]) * 24
                    and not p["liked"]
                ][: int(candidate["likes_assigned"])]
                if execute:
                    completed = 0
                    for post in eligible_posts:
                        raise_if_paused(campaign)
                        execution_event(campaign, candidate, action="Verifying and liking post")
                        begin_action(campaign, candidate, "like", post["post_urn"])
                        if click_like(cdp, post["post_urn"]):
                            completed += 1
                            record_ledger_action(
                                campaign["day"],
                                "like",
                                candidate["profile_url"],
                                post_urn=post["post_urn"],
                                campaign_id=campaign.get("created_at", ""),
                            )
                            candidate["engagement_batch"] = int(batch["number"])
                            campaign.pop("pending_action", None)
                            save_campaign(campaign)
                            time.sleep(
                                random.uniform(
                                    float(config["action_delay_min_seconds"]),
                                    float(config["action_delay_max_seconds"]),
                                )
                            )
                        else:
                            campaign.pop("pending_action", None)
                            save_campaign(campaign)
                else:
                    completed = len(eligible_posts)
                if execute:
                    candidate["likes_completed"] = completed
                    candidate["status"] = "engaged" if completed else "no_eligible_unliked_posts"
                else:
                    candidate["likes_preview"] = completed
                    candidate["status"] = (
                        "dry_run_engagement_ready" if completed else "no_eligible_unliked_posts"
                    )
                if completed:
                    execution_event(
                        campaign,
                        candidate,
                        action="Assessing recent activity",
                        method="DOM preferred · shared activity reader",
                        reason="",
                    )
                    if execute:
                        record_ledger_action(
                            campaign["day"],
                            "engagement",
                            candidate["profile_url"],
                            campaign_id=campaign.get("created_at", ""),
                        )
                    engagement_progress += 1
                    if execute:
                        campaign["engaged"] += 1
                        batch_progress += 1
                        batch["engaged"] = batch_progress
                        candidate["engagement_batch"] = int(batch["number"])
                    else:
                        campaign["preview_engaged"] = engagement_progress
                save_campaign(campaign)
                if completed:
                    campaign["stage"] = "candidate_activity_assessment"
                    save_campaign(campaign)
                    assess_engaged_candidate(session, candidate, config)
                    save_campaign(campaign)
                    if execute and bool(config.get("obf_style_diversions", True)):
                        campaign["stage"] = "obf_style_diversion"
                        save_campaign(campaign)
                        run_obf_diversion(session, candidate)
                        save_campaign(campaign)
                    campaign["stage"] = "candidate_audit"
                    save_campaign(campaign)
            except (PermissionError, PauseRequested):
                raise
            except Exception as error:
                if campaign.get("pending_action"):
                    campaign.update(status="needs_reconciliation", stage="uncertain_action")
                    campaign["pending_action"]["error"] = str(error)
                    save_campaign(campaign)
                    return campaign
                candidate["attempts"] = int(candidate.get("attempts", 0)) + 1
                candidate["last_error"] = str(error)[:500]
                candidate["navigation_events"] = getattr(cdp, "post_engagement_navigation", [])
                campaign["target_id"] = cdp.target_id
                candidate["status"] = (
                    "failed"
                    if candidate["attempts"] < int(config["max_attempts"])
                    else "skipped_after_retry_cap"
                )
                save_campaign(campaign)

        # Each live batch is a self-contained engagement window.  Do not begin
        # the more conspicuous connection/follow stage until every scheduled
        # engagement window has completed.
        if (
            execute
            and batch_progress >= int(batch["target"])
            and engagement_progress < int(campaign["target"])
        ):
            batch.update(status="completed", engaged=batch_progress, completed_at=now().isoformat())
            next_batch = next(
                (
                    item
                    for item in campaign["engagement_batches"]
                    if int(item["number"]) > int(batch["number"])
                ),
                None,
            )
            if next_batch:
                delay = int(config.get("inter_batch_delay_minutes", 90))
                campaign.update(
                    status="waiting_next_batch",
                    stage="waiting_next_batch",
                    next_batch_at=(now() + timedelta(minutes=delay)).isoformat(),
                    current_batch_number=int(next_batch["number"]),
                )
                save_campaign(campaign)
                return campaign

        if execute and batch_progress >= int(batch["target"]):
            batch.update(status="completed", engaged=batch_progress, completed_at=now().isoformat())
            save_campaign(campaign)

        if execute and engagement_progress < int(campaign["target"]):
            campaign.update(
                status="paused_profile_view_limit" if paused_for_quota else "needs_another_post",
                stage="engagement_incomplete",
                engagement_deficit=int(campaign["target"]) - engagement_progress,
            )
            save_campaign(campaign)
            return campaign

        engaged = [
            c
            for c in campaign["candidates"]
            if c.get("status") in {"engaged", "dry_run_engagement_ready", "deferred_final_action"}
        ]
        campaign["stage"] = "recommendation_ranking"
        save_campaign(campaign)
        action_queue = final_action_queue(engaged, campaign)
        connection_progress = int(campaign.get("connections_sent", 0)) if execute else 0
        follow_progress = int(campaign.get("followed", 0)) if execute else 0
        connection_send_capacity = int(
            campaign.get("connection_send_capacity", campaign.get("connection_target", 0))
        )
        follow_send_capacity = int(
            campaign.get("follow_send_capacity", campaign.get("follow_target", 0))
        )
        campaign["stage"] = "final_actions"
        save_campaign(campaign)
        for index, candidate in enumerate(action_queue):
            raise_if_paused(campaign)
            action = (candidate.get("recommendation") or {}).get("action")
            execution_event(
                campaign, candidate, action=f"Preparing {action}", method="Direct URL", reason=""
            )
            if action == "follow":
                if follow_progress >= follow_send_capacity:
                    candidate["final_action_status"] = "deferred_daily_follow_cap"
                    defer_final_action(candidate, "follow", campaign)
                    save_campaign(campaign)
                    continue
                _navigate(cdp, candidate["profile_url"])
                if execute:
                    begin_action(campaign, candidate, "follow")
                    candidate["follow_result"] = (
                        "followed"
                        if follow_current_profile(cdp, candidate.get("name", ""))
                        else "follow_failed"
                    )
                    if candidate["follow_result"] == "followed":
                        campaign["followed"] += 1
                        follow_progress += 1
                        upsert_high_signal(candidate)
                        record_ledger_action(
                            campaign["day"],
                            "follow",
                            candidate["profile_url"],
                            campaign_id=campaign.get("created_at", ""),
                        )
                        resolve_deferred_action("follow", candidate["profile_url"])
                        campaign.pop("pending_action", None)
                    else:
                        campaign.update(status="needs_reconciliation", stage="follow_unconfirmed")
                        save_campaign(campaign)
                        return campaign
                else:
                    candidate["follow_preview"] = "would_follow"
                    follow_progress += 1
            elif action == "connect" and execute:
                if connection_progress >= connection_send_capacity:
                    candidate["final_action_status"] = "deferred_daily_connection_cap"
                    defer_final_action(candidate, "connect", campaign)
                    save_campaign(campaign)
                    continue
                begin_action(campaign, candidate, "connect")
                result = session.send_connection_only(candidate["profile_url"])
                sent = bool(result.get("success"))
                if execute:
                    candidate["connection_result"] = result
                if sent:
                    connection_progress += 1
                    campaign["connections_sent"] += 1
                    record_ledger_action(
                        campaign["day"],
                        "connect",
                        candidate["profile_url"],
                        campaign_id=campaign.get("created_at", ""),
                    )
                    resolve_deferred_action("connect", candidate["profile_url"])
                    campaign.pop("pending_action", None)
                else:
                    campaign.update(status="needs_reconciliation", stage="connection_unconfirmed")
                    save_campaign(campaign)
                    return campaign
            elif action == "connect":
                if connection_progress >= connection_send_capacity:
                    candidate["final_action_status"] = "deferred_daily_connection_cap"
                    # Audit previews must not populate the live deferred queue.
                    save_campaign(campaign)
                    continue
                result = {"status": "dry_run_would_send"}
                candidate["connection_preview"] = result
                connection_progress += 1
                campaign["preview_connections"] = connection_progress
            candidate["final_action_attempted_at"] = now().isoformat()
            save_campaign(campaign)
            remaining_actionable = any(
                (
                    (later.get("recommendation") or {}).get("action") == "connect"
                    and connection_progress < connection_send_capacity
                )
                or (
                    (later.get("recommendation") or {}).get("action") == "follow"
                    and follow_progress < follow_send_capacity
                )
                for later in action_queue[index + 1 :]
            )
            if execute and remaining_actionable:
                delay = random.uniform(
                    float(config["final_action_delay_min_seconds"]),
                    float(config["final_action_delay_max_seconds"]),
                )
                campaign["next_action_delay_seconds"] = round(delay, 2)
                save_campaign(campaign)
                time.sleep(delay)

        engagement_deficit = max(0, campaign["target"] - engagement_progress)
        connection_deficit = max(0, connection_send_capacity - connection_progress)
        complete = not engagement_deficit and not connection_deficit
        final_status = (
            "paused_profile_view_limit"
            if paused_for_quota
            else (
                ("dry_run_complete" if complete else "dry_run_needs_another_post")
                if not execute
                else ("completed" if complete else "needs_another_post")
            )
        )
        final_stage = (
            "paused_profile_view_limit"
            if paused_for_quota
            else (
                "dry_run_complete"
                if not execute and complete
                else "complete"
                if complete
                else "needs_more_sources"
            )
        )
        campaign.update(
            stage=final_stage,
            status=final_status,
            engagement_deficit=engagement_deficit,
            connection_deficit=connection_deficit,
            connection_send_capacity_remaining=max(
                0, connection_send_capacity - connection_progress
            ),
            follow_capacity_remaining=max(0, follow_send_capacity - follow_progress),
            completed_at=now().isoformat(),
        )
        save_campaign(campaign)
        append_history(
            {
                "day": campaign["day"],
                "completed_at": campaign["completed_at"],
                "status": campaign["status"],
                "target": campaign["target"],
                "engaged": campaign["engaged"],
                "connections_sent": campaign["connections_sent"],
                "followed": campaign["followed"],
                "dry_run": campaign["dry_run"],
            }
        )
        return campaign
    except PauseRequested:
        campaign.update(status="paused", stage="paused", paused_at=now().isoformat())
        save_campaign(campaign)
        return campaign
    except Exception as error:
        campaign["status"] = "failed"
        campaign["errors"].append(
            {"at": now().isoformat(), "stage": campaign.get("stage"), "message": str(error)[:1000]}
        )
        save_campaign(campaign)
        raise
    finally:
        if cdp:
            cdp.disconnect()


@exclusive_campaign
def run_campaign_schedule(day: str | None, execute: bool) -> dict[str, Any]:
    """Run live engagement windows through their persisted, pause-aware gaps."""
    while True:
        previous = load_campaign(day)
        if execute:
            ACTION_ACCOUNT.set(previous.get("cdp_account") or load_config()["cdp_account"])
            reconcile_campaign(previous)
            if previous.get("pending_action"):
                previous.update(status="needs_reconciliation", stage="uncertain_action")
                save_campaign(previous)
                return previous
        if execute and previous.get("next_batch_at"):
            if not wait_for_next_batch(previous):
                return load_campaign(previous["day"])
        campaign = run_campaign(day, execute)
        if not execute or campaign.get("status") != "waiting_next_batch":
            return campaign
        if not wait_for_next_batch(campaign):
            return load_campaign(campaign["day"])
        # The next call reconnects to the CDP instead of holding a browser
        # protocol connection open for ninety minutes.
        day = campaign["day"]


def dashboard(day: str | None = None) -> dict[str, Any]:
    campaign = load_campaign(day)
    config = load_config()
    history = []
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-30:]:
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {
        "is_running": runner_active(),
        "config": config,
        "campaign": campaign,
        "control": campaign_control(campaign["day"]),
        "high_signal": read_json(HIGH_SIGNAL_PATH, {"profiles": []}).get("profiles", []),
        "history": list(reversed(history)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--day")
    source = sub.add_parser("add-source")
    source.add_argument("url")
    source.add_argument("--day")
    source.add_argument("--cdp-account", choices=sorted(CDP_ACCOUNTS))
    configure = sub.add_parser("configure")
    configure.add_argument("json")
    pause = sub.add_parser("pause")
    pause.add_argument("--day")
    resume = sub.add_parser("resume")
    resume.add_argument("--day")
    archive = sub.add_parser("archive-and-fresh")
    archive.add_argument("--day")
    archive.add_argument("--reason", default="fresh_validation_run")
    run = sub.add_parser("run")
    run.add_argument("--day")
    run.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            dashboard(args.day)
            if args.command == "status"
            else add_source(args.url, args.day, args.cdp_account)
            if args.command == "add-source"
            else save_config(json.loads(args.json))
            if args.command == "configure"
            else pause_campaign(args.day)
            if args.command == "pause"
            else resume_campaign(args.day)
            if args.command == "resume"
            else archive_and_start_fresh(args.day, args.reason)
            if args.command == "archive-and-fresh"
            else run_campaign_schedule(args.day, args.execute)
        )
        print(json.dumps(result, indent=2))
        return (
            2
            if args.command == "run"
            and result.get("status")
            not in {"completed", "dry_run_complete", "paused", "waiting_next_batch"}
            else 0
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
