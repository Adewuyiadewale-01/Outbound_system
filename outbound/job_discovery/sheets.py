"""Google Sheets layer ported from ``job_discovery/src/sheets.mjs``.

Two transport clients with the same capability surfaces as the reference:

- ``AppsScriptSheetsClient`` -- POSTs the reference's action protocol to the
  user's bound Apps Script Web App (token-authenticated). This is the default
  transport and keeps the existing deployment working unchanged.
- ``GoogleSheetsClient`` -- direct Sheets API access via service-account
  credentials, implemented on gspread.

Tab schemas, default Control/Rules content, and the row builders are shared by
both transports. Constants and request intents are frozen against the
reference implementation by a Node-generated fixture
(``tests/fixtures/job_discovery_sheets.json``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import gspread
import httpx

TAB_SCHEMAS: dict[str, list[str]] = {
    "Control": ["Setting", "Value", "Description"],
    "ATS Platforms": ["Platform", "Google site target", "Enabled"],
    "Roles & Vocabulary": ["Role", "Search type", "Vocabulary"],
    "Queries": ["Query ID", "Platform", "Role", "Search type", "Google query", "Enabled"],
    "Jobs": [
        "job_id",
        "company_id",
        "company",
        "title",
        "location",
        "application_url",
        "ats_platform",
        "role_match",
        "junior_status",
        "remote_status",
        "python_status",
        "evidence_text",
        "source_queries",
        "first_seen_at",
        "last_seen_at",
        "verification_checked_at",
        "score",
        "status",
        "review_reason",
    ],
    "Companies": [
        "company_id",
        "company_name",
        "company_domain",
        "ats_platforms",
        "career_urls",
        "first_seen_at",
        "last_seen_at",
        "active_job_count",
        "remote_hiring_signal",
        "status",
        "notes",
    ],
    "Runs": [
        "run_id",
        "trigger",
        "status",
        "started_at",
        "ended_at",
        "queries_attempted",
        "results_found",
        "unique_candidates",
        "hydrated",
        "new_jobs",
        "errors",
        "notes",
    ],
    "Review Queue": [
        "job_id",
        "company",
        "title",
        "application_url",
        "review_reason",
        "junior_status",
        "remote_status",
        "python_status",
        "checked_at",
    ],
    "Rules": ["Rule", "Value", "Version"],
}

DEFAULT_CONTROL: list[list[Any]] = [
    ["Automation Enabled", "FALSE", "Set TRUE to allow the scheduler to launch the daily run."],
    ["Daily Run Time", "08:00", "24-hour local time."],
    ["Timezone", "Africa/Lagos", "IANA timezone used by the scheduler."],
    ["Max Queries Per Run", "180", "Use a smaller number for testing."],
    [
        "Max Listings Per Run",
        "180",
        "Daily verification target. The bot finishes its current query before stopping, so this can be exceeded slightly.",
    ],
    ["Minimum Listing Delay (ms)", "8000", "Minimum spacing after a job listing read."],
    ["Maximum Listing Delay (ms)", "15000", "Maximum spacing after a job listing read."],
    ["Minimum Listing Dwell (ms)", "4000", "Minimum time spent loading and reading a job listing."],
    ["Maximum Listing Dwell (ms)", "7000", "Maximum time spent loading and reading a job listing."],
    ["Minimum Page Delay (ms)", "15000", "Minimum spacing before advancing a Google results page."],
    ["Maximum Page Delay (ms)", "30000", "Maximum spacing before advancing a Google results page."],
    [
        "Search Page Burst Size",
        "3",
        "Pause after this many consecutive Google results pages within one query.",
    ],
    [
        "Minimum Search Page Cooldown (ms)",
        "180000",
        "Shortest pause between result-page bursts (3 minutes).",
    ],
    [
        "Maximum Search Page Cooldown (ms)",
        "300000",
        "Longest pause between result-page bursts (5 minutes).",
    ],
    [
        "Minimum Inter-query Delay (ms)",
        "90000",
        "Minimum spacing between completed Google queries.",
    ],
    [
        "Maximum Inter-query Delay (ms)",
        "180000",
        "Maximum spacing between completed Google queries.",
    ],
    ["Query Burst Size", "3", "Complete this many full queries before a longer cooldown."],
    ["Minimum Cooldown (ms)", "600000", "Shortest pause between query bursts (10 minutes)."],
    ["Maximum Cooldown (ms)", "900000", "Longest pause between query bursts (15 minutes)."],
    [
        "Maximum Pages Per Query",
        "0",
        "Optional total-page ceiling. Use 0 to paginate until Google ends or two thin pages occur.",
    ],
    [
        "Maximum Search Minutes Per Query",
        "75",
        "Safety time limit for a single Google query; normal pagination still stops after two low-yield pages.",
    ],
    ["Search Retry Attempts", "2", "Attempts before leaving a query pending for a later run."],
    [
        "Listing Retry Attempts",
        "3",
        "Attempts before leaving a failed listing pending for a later run.",
    ],
    ["Retry Base Delay (ms)", "2000", "Base delay used for bounded exponential retries."],
    ["Verified Job Recheck Days", "7", "Re-open unchanged verified jobs after this many days."],
    [
        "Stale Lock Minutes",
        "360",
        "Recover a lock only after this age or when its local process is gone.",
    ],
    [
        "Close After Query Misses",
        "3",
        "Mark a job closed after this many completed source-query misses.",
    ],
]

DEFAULT_RULES: list[list[str]] = [
    ["Rules version", "3", "3"],
    [
        "Junior signals",
        "junior | jr | associate | entry level | new grad | graduate | I | 1 | early career",
        "2",
    ],
    ["Senior signals", "senior | staff | principal | lead | manager | director", "2"],
    ["Remote signals", "remote | work from home | distributed | anywhere", "2"],
    ["Hybrid signals", "hybrid", "3"],
    ["Onsite signals", "no remote | on-site | onsite | in office", "3"],
    ["Non-remote signals", "no remote | on-site | onsite | in office | hybrid only", "2"],
    ["Python signals", "python", "2"],
]

LOCAL_ONLY_TABS = frozenset({"Control", "ATS Platforms", "Roles & Vocabulary", "Queries", "Rules"})

_SHEET_COLUMNS = {"C": 3, "F": 6}


def job_row(job: dict) -> list:
    """Port of ``jobRow`` (19 columns, order frozen)."""
    return [
        job.get("jobId"),
        job.get("companyId"),
        job.get("company"),
        job.get("title"),
        job.get("location"),
        job.get("canonicalUrl"),
        job.get("platform"),
        job.get("role"),
        job.get("juniorStatus"),
        job.get("remoteStatus"),
        job.get("pythonStatus"),
        job.get("evidenceText"),
        " | ".join(job.get("sourceQueries") or []),
        job.get("firstSeenAt"),
        job.get("lastSeenAt"),
        job.get("verificationCheckedAt"),
        job.get("score"),
        job.get("status"),
        job.get("reviewReason"),
    ]


def company_row(company: dict) -> list:
    """Port of ``companyRow``. The Notes column belongs to the user, so
    synchronisation deliberately leaves it untouched."""
    return [
        company.get("companyId"),
        company.get("companyName"),
        company.get("companyDomain"),
        " | ".join(company.get("platforms") or []),
        " | ".join(company.get("careerUrls") or []),
        company.get("firstSeenAt"),
        company.get("lastSeenAt"),
        company.get("activeJobCount"),
        company.get("remoteHiringSignal"),
        company.get("status"),
    ]


def run_row(run: dict) -> list:
    """Port of ``runRow`` (notes = ``stop=`` + errors joined with " | ")."""
    errors = run.get("errors") or []
    notes = " | ".join(
        part
        for part in [f"stop={run['stopReason']}" if run.get("stopReason") else "", *errors]
        if part
    )
    return [
        run.get("id"),
        run.get("trigger"),
        run.get("status"),
        run.get("startedAt"),
        run.get("endedAt") or "",
        run.get("queriesAttempted"),
        run.get("resultsFound"),
        run.get("uniqueCandidates"),
        run.get("hydrated"),
        run.get("newJobs"),
        len(errors),
        notes,
    ]


def _is_unsupported(action_error: Exception) -> bool:
    return bool(re.search(r"unsupported action", str(action_error), re.IGNORECASE))


class AppsScriptSheetsClient:
    """Port of the reference Apps Script transport (token-authenticated POSTs)."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def call(self, action: str, payload: dict | None = None) -> dict:
        body = {"action": action, "token": self.token, **(payload or {})}
        response = self._client.post(self.endpoint, json=body)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Apps Script endpoint failed: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            raise RuntimeError(error or "Apps Script endpoint rejected the request")
        return data

    def setup(
        self, *, platforms: list, roles: list, queries: list, localFirst: bool = False
    ) -> dict:
        return self.call(
            "bootstrap",
            {
                "platforms": platforms,
                "roles": roles,
                "queries": queries,
                "localFirst": localFirst,
                "tabSchemas": TAB_SCHEMAS,
                "defaultControl": DEFAULT_CONTROL,
            },
        )

    def read_control(self) -> dict:
        return self.call("getControl").get("control")

    def read_configuration(self) -> dict:
        try:
            return self.call("getConfiguration").get("configuration") or {}
        except Exception as error:
            if _is_unsupported(error):
                return {}
            raise

    def append(self, tab: str, rows: list) -> dict:
        return self.call("append", {"tab": tab, "rows": rows})

    def upsert(self, tab: str, records: list) -> dict:
        return self.call("upsert", {"tab": tab, "records": records})

    def replace(self, tab: str, rows: list) -> dict | None:
        try:
            return self.call("replace", {"tab": tab, "rows": rows})
        except Exception as error:
            if not _is_unsupported(error):
                raise
            if rows:
                return self.upsert(tab, [{"id": values[0], "values": values} for values in rows])
            return None

    def retain(self, tab: str, ids: list) -> dict | None:
        try:
            return self.call("retain", {"tab": tab, "ids": ids})
        except Exception as error:
            if _is_unsupported(error):
                return None
            raise

    def sync_projection(self, payload: dict) -> dict:
        try:
            return {"supported": True, **self.call("syncProjection", payload)}
        except Exception as error:
            if _is_unsupported(error):
                return {"supported": False}
            raise

    def clear_projection(self) -> dict:
        for tab in ("Jobs", "Companies", "Runs", "Review Queue"):
            self.replace(tab, [])
        return self.sync_projection(
            {"jobIds": [], "companyIds": [], "jobs": [], "companies": [], "reviews": [], "runs": []}
        )


