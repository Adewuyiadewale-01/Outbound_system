"""Campaign state, action ledger, and control plane for engagement."""

from __future__ import annotations

import fcntl
import functools
import inspect
import re
import time
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from outbound.engagement.assessment import (
    ACTIVITY_ASSESSMENT_VERSION,
    PROFILE_PARSER_VERSION,
)
from outbound.engagement.config import CDP_ACCOUNTS, load_config
from outbound.engagement.paths import (
    ARCHIVE_DIR,
    CAMPAIGNS_DIR,
    CONTROL_PATH,
    LEDGER_PATH,
    PENDING_ACTIONS_PATH,
    RUNNER_LOCK_PATH,
    STATE_DIR,
)
from outbound.shared.state import daily_rng, now, read_json, write_json

ACTION_ACCOUNT = ContextVar("post_engagement_account", default=None)


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


@exclusive_campaign
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
