"""Engagement workflow configuration."""

from __future__ import annotations

from typing import Any

from outbound.engagement.paths import STATE_DIR
from outbound.shared.state import read_json

CONFIG_PATH = STATE_DIR / "config.json"
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
    "source_collection_cap": 200,
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
CDP_ACCOUNTS = {
    "design": {"host": "127.0.0.1", "port": 18800},
    "automation": {"host": "127.0.0.1", "port": 18801},
}


def load_config() -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **read_json(CONFIG_PATH, {})}
