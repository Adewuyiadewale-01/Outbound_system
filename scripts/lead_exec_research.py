#!/usr/bin/env python3
"""Compatibility entry point for the lead executive research workflow.

All logic lives under outbound/leads/* (the leads carve — see docs/CARVE-LEADS.md).
This file preserves the historical invocation surface — path, CLI, and module
import — for its consumers (sibling scripts, tests, and the activity_check bridge).

    python3 scripts/lead_exec_research.py --help
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
SCRIPTS = ROOT / "scripts"
for _path in (str(HELPERS), str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- leads carve: shim re-exports (appended per slice) ---
from runtime_environment import load_repo_env  # noqa: E402

from outbound.leads.archive import (  # noqa: F401
    MATCH_AVAILABLE,
    MATCH_CONFLICT,
    MATCH_CONSUMED,
    MATCH_FRESH,
    ResearchArchive,
    has_reusable_research,
    hydrate_computation_lead,
)
from outbound.leads.bridge import (  # noqa: F401
    bridge_date_key,
    bridge_date_value,
    bridge_prefinal_to_prospects,
    bridge_state_path,
    existing_prospect_ids,
    first_prospect_write_row,
    parse_bridge_date,
    pick_engaged_person,
    prefinal_bridge_fingerprint,
    prefinal_rows_for_bridge,
    prospect_row_from_prefinal,
    record_outreach_control_prospects_start_row,
    write_prospect_rows,
)
from outbound.leads.cli import add_common_args, main, validate_args  # noqa: F401
from outbound.leads.computation import (  # noqa: F401
    archive_computation_state,
    archive_unreviewed_computation,
    consume_reused_archive_entries,
)
from outbound.leads.config import (  # noqa: F401
    BRIDGES_DIR,
    COMPUTATIONS_DIR,
    DEFAULT_CREDS,
    DEFAULT_DESTINATION_TAB,
    DEFAULT_FINAL_TAB,
    DEFAULT_NOTIFICATION_QUEUE_TAB,
    DEFAULT_NOTIFY_EMAIL,
    DEFAULT_OBF_SHEET_URL,
    DEFAULT_OUTREACH_CONTROL_TAB,
    DEFAULT_PROSPECTS_TAB,
    DEFAULT_RESEARCH_ARCHIVE_INDEX,
    DEFAULT_REVIEW_TAB,
    DEFAULT_SHEET_URL,
    DEFAULT_SOURCE_TAB,
    DESTINATION_COLUMNS,
    EXEC_TITLE_PATTERNS,
    FINAL_BRIDGE_COLUMNS,
    LEAD_PREP_CONFIG_PATH,
    NOTIFICATION_QUEUE_COLUMNS,
    OPENCLAW_CREDS,
    OPTIONAL_DESTINATION_COLUMNS,
    OUTREACH_CONTROL_PROSPECTS_START_ROW,
    PRIMARY_LANES,
    PROMPTS_DIR,
    PROSPECTS_COLUMNS,
    REPO_CREDS,
    RESEARCH_ARCHIVE_DIR,
    REVIEW_COLUMNS,
    REVIEW_OVERLAP_COLUMNS,
    ROLE_KEYWORDS,
    RUNS_DIR,
    SEARCH_RESULTS_DIR,
    SEARCH_TASKS_DIR,
    SNAPSHOTS_DIR,
    SOURCE_ALIASES,
    SOURCE_COLUMNS,
    STATE_DIR,
)
from outbound.leads.destination import (  # noqa: F401
    build_destination_row,
    build_destination_row_from_computation,
    choose_people,
    computation_rows_for_write,
    filter_ready_prefinal_rows,
    prefinal_row_readiness,
    select_computation_people,
    verify_destination_rows,
    write_computation_rows,
    write_destination_rows,
)
from outbound.leads.extract import (  # noqa: F401
    employee_role_score,
    extract_exec_candidates,
    extract_name_role_pairs,
    fallback_execs_from_employees,
    match_employee,
    reconcile_exec,
    score_search_candidate,
    search_execs_for_company,
    search_linkedin_for_exec,
    seniority_score,
)
from outbound.leads.gates import (  # noqa: F401
    approved_gate_status,
    approved_workflow_status,
    claim_approved_gate,
    computation_status_counts,
    mark_approved_gate,
    next_approved_action,
)
from outbound.leads.grouping import (  # noqa: F401
    add_employee,
    assign_primary_lanes,
    chunks,
    group_source_rows,
    looks_like_company_row,
    looks_like_employee_row,
)
from outbound.leads.notify import (  # noqa: F401
    enqueue_review_notification,
    export_pending_search_tasks,
    notify_review_with_fallback,
    review_email_body,
    review_email_subject,
    send_review_email,
)
from outbound.leads.reviewtab import (  # noqa: F401
    build_review_row,
    checkbox_truthy,
    ensure_review_tab,
    existing_successful_review_group_row,
    find_successful_review_group_row_for_today,
    get_or_create_worksheet,
    last_nonempty_review_row,
    parse_review_group_date,
    read_review_rows,
    remove_review_group_for_run,
    review_group_date_value,
    update_review_statuses,
    write_review_rows,
)
from outbound.leads.runs import (  # noqa: F401
    apply_review_slice,
    approval_gate_enabled,
    archive_index_path,
    computation_fingerprint,
    destination_sheet_url,
    ensure_dirs,
    latest_computation_file,
    latest_computation_file_for_fingerprint,
    latest_review_run_file,
    latest_run_file,
    latest_search_result_file,
    lead_id_fingerprint,
    load_run,
    now_run_id,
    parse_review_slice,
    resolve_ignore_review_approval,
    save_run,
    source_sheet_url,
)
from outbound.leads.search import (  # noqa: F401
    SearchClient,
    parse_bing_results,
    parse_duckduckgo_results,
    parse_yahoo_results,
    search_query_variants,
    strip_tags,
    unwrap_duckduckgo_url,
    unwrap_yahoo_url,
)
from outbound.leads.sheetsio import (  # noqa: F401
    canonicalize_source_rows,
    load_destination_company_by_id,
    load_destination_ids,
    load_reviewed_ids,
    read_worksheet,
    require_columns,
    source_value,
)
from outbound.leads.text import (  # noqa: F401
    clean_person_name,
    clean_role_for_person,
    clean_text,
    compact_company_name,
    is_linkedin_profile_url,
    is_plausible_person_name,
    linkedin_url_name_score,
    meaningful_name_parts,
    normalize_key,
    normalize_url,
    strip_accents,
    title_weight,
    token_overlap_score,
)
from outbound.leads.workflow import (  # noqa: F401
    annotate_overlap_scan,
    apply_research_results,
    apply_search_result_batch,
    approved_leads_from_review,
    build_research_prompt,
    build_run,
    collect_overlap_top_up_waves,
    filter_run_to_approved,
    find_executive_for_task,
    freeze_approved,
    normalize_research_executives,
    notify_review,
    preflight,
    prepare_review,
    print_summary,
    process_approved_run,
    process_run,
    push_run,
    reconcile_computation_existing_data,
    run_search_tasks,
    score_task_candidate,
    write_research_prompt,
)

load_repo_env()


if __name__ == "__main__":
    raise SystemExit(main())
