"""Engagement batch planning for the daily target."""

from __future__ import annotations

from typing import Any

from outbound.shared.state import daily_rng


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
