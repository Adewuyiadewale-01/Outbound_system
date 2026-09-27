#!/usr/bin/env python3
"""Standalone Pre-final activity checker with staged Final and Prospects bridges."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for _path in (str(HELPERS), str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- activity_check carve: shim re-exports (appended per slice) ---
from runtime_environment import load_repo_env  # noqa: E402

from outbound.activity_check.analysis import (  # noqa: F401
    activity_detail_empty_success_reason,
    activity_detail_should_defer,
    activity_evidence,
    activity_level,
    activity_score,
    category_for_row,
    count_entries_within,
    detail_contains_text,
    final_sort_key,
    is_aggregate_activity_container,
    non_aggregate_entries,
    rank_row_for_final,
    should_swap,
)
from outbound.activity_check.cli import (  # noqa: F401
    fake_activity,
    main,
    parse_args,
    run_self_tests,
)
from outbound.activity_check.config import (  # noqa: F401
    ACTIVITY_COLUMNS,
    ACTIVITY_SCORE,
    ACTIVITY_VALUES,
    BASE_COLUMNS,
    BLOCKING_DANGER_PATTERNS,
    CATEGORY_PRIORITY,
    DEFAULT_ACTIVITY_SEQUENCE_TAB,
    DEFAULT_CREDS,
    DEFAULT_FINAL_TAB,
    DEFAULT_LEADS_SHEET_URL,
    DEFAULT_MAX_TARGET_ATTEMPTS,
    DEFAULT_PREFINAL_TAB,
    DEFER_ACTIVITY_REASONS,
    DIVERSION_OPTIONS,
    FINAL_REQUIRED_COLUMNS,
    HARD_READ_DANGERS,
    HARD_READ_REASONS,
    JOURNAL_DIR,
    NAVIGATION_TYPE_OPTIONS,
    OPENCLAW_CREDS,
    P1_COLUMNS,
    P2_COLUMNS,
    PENDING_404_STATUS,
    PERSON_COLUMNS,
    REPO_CREDS,
    SKIP_ACTIVITY_REASONS,
    STATE_DIR,
)
from outbound.activity_check.danger import (  # noqa: F401
    blocked_result,
    hard_read_failure_reason,
    is_blocking_danger,
    is_cdp_transport_exception,
)
from outbound.activity_check.diversion import apply_diversion  # noqa: F401
from outbound.activity_check.finalize import (  # noqa: F401
    bridge_final_to_prospects,
    bridge_ready_rows_to_final,
    final_source_row_count,
    finalize_prepared_session,
    no_queued_batch_result,
    queue_source_rows,
    ready_final_rows,
    resume_final_bridged_batch,
    row_activity_from_state,
    row_targets,
    target_has_activity_decision,
    write_final_batch_upsert_and_sort,
)
from outbound.activity_check.reader import (  # noqa: F401
    LiveActivityReader,
    build_fixture_reader,
    build_synthetic_activity_reader,
)
from outbound.activity_check.retry import (  # noqa: F401
    next_activity_retry_record,
    persist_activity_retry_attempt,
    persist_terminal_activity_issue,
)
from outbound.activity_check.runner import run  # noqa: F401
from outbound.activity_check.sequence import (  # noqa: F401
    distribute_slots_to_batches,
    ensure_activity_sequence,
    generated_activity_timing,
    generated_batch_gaps,
    generated_delay_seconds,
    generated_diversion_seconds,
    generated_diversions,
    generated_navigation_types,
    resolve_activity_sequence_columns,
    unique_ints_in_order,
)
from outbound.activity_check.sheets import person_from_row, read_worksheet, set_person  # noqa: F401
from outbound.activity_check.state import (  # noqa: F401
    activity_journal_path,
    activity_session_path,
    append_jsonl,
    emit_progress,
    journal_event,
    load_session_state,
    prepare_session_state,
    save_session_state,
)
from outbound.activity_check.targets import extract_targets  # noqa: F401
from outbound.activity_check.text import (  # noqa: F401
    _parse_positive_int,
    canonical_linkedin_profile_url,
    clean_text,
    is_linkedin_profile_url,
    navigation_type_key,
    normalize_activity_value,
    normalize_navigation_type,
    normalize_url,
    normalized_profile_key,
    target_key,
)

load_repo_env()


# A LinkedIn route can occasionally land on a visually blank shell: URL is loaded,
# CDP is reachable, but the app never hydrates. That is a per-profile/page load
# problem, not a reason to kill the whole activity lane.


if __name__ == "__main__":
    raise SystemExit(main())