class GoogleSheetsClient:
    """Port of the reference service-account transport, implemented on gspread.

    The spreadsheet connection is established lazily (first use), matching the
    reference where no network request happens at construction time.
    """

    def __init__(
        self,
        *,
        spreadsheet_id: str | None = None,
        service_account_json: str | dict | None = None,
        spreadsheet: Any | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.service_account = (
            json.loads(service_account_json)
            if isinstance(service_account_json, str)
            else service_account_json
        )
        self._spreadsheet = spreadsheet

    def _sheet(self) -> Any:
        if self._spreadsheet is None:
            service = gspread.service_account_from_dict(self.service_account)
            self._spreadsheet = service.open_by_key(self.spreadsheet_id)
        return self._spreadsheet

    def setup(
        self, *, platforms: list, roles: list, queries: list, localFirst: bool = False
    ) -> None:
        spreadsheet = self._sheet()
        existing_names = {worksheet.title for worksheet in spreadsheet.worksheets()}
        desired_tabs = [
            name for name in TAB_SCHEMAS if not localFirst or name not in LOCAL_ONLY_TABS
        ]
        created = [name for name in desired_tabs if name not in existing_names]
        for title in created:
            spreadsheet.add_worksheet(title=title, rows=1000, cols=26)
        values: dict[str, list] = {}
        for tab in created:
            values[f"'{tab}'!A1"] = [TAB_SCHEMAS[tab]]
        if not localFirst:
            if "Control" in created:
                values["'Control'!A2"] = DEFAULT_CONTROL
            if "ATS Platforms" in created:
                values["'ATS Platforms'!A2"] = [
                    [item.get("name"), item.get("siteTarget"), item.get("enabled")]
                    for item in platforms
                ]
            if "Roles & Vocabulary" in created:
                values["'Roles & Vocabulary'!A2"] = [
                    [role.get("name"), "junior", " | ".join(role.get("junior") or [])]
                    for role in roles
                ] + [
                    [role.get("name"), "unfiltered", " | ".join(role.get("unfiltered") or [])]
                    for role in roles
                ]
            if "Queries" in created:
                values["'Queries'!A2"] = [
                    [
                        item.get("id"),
                        item.get("platform"),
                        item.get("role"),
                        item.get("type"),
                        item.get("query"),
                        True,
                    ]
                    for item in queries
                ]
            if "Rules" in created:
                values["'Rules'!A2"] = DEFAULT_RULES
        if values:
            spreadsheet.values_batch_update(
                {
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {"range": range_name, "majorDimension": "ROWS", "values": rows}
                        for range_name, rows in values.items()
                    ],
                }
            )

    def read_control(self) -> dict[str, str]:
        rows = self._sheet().worksheet("Control").get_all_values()
        return {
            row[0]: (row[1] if len(row) > 1 else "") or "" for row in rows[1:] if row and row[0]
        }

    def read_rows(self, tab: str, last_column: str) -> list[list[str]]:
        column_count = _SHEET_COLUMNS[last_column.upper()]
        rows = self._sheet().worksheet(tab).get_all_values()
        trimmed_rows: list[list[str]] = []
        for row in rows[1:]:
            trimmed = list(row[:column_count])
            while trimmed and trimmed[-1] == "":
                trimmed.pop()
            trimmed_rows.append(trimmed)
        return trimmed_rows

    def read_configuration(self) -> dict:
        return {
            "platformRows": self.read_rows("ATS Platforms", "C"),
            "roleRows": self.read_rows("Roles & Vocabulary", "C"),
            "queryRows": self.read_rows("Queries", "F"),
            "ruleRows": self.read_rows("Rules", "C"),
        }

    def append(self, tab: str, rows: list) -> None:
        if not rows:
            return
        self._sheet().worksheet(tab).append_rows(
            rows,
            value_input_option="RAW",
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )

    def upsert(self, tab: str, records: list[dict]) -> None:
        if not records:
            return
        worksheet = self._sheet().worksheet(tab)
        current_ids = worksheet.col_values(1)
        index = {value: offset + 2 for offset, value in enumerate(current_ids[1:]) if value}
        updates = [record for record in records if record["id"] in index]
        additions = [record for record in records if record["id"] not in index]
        if updates:
            worksheet.batch_update(
                [
                    {
                        "range": f"A{index[record['id']]}",
                        "majorDimension": "ROWS",
                        "values": [record["values"]],
                    }
                    for record in updates
                ],
                value_input_option="RAW",
            )
        if additions:
            self.append(tab, [record["values"] for record in additions])

    def replace(self, tab: str, rows: list) -> None:
        worksheet = self._sheet().worksheet(tab)
        values = worksheet.get_all_values()
        row_count = max(1, len(values))
        if row_count > 1:
            worksheet.batch_clear([f"A2:ZZ{row_count}"])
        if rows:
            self.append(tab, rows)


def create_sheets_client(environment: dict) -> AppsScriptSheetsClient | GoogleSheetsClient | None:
    """Port of the runtime client selection from ``cli.mjs`` (buildRuntime)."""
    if (
        environment.get("SHEETS_TRANSPORT") == "apps-script"
        and environment.get("GOOGLE_APPS_SCRIPT_URL")
        and environment.get("APPS_SCRIPT_TOKEN")
    ):
        return AppsScriptSheetsClient(
            endpoint=environment["GOOGLE_APPS_SCRIPT_URL"],
            token=environment["APPS_SCRIPT_TOKEN"],
        )
    if environment.get("GOOGLE_SHEET_ID") and environment.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        source = environment["GOOGLE_SERVICE_ACCOUNT_JSON"].strip()
        if not source.startswith("{"):
            source = Path(source).read_text(encoding="utf-8")
        return GoogleSheetsClient(
            spreadsheet_id=environment["GOOGLE_SHEET_ID"],
            service_account_json=source,
        )
    return None
