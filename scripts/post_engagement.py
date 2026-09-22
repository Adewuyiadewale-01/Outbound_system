#!/usr/bin/env python3
"""Resumable LinkedIn source-post engagement workflow.

The default `run` is a read-only dry run. Real Likes, Follows and connection
requests require the explicit `--execute` switch.
"""

from __future__ import annotations

import sys
from datetime import datetime  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "helpers"))
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_environment import load_repo_env

from outbound.engagement.assessment import (  # noqa: F401
    ACTIVITY_ASSESSMENT_VERSION,
    PROFILE_PARSER_VERSION,
    activity_counts,
    qualifies,
    recommendation_for,
    validate_profile_gate,
)
from outbound.engagement.batches import (  # noqa: F401
    choose_like_target,
    current_engagement_batch,
    ensure_engagement_batches,
)
from outbound.engagement.browser import (  # noqa: F401
    POST_CARDS_JS,
    PROFILE_GATE_JS,
    SOURCE_REACTOR_BOOTSTRAP_JS,
    SOURCE_REACTOR_SNAPSHOT_JS,
    _connect_campaign_browser,
    _evaluate_json,
    _navigate,
    append_history,
    assess_engaged_candidate,
    click_like,
    collect_sources,
    ensure_obf_diversions,
    execution_event,
    follow_current_profile,
    inspect_candidate,
    review_recent_activity,
    run_obf_diversion,
    upsert_high_signal,
)
from outbound.engagement.campaign import (  # noqa: F401
    ACTION_ACCOUNT,
    PauseRequested,
    action_account,
    add_source,
    archive_and_start_fresh,
    campaign_control,
    campaign_lock,
    campaign_path,
    defer_final_action,
    exclusive_campaign,
    final_action_queue,
    ledger_count,
    ledger_has,
    load_campaign,
    new_campaign,
    pause_campaign,
    raise_if_paused,
    ranking_key,
    read_daily_ledger,
    read_pending_actions,
    record_ledger_action,
    resolve_deferred_action,
    resume_campaign,
    runner_active,
    save_campaign,
    wait_for_next_batch,
)
from outbound.engagement.cli import (  # noqa: F401
    dashboard,
    main,
)
from outbound.engagement.config import (  # noqa: F401
    CDP_ACCOUNTS,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    load_config,
)
from outbound.engagement.parsing import (  # noqa: F401
    canonical_profile_url,
    classify_location,
    parse_follower_count,
    parse_relative_age_hours,
)
from outbound.engagement.runner import (  # noqa: F401
    begin_action,
    reconcile_campaign,
    run_campaign,
    run_campaign_schedule,
    save_config,
)
from outbound.shared.state import (  # noqa: F401
    daily_rng,
    now,
    read_json,
    write_json,
)

load_repo_env()

if __name__ == "__main__":
    raise SystemExit(main())