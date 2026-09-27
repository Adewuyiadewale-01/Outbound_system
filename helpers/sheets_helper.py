#!/usr/bin/env python3
"""Compatibility shim: shared Sheets access lives in outbound/shared/sheets.py.

Preserved for the scripts/ and ORCHESTRATION/ consumers that still import the
historical name (see docs/CARVE-LEADS.md, Stage 3).
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from outbound.shared.sheets import (  # noqa: F401
    APPROVAL_BLOCK_PATTERNS,
    APPROVAL_SIGNAL_PATTERNS,
    DAILY_ACTIONS_TAB,
    DAILY_APPROVAL_COLUMN,
    INBOX_TAB,
    INBOX_TO_DAILY_FIELDS,
    INBOX_TO_DAILY_REMAP,
    SCOPES,
    SPREADSHEET_SERIAL_EPOCH,
    WEEKLY_TARGETS_TAB,
    _maybe_float,
    _update_daily_status,
    append_row,
    build_daily_approval_packet,
    complete_daily_row,
    create_daily_group,
    create_weekly_group,
    daily_approval_state,
    detect_approval_intent,
    fill_grouped_rows,
    find_date_group_bounds,
    find_group_last_content_row,
    find_week_group_bounds,
    format_sheet_date,
    format_sheet_time,
    get_client,
    get_daily_group_data,
    get_last_meaningful_row,
    get_worksheet,
    insert_rows_by_headers,
    is_checked_value,
    list_tabs,
    main,
    normalize_message_text,
    normalize_owner_label,
    normalize_rows,
    open_sheet,
    parse_sheet_date,
    parse_sheet_time,
    partial_daily_row,
    process_daily_approval_reply,
    promote_inbox_row,
    read_tab,
    recompute_weekly_progress,
    require_columns,
    roll_daily_row,
    row_exists,
    safe_number,
    set_daily_group_approval,
    sheet_values_equal,
    skip_daily_row,
    update_row,
)

if __name__ == "__main__":
    from outbound.shared.sheets import main

    raise SystemExit(main())
