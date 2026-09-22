"""Campaign execution: reconciliation, the run loop, and scheduling."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any

from outbound.engagement.assessment import PROFILE_PARSER_VERSION
from outbound.engagement.batches import current_engagement_batch
from outbound.engagement.browser import (
    _connect_campaign_browser,
    _navigate,
    append_history,
    assess_engaged_candidate,
    click_like,
    collect_sources,
    ensure_obf_diversions,
    execution_event,
    follow_current_profile,
    inspect_candidate,
    run_obf_diversion,
    upsert_high_signal,
)
from outbound.engagement.campaign import (
    ACTION_ACCOUNT,
    PauseRequested,
    action_account,
    campaign_control,
    defer_final_action,
    exclusive_campaign,
    final_action_queue,
    ledger_has,
    load_campaign,
    raise_if_paused,
    read_daily_ledger,
    record_ledger_action,
    resolve_deferred_action,
    save_campaign,
    wait_for_next_batch,
)
from outbound.engagement.config import (
    CDP_ACCOUNTS,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    load_config,
)
from outbound.shared.state import daily_rng, now, write_json


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
