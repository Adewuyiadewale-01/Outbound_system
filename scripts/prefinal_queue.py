"""Compatibility shim: the finalized-batch queue lives in outbound/shared/queue.py."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from outbound.shared.queue import (  # noqa: F401
    OPEN_STATUSES,
    QUEUE_DIR,
    QUEUE_ROW_COLUMNS,
    ROOT,
    _atomic_write,
    batch_path,
    canonical_rows,
    clean_text,
    enqueue_batch,
    list_batches,
    load_batch,
    next_activity_batch,
    queue_dir,
    record_prefinal_publish,
    rows_fingerprint,
    save_batch,
    update_batch_status,
)
