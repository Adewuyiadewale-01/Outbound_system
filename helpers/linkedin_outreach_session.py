#!/usr/bin/env python3
"""
Deterministic runner for the 8:30 AM LinkedIn outreach block.

The runner keeps the cron agent out of low-level decision-making:
- Outreach approval, targets, rollover, and progress stay in Operation Brute Force -> Outreach Control.
- Prospect status/logging stays in outreach_helper.py.
- Browser actions and quota state stay in linkedin_helper.py.
"""

import sys
from pathlib import Path

from runtime_environment import load_repo_env

load_repo_env()

HELPERS_DIR = Path(__file__).resolve().parent
ROOT_DIR = HELPERS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

sys.path.insert(0, str(HELPERS_DIR))


from outbound.outreach.acceptance import (  # noqa: F401
    _acceptance_fingerprint,
    _build_pending_indexes,
    _is_active_hour,
    _load_acceptance_state,
    _load_pending_queue,
    _match_pending_acceptance,
    _pick_first_message_template,
    _prune_seen_acceptances,
    _render_first_message_draft,
    _save_acceptance_state,
    _schedule_acceptance_check,
    acceptance_check,
)
from outbound.outreach.activity import (  # noqa: F401
    _activity_classification_uncertain,
    _activity_dropdown_value,
    _activity_summary,
    _build_requires_email_fields,
    _live_prospect_activity_value,
    _read_and_sync_activity,
    _reconcile_profile_state,
    _record_requires_email_status,
    _short_skip_dwell,
)
from outbound.outreach.control_sheet import (  # noqa: F401
    _append_auto_created_control_row,
    _control_lane_targets,
    _control_prospects_start_row,
    _control_target,
    _ensure_outreach_control_tab,
    _extract_target,
    _find_conn_req_row,
    _latest_successful_control_row,
    _normalize_control_status,
    _read_outreach_control,
    _update_outreach_control_progress,
    configure_outreach_control,
)
from outbound.outreach.guards import (  # noqa: F401
    _ensure_session_healthy,
    _is_profile_ui_failure,
    _normalize_failure_key,
    _outreach_ui_sample_size,
    _record_early_profile_guard,
    _register_browser_failure,
    _register_send_failure,
    _reset_browser_failure_guard,
    _reset_send_failure_guard,
    _run_connection_modal_checks,
    _run_outreach_ui_preflight,
    _session_cdp_health_check,
    _sleep_failure_backoff,
)
from outbound.outreach.journal import (  # noqa: F401
    _append_jsonl,
    _confirmed_send_key,
    _emit_outreach_progress,
    _journal_confirmed_send_records,
    _journal_event,
    _journal_event_matches_prospect,
    _journal_has_confirmed_send,
    _journal_pending_sync_count,
    _pending_sync_payload_for_confirmed_send,
    _prospect_identity,
    _read_journal_events,
    _read_journal_events_for_prospect,
    _reconcile_result_from_journal,
    _record_breadcrumb,
    _record_journal_event,
    _sheet_target_payload,
    journal_path,
    prepared_session_path,
    sequence_path,
)
from outbound.outreach.lanes import (  # noqa: F401
    _assign_outreach_workers,
    _auto_assign_missing_primary_lanes,
    _balanced_lane_targets,
    _persist_primary_lane_assignments,
    _read_outreach_workers,
    _select_queue_for_lane_targets,
)
from outbound.outreach.lead_machine import (  # noqa: F401
    _process_outreach_lead_state_machine,
)
from outbound.outreach.paths import (  # noqa: F401
    ACCEPTANCE_STATE_DIR,
    ACCEPTANCE_STATE_FILE,
    DEFAULT_CREDS,
    JOURNAL_DIR,
    OBF_SHEET_URL,
    OUTREACH_WORKERS_CONFIG_PATH,
    STATE_DIR,
    TASK_MANAGER_URL,
    _resolve_default_creds,
)
from outbound.outreach.planning import (  # noqa: F401
    _normalize_activity_timing,
    _random_positive_partition,
    _runtime_plan_from_generated_sequence,
    generate_activity_timing_plan,
    generate_batch_size_plan,
    generate_delay_seconds_plan,
    generate_sequence,
    load_or_generate_sequence,
)
from outbound.outreach.policy import (  # noqa: F401
    ACTIVITY_READ_TIMEOUT_SEC,
    CDP_HEALTH_EVAL_TIMEOUT_SEC,
    DAILY_CONN_REQ_LIMIT,
    DEFAULT_LANE_SPLIT_MARKER,
    DEFAULT_LANE_TARGETS,
    DEFAULT_TARGET,
    EMAIL_REQUIRED_TO_CONNECT,
    FAILURE_BACKOFF_MAX_SEC,
    FAILURE_BACKOFF_MIN_SEC,
    HARD_STOP_ERRORS,
    MAX_BROWSER_RECOVERY_ATTEMPTS,
    MAX_CONSECUTIVE_SEND_FAILURES,
    MAX_SAME_BROWSER_ERROR_STREAK,
    MAX_SAME_SEND_ERROR_STREAK,
    NO_CONNECT_BUTTON_RETRIES,
    NO_CONNECT_BUTTON_RETRY_MAX_SEC,
    NO_CONNECT_BUTTON_RETRY_MIN_SEC,
    NON_FATAL_SEND_ERRORS,
    OUTREACH_CONTROL_APPROVED_STATUSES,
    OUTREACH_CONTROL_HEADERS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    OUTREACH_CONTROL_TAB,
    OUTREACH_CONTROL_TERMINAL_STATUSES,
    OUTREACH_SEQUENCE_TAB,
    OUTREACH_STATUS_REQUIRES_EMAIL,
    PROFILE_UI_GUARD_ERRORS,
    PROFILE_UNKNOWN_RETRIES,
    PROFILE_UNKNOWN_RETRY_MAX_SEC,
    PROFILE_UNKNOWN_RETRY_MIN_SEC,
    QUEUE_BUFFER_LIMIT,
    RETRYABLE_CONNECT_ERRORS,
    WEEKLY_CONN_REQ_LIMIT,
)
from outbound.outreach.runner import (  # noqa: F401
    _approval_and_task,
    _extract_sheet_id,
    _is_obf_weekend,
    _load_prepared_session,
    _load_queue,
    _load_quotas,
    _prospect_reporting_missing_fields,
    _quota_capacity,
    _read_json,
    _recover_confirmed_send_sheet_sync,
    _verify_sheet_url_identity,
    _weekend_hold_message,
    prepare_8_30_session,
    run,
)
from outbound.outreach.sequence_sheet import (  # noqa: F401
    _load_runtime_plan_from_sequence_sheet,
    _resolve_sequence_columns,
    generate_activity_timing_from_sheet,
    generate_batch_sizes_from_sheet,
    generate_delay_seconds_from_sheet,
    generate_lead_diversion_seconds_from_sheet,
    generate_lead_diversions_from_sheet,
)
from outbound.shared.dates import (  # noqa: F401
    _parse_date,
    sequence_date_key,
    sheet_date,
)
from outbound.shared.diversion import (  # noqa: F401
    _diversion_range,
    _first_post_url,
    _run_diversion,
    generate_lead_diversion_plan,
    generate_lead_diversion_seconds_plan,
)
from outbound.shared.sheetutils import (  # noqa: F401
    _a1_cell,
    _batch_update_cells,
    _clamped_gauss,
    _col_to_a1,
    _find_first_index,
    _find_last_index,
    _is_enabled,
    _normalize_person_name,
    _normalize_profile_url,
    _parse_int,
    _parse_iso_datetime,
)
