#!/usr/bin/env python3
"""Freeze the test Prospects source into the normal OBF prepared-state shape."""

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helpers"))
from linkedin_outreach_session import _assign_outreach_workers
from outreach_helper import CREDS_PATH, OBF_SHEET_URL
from sheets_helper import read_tab

day = date.today().isoformat()
rows = read_tab(CREDS_PATH, OBF_SHEET_URL, "Prospects - Test")["rows"]
queue = []
for row in rows:
    engaged = str(row.get("Engaged Person") or "Person 1").strip()
    prefix = "P2" if engaged == "Person 2" else "P1"
    url = str(row.get(f"{prefix} LinkedIn") or "").strip()
    if url and not url.startswith("http"):
        url = "https://www." + url
    if url:
        queue.append(
            {
                "id": str(row.get("ID") or ""),
                "company": str(row.get("Company") or ""),
                "primary_lane": str(row.get("Primary Lane") or ""),
                "contact_linkedin": url,
                "contact_name": str(row.get(f"{prefix} Name") or ""),
                "_row_number": row.get("_row_number"),
            }
        )
worker_plan = _assign_outreach_workers(queue)
payload = {
    "date": day,
    "ready": True,
    "test_only": True,
    "source": {"prospects_tab": "Prospects - Test", "test_mode": True},
    "prepared_at": datetime.now().isoformat(timespec="seconds"),
    "queue": queue,
    "runtime_plan": [{"slot_id": index + 1} for index in range(len(queue))],
    "worker_plan": worker_plan,
}
path = ROOT / "state" / "outreach_sequences" / f"{day}-prepared.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(
    json.dumps(
        {
            "ok": True,
            "status": "prepared",
            "test_only": True,
            "path": str(path),
            "counts": worker_plan["counts"],
        },
        indent=2,
    )
)
