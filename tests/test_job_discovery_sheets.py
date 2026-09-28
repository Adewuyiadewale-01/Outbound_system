"""Tests for the ported Sheets layer (``job_discovery/src/sheets.mjs``).

Constants and transport intents are frozen against Node-generated expectations
in ``tests/fixtures/job_discovery_sheets.json`` (produced by running the
reference implementation; see the port plan for the extraction script).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from outbound.job_discovery import sheets
from outbound.job_discovery.sheets import (
    AppsScriptSheetsClient,
    GoogleSheetsClient,
    company_row,
    create_sheets_client,
    job_row,
    run_row,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "job_discovery_sheets.json").read_text())

SAMPLE_PLATFORMS = [
    {"name": "Ashby", "siteTarget": "site:jobs.ashbyhq.com", "enabled": True},
    {
        "name": "Lever",
        "siteTarget": "(site:jobs.lever.co OR site:jobs.eu.lever.co)",
        "enabled": True,
    },
]
SAMPLE_ROLES = [
    {
        "name": "Python Developer",
        "field": "engineering",
        "junior": ["junior python developer", "entry level python developer"],
        "unfiltered": ["python developer", "python engineer"],
    }
]
SAMPLE_QUERIES = [
    {
        "id": "Ashby:Python Developer:junior",
        "platform": "Ashby",
        "role": "Python Developer",
        "type": "junior",
        "query": 'site:jobs.ashbyhq.com (intitle:"junior python developer") remote -"no remote"',
        "allowedHosts": ["jobs.ashbyhq.com"],
    }
]


# --------------------------------------------------------------------- fakes


class FakeWorksheet:
    def __init__(self, title: str, rows: list | None = None) -> None:
        self.title = title
        self.rows = [list(row) for row in (rows or [])]
        self.batch_updates: list = []
        self.appends: list = []
        self.clears: list = []

    def get_all_values(self) -> list[list[str]]:
        if not self.rows:
            return []
        width = max(len(row) for row in self.rows)
        return [list(row) + [""] * (width - len(row)) for row in self.rows]

    def col_values(self, column: int) -> list[str]:
        values = [row[column - 1] if len(row) >= column else "" for row in self.rows]
        while values and values[-1] == "":
            values.pop()
        return values

    def batch_update(self, data, value_input_option=None, **_kwargs) -> None:
        items = []
        for item in data:
            item = dict(item)
            if "!" not in item["range"]:
                item["range"] = f"'{self.title}'!{item['range']}"
            items.append(item)
        self.batch_updates.append({"data": items, "valueInputOption": value_input_option})

    def append_rows(
        self, values, value_input_option=None, insert_data_option=None, table_range=None, **_kw
    ):
        self.appends.append(
            {
                "values": [list(row) for row in values],
                "value_input_option": value_input_option,
                "insert_data_option": insert_data_option,
                "table_range": table_range,
            }
        )

    def batch_clear(self, ranges) -> None:
        self.clears.append(list(ranges))


class FakeSpreadsheet:
    def __init__(self, rows_by_title: dict[str, list] | None = None) -> None:
        self.worksheets_by_title: dict[str, FakeWorksheet] = {
            title: FakeWorksheet(title, rows) for title, rows in (rows_by_title or {}).items()
        }
        self.added: list[dict] = []
        self.last_values_batch_update: dict | None = None

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self.worksheets_by_title.values())

    def worksheet(self, title: str) -> FakeWorksheet:
        return self.worksheets_by_title[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        worksheet = FakeWorksheet(title)
        self.worksheets_by_title[title] = worksheet
        self.added.append({"title": title, "rows": rows, "cols": cols})
        return worksheet

    def values_batch_update(self, body) -> None:
        self.last_values_batch_update = body


def _mock_apps_client(handler) -> tuple[AppsScriptSheetsClient, list]:
    requests: list = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return handler(request, body)

    http = httpx.Client(transport=httpx.MockTransport(recording_handler))
    client = AppsScriptSheetsClient(
        endpoint="https://example.com/apps-script", token="token-1", client=http
    )
    return client, requests


def _ok_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ----------------------------------------------------------------- builders


def test_job_row_column_order_and_joining() -> None:
    row = job_row(
        {
            "jobId": "job-1",
            "companyId": "c1",
            "company": "Acme",
            "title": "Junior Python Developer",
            "location": "Remote",
            "canonicalUrl": "https://example.com/job",
            "platform": "Ashby",
            "role": "Python Developer",
            "juniorStatus": "verified",
            "remoteStatus": "verified",
            "pythonStatus": "verified",
            "evidenceText": "ev",
            "sourceQueries": ["q1", "q2"],
            "firstSeenAt": "2026-09-01T00:00:00.000Z",
            "lastSeenAt": "2026-09-02T00:00:00.000Z",
            "verificationCheckedAt": "2026-09-02T00:00:00.000Z",
            "score": 110,
            "status": "verified",
            "reviewReason": "",
        }
    )
    assert row == [
        "job-1",
        "c1",
        "Acme",
        "Junior Python Developer",
        "Remote",
        "https://example.com/job",
        "Ashby",
        "Python Developer",
        "verified",
        "verified",
        "verified",
        "ev",
        "q1 | q2",
        "2026-09-01T00:00:00.000Z",
        "2026-09-02T00:00:00.000Z",
        "2026-09-02T00:00:00.000Z",
        110,
        "verified",
        "",
    ]
    assert len(row) == len(sheets.TAB_SCHEMAS["Jobs"]) == 19


def test_company_row_leaves_notes_untouched() -> None:
    row = company_row(
        {
            "companyId": "c1",
            "companyName": "Acme",
            "companyDomain": "acme.com",
            "platforms": ["Ashby", "Lever"],
            "careerUrls": ["https://acme.com/careers"],
            "firstSeenAt": "a",
            "lastSeenAt": "b",
            "activeJobCount": 3,
            "remoteHiringSignal": "yes",
            "status": "active",
            "notes": "user notes are never written",
        }
    )
    assert row == [
        "c1",
        "Acme",
        "acme.com",
        "Ashby | Lever",
        "https://acme.com/careers",
        "a",
        "b",
        3,
        "yes",
        "active",
    ]
    assert len(row) == len(sheets.TAB_SCHEMAS["Companies"]) - 1 == 10


def test_run_row_notes_and_error_count() -> None:
    row = run_row(
        {
            "id": "run-1",
            "trigger": "manual",
            "status": "completed",
            "startedAt": "s",
            "endedAt": "e",
            "queriesAttempted": 5,
            "resultsFound": 50,
            "uniqueCandidates": 20,
            "hydrated": 10,
            "newJobs": 8,
            "errors": ["oops", "twice"],
            "stopReason": "daily_listing_target",
        }
    )
    assert row == [
        "run-1",
        "manual",
        "completed",
        "s",
        "e",
        5,
        50,
        20,
        10,
        8,
        2,
        "stop=daily_listing_target | oops | twice",
    ]
    empty = run_row(
        {"id": "r", "trigger": "schedule", "status": "running", "startedAt": "s", "errors": []}
    )
    assert empty[4] == ""
    assert empty[10] == 0
    assert empty[11] == ""


# ------------------------------------------------------- fixture-locked data


def test_constants_match_node_generated_fixture() -> None:
    assert sheets.TAB_SCHEMAS == FIXTURE["tabSchemas"]
    assert sheets.DEFAULT_CONTROL == FIXTURE["defaultControl"]
    assert sheets.DEFAULT_RULES == FIXTURE["defaultRules"]
    assert len(sheets.DEFAULT_CONTROL) == 27
    assert len(sheets.DEFAULT_RULES) == 8


def test_apps_script_bootstrap_payload_matches_fixture() -> None:
    client, requests = _mock_apps_client(
        lambda request, body: _ok_response({"ok": True, "created": ["Control"]})
    )
    client.setup(
        platforms=SAMPLE_PLATFORMS, roles=SAMPLE_ROLES, queries=SAMPLE_QUERIES, localFirst=False
    )
    assert requests == [FIXTURE["bootstrapPayload"]]
    client.close()


# ----------------------------------------------------- Apps Script transport


def test_apps_script_reads_and_fallbacks() -> None:
    def handler(request: httpx.Request, body: dict) -> httpx.Response:
        action = body["action"]
        if action == "getControl":
            return _ok_response(
                {"ok": True, "control": {"Automation Enabled": "TRUE", "Timezone": "Africa/Lagos"}}
            )
        if action == "getConfiguration":
            return _ok_response(
                {
                    "ok": True,
                    "configuration": {"platformRows": [["Ashby", "site:jobs.ashbyhq.com", "TRUE"]]},
                }
            )
        if action == "replace":
            return _ok_response({"ok": False, "error": "Unsupported action: replace"})
        if action == "retain":
            return _ok_response({"ok": False, "error": "Unsupported action: retain"})
        if action == "syncProjection":
            return _ok_response({"ok": False, "error": "Unsupported action: syncProjection"})
        return _ok_response({"ok": True, "inserted": 1, "updated": 0})

    client, requests = _mock_apps_client(handler)
    assert client.read_control() == {"Automation Enabled": "TRUE", "Timezone": "Africa/Lagos"}
    assert client.read_configuration() == {
        "platformRows": [["Ashby", "site:jobs.ashbyhq.com", "TRUE"]]
    }
    assert client.replace("Review Queue", [["a", "b"]]) == {"ok": True, "inserted": 1, "updated": 0}
    assert requests[-1] == {
        "action": "upsert",
        "token": "token-1",
        "tab": "Review Queue",
        "records": [{"id": "a", "values": ["a", "b"]}],
    }
    assert client.replace("Review Queue", []) is None
    assert client.retain("Jobs", ["a"]) is None
    assert client.sync_projection({"jobIds": []}) == {"supported": False}
    client.close()


def test_apps_script_get_configuration_unsupported_returns_empty() -> None:
    client, _ = _mock_apps_client(
        lambda request, body: _ok_response(
            {"ok": False, "error": "Unsupported action: getConfiguration"}
        )
    )
    assert client.read_configuration() == {}
    client.close()


def test_apps_script_supported_projection_and_clear_sequence() -> None:
    def handler(request: httpx.Request, body: dict) -> httpx.Response:
        if body["action"] == "syncProjection":
            return _ok_response(
                {"ok": True, "jobs": {"inserted": 1}, "priorityViews": ["High"], "version": 3}
            )
        return _ok_response({"ok": True, "replaced": 0})

    client, requests = _mock_apps_client(handler)
    result = client.clear_projection()
    assert [item["action"] for item in requests] == [
        "replace",
        "replace",
        "replace",
        "replace",
        "syncProjection",
    ]
    assert [item["tab"] for item in requests[:4]] == ["Jobs", "Companies", "Runs", "Review Queue"]
    assert requests[-1]["jobIds"] == [] and requests[-1]["companyIds"] == []
    assert result == {
        "supported": True,
        "ok": True,
        "jobs": {"inserted": 1},
        "priorityViews": ["High"],
        "version": 3,
    }
    client.close()


def test_apps_script_errors_surface_with_reference_messages() -> None:
    client, _ = _mock_apps_client(lambda request, body: httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError, match="Apps Script endpoint failed: HTTP 500"):
        client.read_control()
    client.close()

    client, _ = _mock_apps_client(
        lambda request, body: _ok_response({"ok": False, "error": "Unauthorized"})
    )
    with pytest.raises(RuntimeError, match="Unauthorized"):
        client.read_control()
    client.close()

    client, _ = _mock_apps_client(lambda request, body: _ok_response({"ok": False}))
    with pytest.raises(RuntimeError, match="Apps Script endpoint rejected the request"):
        client.read_control()
    client.close()


# ------------------------------------------------- Google Sheets transport


def test_google_setup_matches_fixture() -> None:
    fake = FakeSpreadsheet()
    client = GoogleSheetsClient(spreadsheet=fake)
    client.setup(
        platforms=SAMPLE_PLATFORMS, roles=SAMPLE_ROLES, queries=SAMPLE_QUERIES, localFirst=False
    )
    assert [item["title"] for item in fake.added] == FIXTURE["googleSetup"]["created"]
    body = fake.last_values_batch_update
    assert body is not None
    assert body["valueInputOption"] == "USER_ENTERED"  # reference passes this as a query param
    assert body["data"] == FIXTURE["googleSetup"]["valuesBatchUpdate"]["data"]


def test_google_setup_local_first_matches_fixture() -> None:
    fake = FakeSpreadsheet()
    client = GoogleSheetsClient(spreadsheet=fake)
    client.setup(
        platforms=SAMPLE_PLATFORMS, roles=SAMPLE_ROLES, queries=SAMPLE_QUERIES, localFirst=True
    )
    assert [item["title"] for item in fake.added] == FIXTURE["googleSetupLocalFirst"]["created"]
    body = fake.last_values_batch_update
    assert body is not None
    assert body["valueInputOption"] == "USER_ENTERED"
    assert body["data"] == FIXTURE["googleSetupLocalFirst"]["valuesBatchUpdate"]["data"]


def test_google_upsert_matches_fixture() -> None:
    fake = FakeSpreadsheet({"Jobs": [["job_id"], ["job-1"]]})
    client = GoogleSheetsClient(spreadsheet=fake)
    client.upsert(
        "Jobs",
        [
            {"id": "job-1", "values": ["job-1", "New Title"]},
            {"id": "job-2", "values": ["job-2", "Fresh"]},
        ],
    )
    worksheet = fake.worksheet("Jobs")
    assert worksheet.batch_updates == [
        {"data": FIXTURE["googleUpsert"]["updates"], "valueInputOption": "RAW"}
    ]
    assert [item["values"] for item in worksheet.appends] == [FIXTURE["googleUpsert"]["appends"]]
    append_call = worksheet.appends[0]
    assert append_call["value_input_option"] == "RAW"
    assert append_call["insert_data_option"] == "INSERT_ROWS"
    assert append_call["table_range"] == "A1"


def test_google_replace_matches_fixture() -> None:
    fake = FakeSpreadsheet({"Review Queue": [["job_id"], ["b"], ["c"]]})
    client = GoogleSheetsClient(spreadsheet=fake)
    client.replace("Review Queue", [["x", "y"]])
    worksheet = fake.worksheet("Review Queue")
    assert worksheet.clears == [[FIXTURE["googleReplace"]["cleared"]]]
    assert [item["values"] for item in worksheet.appends] == [FIXTURE["googleReplace"]["appended"]]


def test_google_reads_match_fixture() -> None:
    control_rows = FIXTURE["googleReadControl"]["sheetRows"]
    fake = FakeSpreadsheet({"Control": control_rows})
    client = GoogleSheetsClient(spreadsheet=fake)
    assert client.read_control() == FIXTURE["googleReadControl"]["result"]

    configuration_rows = {
        tab: rows for tab, rows in FIXTURE["googleReadConfiguration"]["sheetRows"].items()
    }
    fake2 = FakeSpreadsheet(configuration_rows)
    client2 = GoogleSheetsClient(spreadsheet=fake2)
    assert client2.read_configuration() == FIXTURE["googleReadConfiguration"]["result"]


def test_google_empty_operations_are_noops() -> None:
    fake = FakeSpreadsheet({"Jobs": [["job_id"]]})
    client = GoogleSheetsClient(spreadsheet=fake)
    client.append("Jobs", [])
    client.upsert("Jobs", [])
    assert fake.worksheet("Jobs").appends == []
    assert fake.worksheet("Jobs").batch_updates == []


def test_create_sheets_client_selection() -> None:
    apps = create_sheets_client(
        {
            "SHEETS_TRANSPORT": "apps-script",
            "GOOGLE_APPS_SCRIPT_URL": "https://x",
            "APPS_SCRIPT_TOKEN": "t",
        }
    )
    assert isinstance(apps, AppsScriptSheetsClient)
    apps.close()

    google = create_sheets_client(
        {"GOOGLE_SHEET_ID": "sheet-1", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"client_email": "x@y"}'}
    )
    assert isinstance(google, GoogleSheetsClient)
    assert google.service_account == {"client_email": "x@y"}

    assert create_sheets_client({}) is None


def test_create_sheets_client_reads_service_account_file(tmp_path: Path) -> None:
    credential_file = tmp_path / "svc.json"
    credential_file.write_text('{"client_email": "file@y"}')
    client = create_sheets_client(
        {"GOOGLE_SHEET_ID": "sheet-1", "GOOGLE_SERVICE_ACCOUNT_JSON": str(credential_file)}
    )
    assert isinstance(client, GoogleSheetsClient)
    assert client.service_account == {"client_email": "file@y"}
