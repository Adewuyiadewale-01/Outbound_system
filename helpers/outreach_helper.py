#!/usr/bin/env python3
"""Compatibility shim for the outreach helper.

All logic lives in outbound/outreach/{config,sheetops}.py (the outreach_helper
carve). This preserves the historical import surface and the CLI:

    python3 helpers/outreach_helper.py load-queue --limit 30
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from outbound.shared.env import load_repo_env

load_repo_env()

from outbound.outreach.config import (  # noqa: F401
    _DEFAULT_CREDS_PATH,
    _OPENCLAW_CREDS_PATH,
    ALLOWED_ACTIVITY_VALUES,
    CREDS_PATH,
    DAILY_METRICS_TAB,
    ENRICHMENT_SHEET_URL,
    LOG_ACTION_MAP,
    OBF_SHEET_URL,
    OUTREACH_LOG_TAB,
    PIPELINE_TAB,
    PROSPECTS_TAB,
    ROOT_DIR,
    SKIP_CONN_REQ_STATUSES,
    STATUS_CONN_SENT,
    STATUS_CONNECTED,
    STATUS_FIRST_MSG,
    STATUS_FOLLOWING_UP,
    STATUS_MEETING,
    STATUS_NO_RESPONSE,
    STATUS_NOT_INTERESTED,
    STATUS_QUEUED,
    STATUS_REPLIED,
    STATUS_REQUIRES_EMAIL,
    STATUS_WITHDRAWN,
    TEMPLATES_TAB,
)
from outbound.outreach.sheetops import (  # noqa: F401
    _CATEGORY_ALIASES,
    _activity_column_name,
    _normalize_activity_value,
    _normalize_log_action,
    _normalize_profile_url,
    _pipeline_row_from_acceptance,
    _prospect_from_log_row,
    _raw_prospects_by_id,
    append_pipeline_row_for_acceptance,
    apply_outreach_log_fields,
    apply_prospect_fields,
    build_connection_sent_fields,
    build_outreach_log_row,
    build_prospect_activity_fields,
    build_template_variables,
    count_outreach_log_connection_requests,
    count_pipeline_connected_leads,
    get_connected_needing_first_message,
    get_pending_connections,
    get_stale_pending_connections,
    insert_outreach_log_row,
    load_pending_outreach_log_connections,
    load_prospect_queue,
    load_prospects_by_status,
    load_templates,
    log_activity_review,
    log_daily_metrics,
    log_outreach_event,
    log_outreach_event_from_prospect,
    main,
    mark_connected,
    mark_connection_sent,
    mark_outreach_log_connected,
    mark_withdrawn,
    pick_connection_template,
    record_connection_sent,
    render_template,
    update_prospect_status,
    write_prospect_activity,
)
from outbound.shared.sheets import (  # noqa: F401
    append_row,
    get_client,
    get_worksheet,
    open_sheet,
    read_tab,
    update_row,
)

if __name__ == "__main__":
    from outbound.outreach.sheetops import main

    raise SystemExit(main())
