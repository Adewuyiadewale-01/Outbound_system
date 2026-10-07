"""Tests for the ported providers / browser / listing modules (Phase 5).

Ported from ``job_discovery/tests/playwright-provider.test.mjs`` plus extended
coverage for the provider factory, the Apps Script-independent transports, and
the HTTP listing reader.

Note: the reference's playwright-cli text parser (``parsePlaywrightResult``)
is intentionally not ported -- the transport was replaced by the direct
Playwright Python API (see ``docs/PORT-JOB-DISCOVERY.md`` §6.2); its behavioral
intent is covered by the headed/persistent launch test below and Phase 7 live
verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from outbound.job_discovery import listing as listing_module
from outbound.job_discovery import providers as providers_module
from outbound.job_discovery.browser import (
    PlaywrightBrowser,
    PlaywrightGoogleSearchProvider,
    SearchBlockedError,
    SearchSafetyLimitError,
    create_playwright_listing_reader,
    next_pagination_state,
)

QUERY = {"query": "site:jobs.ashbyhq.com python", "allowedHosts": ["jobs.ashbyhq.com"]}


class FakeBrowser:
    """Duck-typed browser: queues page payloads for search, a fixed listing for reads."""

    def __init__(self, pages=None, listing=None):
        self.opened: list = []
        self._pages = list(pages or [])
        self._listing = listing
        self.dwell_ms = None
        self.closed = False

    def open(self, url):
        self.opened.append(url)

    def reject_google_cookies_if_present(self):
        return None

    def scroll_search_results(self):
        return None

    def dwell_and_scroll(self, dwell_ms):
        self.dwell_ms = dwell_ms

    def evaluate(self, expression):
        if self._listing is not None:
            return self._listing
        page = self._pages.pop(0)
        return {"url": "https://google.com/search", "title": "Search", "bodyText": "", **page}

    def close(self):
        self.closed = True


def _valid_jobs(*numbers):
    return [
        {
            "title": f"Job {number}",
            "link": f"https://jobs.ashbyhq.com/acme/job-{number}",
            "snippet": "Remote",
        }
        for number in numbers
    ]


# ---------------------------------------------------------------- pagination


def test_ends_pagination_after_three_consecutive_sparse_pages() -> None:
    assert next_pagination_state(0, 2) == {"sparseStreak": 0, "complete": False}
    assert next_pagination_state(0, 1) == {"sparseStreak": 1, "complete": False}
    assert next_pagination_state(1, 1) == {"sparseStreak": 2, "complete": False}
    assert next_pagination_state(2, 1) == {"sparseStreak": 3, "complete": True}
    assert next_pagination_state(2, 3) == {"sparseStreak": 0, "complete": False}


def test_paginates_until_three_consecutive_sparse_pages_and_rejects_off_domain_results() -> None:
    pages = [
        {
            "hasNext": True,
            "results": _valid_jobs(1, 2, 3)
            + [{"title": "Noise", "link": "https://example.com/noise", "snippet": ""}],
        },
        {"hasNext": True, "results": _valid_jobs(4, 5)},
        {"hasNext": True, "results": _valid_jobs(6)},
        {"hasNext": True, "results": _valid_jobs(7)},
        {"hasNext": True, "results": _valid_jobs(8)},
    ]
    browser = FakeBrowser(pages)
    checkpoints: list = []
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {
            "maxPages": 10,
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "onPage": lambda checkpoint: checkpoints.append(
                {
                    "validResults": checkpoint["validResults"],
                    "sparseStreak": checkpoint["sparseStreak"],
                    "paginationStop": checkpoint["paginationStop"],
                }
            ),
        },
    )
    assert len(results) == 8
    assert results.pagination_stop == "low_yield"
    assert results.last_page == 4
    assert [checkpoint["validResults"] for checkpoint in checkpoints] == [3, 2, 1, 1, 1]
    assert [checkpoint["sparseStreak"] for checkpoint in checkpoints] == [0, 0, 1, 2, 3]
    assert len(browser.opened) == 5
    assert "start=0" in browser.opened[0]
    assert "start=40" in browser.opened[4]


def test_continues_from_a_saved_page_checkpoint_when_the_total_page_ceiling_is_disabled() -> None:
    browser = FakeBrowser(
        [
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "Job",
                        "link": "https://jobs.ashbyhq.com/acme/page-21",
                        "snippet": "Remote",
                    }
                ],
            }
        ]
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY, {"resume": {"nextPage": 20}, "maxPages": 0, "minPageDelayMs": 0, "maxPageDelayMs": 0}
    )
    assert "start=200" in browser.opened[0]
    assert len(results) == 1
    assert results.pagination_stop == "exhausted"


def test_resumed_partial_results_are_canonicalized_and_deduplicated() -> None:
    browser = FakeBrowser([{"hasNext": False, "results": []}])
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {
            "resume": {
                "partialResults": [
                    {
                        "title": "Old",
                        "link": "https://jobs.ashbyhq.com/acme/old?utm_source=x",
                        "snippet": "s",
                    },
                    {
                        "title": "Old dup",
                        "link": "https://jobs.ashbyhq.com/acme/old?ref=y",
                        "snippet": "s",
                    },
                    {"title": "Bad", "link": "not a url", "snippet": ""},
                ]
            },
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
        },
    )
    assert [item["link"] for item in results] == ["https://jobs.ashbyhq.com/acme/old"]
    assert results.pagination_stop == "exhausted"


def test_normalizes_tracking_variants_before_recording_search_results() -> None:
    browser = FakeBrowser(
        [
            {
                "hasNext": False,
                "results": [
                    {
                        "title": "Job",
                        "link": "https://jobs.ashbyhq.com/acme/role-1?utm_source=google",
                        "snippet": "Remote",
                    },
                    {
                        "title": "Job",
                        "link": "https://jobs.ashbyhq.com/acme/role-1?ref=duplicate",
                        "snippet": "Remote",
                    },
                ],
            }
        ]
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        {"query": "site:jobs.ashbyhq.com python", "allowedHosts": ["jobs.ashbyhq.com"]}
    )
    assert len(results) == 1
    assert results[0]["link"] == "https://jobs.ashbyhq.com/acme/role-1"


def test_leaves_a_query_retryable_when_google_presents_a_challenge() -> None:
    browser = FakeBrowser(
        [
            {
                "url": "https://google.com/sorry/",
                "title": "Verify",
                "bodyText": "Our systems detected unusual traffic",
                "hasNext": False,
                "results": [],
            }
        ]
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    with pytest.raises(SearchBlockedError, match="verification or unusual-traffic"):
        provider.search({"query": "site:jobs.ashbyhq.com python"})


def test_result_limit_stop_reason() -> None:
    browser = FakeBrowser([{"hasNext": True, "results": _valid_jobs(1, 2, 3)}])
    provider = PlaywrightGoogleSearchProvider(browser=browser, max_results=2)
    results = provider.search(QUERY, {"minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert len(results) == 2
    assert results.pagination_stop == "result_limit"


def test_page_ceiling_raises_safety_limit() -> None:
    browser = FakeBrowser([{"hasNext": True, "results": _valid_jobs(1, 2, 3)}])
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    with pytest.raises(SearchSafetyLimitError, match="1-page safety limit"):
        provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})


def test_empty_allowed_hosts_derives_from_query_site_targets() -> None:
    browser = FakeBrowser(
        [
            {
                "hasNext": False,
                "results": _valid_jobs(1)
                + [{"title": "Other", "link": "https://example.com/job-9", "snippet": ""}],
            }
        ]
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search({"query": "site:jobs.ashbyhq.com python", "allowedHosts": []})
    assert [item["link"] for item in results] == ["https://jobs.ashbyhq.com/acme/job-1"]


# --------------------------------------------------------------- real browser


class _FakePage:
    def __init__(self):
        self.gotos: list = []
        self.waits: list = []

    def goto(self, url):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _FakeContext:
    def __init__(self):
        self.pages: list = []
        self.default_timeout = None
        self.closed = False
        self.page_obj = _FakePage()

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def new_page(self):
        return self.page_obj

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self):
        self.launch_args = None
        self.context = _FakeContext()

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launch_args = {"user_data_dir": user_data_dir, **kwargs}
        return self.context


class _FakePlaywrightApp:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_opens_a_headed_browser_with_a_persistent_local_profile() -> None:
    browser = PlaywrightBrowser(headed=True, profile_path="./data/test-browser-profile")
    fake_app = _FakePlaywrightApp()
    browser._playwright = fake_app  # inject: no real launch in unit tests
    browser._owns_playwright = True
    browser.open("https://example.com")
    assert fake_app.chromium.launch_args == {
        "user_data_dir": str(Path("./data/test-browser-profile").resolve()),
        "headless": False,
    }
    assert fake_app.chromium.context.page_obj.gotos == ["https://example.com"]
    assert fake_app.chromium.context.default_timeout == 45_000
    browser.close()
    assert fake_app.chromium.context.closed is True
    assert fake_app.stopped is True


def test_records_video_when_requested() -> None:
    browser = PlaywrightBrowser(
        headed=True, profile_path="./data/test-browser-profile", record_video_dir="./data/videos"
    )
    fake_app = _FakePlaywrightApp()
    browser._playwright = fake_app
    browser._owns_playwright = True
    browser.open("https://example.com")
    assert fake_app.chromium.launch_args["record_video_dir"] == str(Path("./data/videos").resolve())
    browser.close()


# ----------------------------------------------------------- listing (browser)


def test_playwright_listing_reader_short_circuits_career_landing_pages() -> None:
    browser = FakeBrowser(listing={"title": "x"})
    reader = create_playwright_listing_reader(browser)
    result = reader({"canonicalUrl": "https://jobs.ashbyhq.com/cradlebio", "platform": "Ashby"})
    assert result == {
        "isJobPosting": False,
        "canonicalUrl": "https://jobs.ashbyhq.com/cradlebio",
        "exclusionReason": "company_careers_landing_page",
    }
    assert browser.opened == []


def test_playwright_listing_reader_infers_company_and_dwells() -> None:
    listing = {
        "title": "Backend Engineer",
        "description": "d",
        "company": "",
        "location": "",
        "closed": False,
        "blocked": False,
        "isJobPosting": True,
    }
    browser = FakeBrowser(listing=listing)
    reader = create_playwright_listing_reader(browser)
    candidate = {
        "canonicalUrl": "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Backend-Engineer_R-12345",
        "platform": "Workday",
        "displayLink": "acme.wd3.myworkdayjobs.com",
    }
    result = reader(candidate)
    assert result["company"] == "Acme"
    assert result["canonicalUrl"] == candidate["canonicalUrl"]
    assert browser.opened == [candidate["canonicalUrl"]]
    assert 4000 <= browser.dwell_ms <= 7000


def test_playwright_listing_reader_tenant_and_path_inference() -> None:
    listing = {
        "title": "T",
        "description": "d",
        "company": "",
        "location": "",
        "blocked": False,
        "isJobPosting": True,
    }
    recruitee = create_playwright_listing_reader(FakeBrowser(listing=listing))
    result = recruitee(
        {
            "canonicalUrl": "https://acme.recruitee.com/o/backend-engineer",
            "platform": "Recruitee",
            "displayLink": "acme.recruitee.com",
        }
    )
    assert result["company"] == "Acme"
    lever = create_playwright_listing_reader(FakeBrowser(listing=listing))
    result = lever(
        {
            "canonicalUrl": "https://jobs.lever.co/acme/1234abcd-56ef",
            "platform": "Lever",
            "displayLink": "jobs.lever.co",
        }
    )
    assert result["company"] == "Acme"


def test_playwright_listing_reader_excludes_non_postings_and_raises_on_blocked() -> None:
    not_posting = {
        "title": "T",
        "description": "d",
        "company": "",
        "location": "",
        "blocked": False,
        "isJobPosting": False,
    }
    reader = create_playwright_listing_reader(FakeBrowser(listing=not_posting))
    result = reader(
        {
            "canonicalUrl": "https://jobs.lever.co/acme/1234abcd",
            "platform": "Lever",
            "displayLink": "jobs.lever.co",
        }
    )
    assert result["exclusionReason"] == "careers_landing_page"

    blocked = {"blocked": True, "isJobPosting": True}
    reader = create_playwright_listing_reader(FakeBrowser(listing=blocked))
    with pytest.raises(RuntimeError, match="access or verification"):
        reader(
            {
                "canonicalUrl": "https://jobs.lever.co/acme/1234abcd",
                "platform": "Lever",
                "displayLink": "jobs.lever.co",
            }
        )


# ------------------------------------------------------------------- providers


def test_fixture_search_provider_reads_by_query_id(tmp_path: Path) -> None:
    fixture_file = tmp_path / "search-results.json"
    fixture_file.write_text(json.dumps({"Ashby:X:junior": [{"title": "T", "link": "https://x"}]}))
    provider = providers_module.FixtureSearchProvider(fixture_file)
    assert provider.search({"id": "Ashby:X:junior"}) == [{"title": "T", "link": "https://x"}]
    assert provider.search({"id": "missing"}) == []


def test_fixture_search_provider_defaults_to_packaged_fixture() -> None:
    provider = providers_module.FixtureSearchProvider()
    assert provider.file_path.name == "search-results.json"
    assert provider.file_path.exists()
    assert "outbound/job_discovery/fixtures" in str(provider.file_path)


def test_google_cse_provider_maps_items_and_reports_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["key"] == "k" and params["cx"] == "e" and params["num"] == "10"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"title": "T", "link": "https://x", "snippet": "s", "displayLink": "x.com"}
                ]
            },
            request=request,
        )

    provider = providers_module.GoogleCustomSearchProvider(
        api_key="k", engine_id="e", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert provider.search({"query": "q"}) == [
        {"title": "T", "link": "https://x", "snippet": "s", "displayLink": "x.com"}
    ]

    failing = providers_module.GoogleCustomSearchProvider(
        api_key="k",
        engine_id="e",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
        ),
    )
    with pytest.raises(RuntimeError, match="Search API returned HTTP 500"):
        failing.search({"query": "q"})


def test_http_search_provider_posts_and_validates() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"results": [{"title": "T", "link": "https://x", "snippet": "s"}]},
            request=request,
        )

    provider = providers_module.HttpSearchProvider(
        endpoint="https://search.example.com/api",
        token="tok",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    results = provider.search({"query": "site:x python", "id": "q-1"})
    assert captured["body"] == {"query": "site:x python", "queryId": "q-1"}
    assert captured["authorization"] == "Bearer tok"
    assert results == [{"title": "T", "link": "https://x", "snippet": "s"}]

    bad = providers_module.HttpSearchProvider(
        endpoint="https://search.example.com/api",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"nope": True}, request=request)
            )
        ),
    )
    with pytest.raises(RuntimeError, match="must include a results array"):
        bad.search({"query": "q"})

    failing = providers_module.HttpSearchProvider(
        endpoint="https://search.example.com/api",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request))
        ),
    )
    with pytest.raises(RuntimeError, match="Search provider returned HTTP 503"):
        failing.search({"query": "q"})


def test_create_search_provider_and_listing_reader_selection(tmp_path: Path) -> None:
    fixture = providers_module.create_search_provider({})
    assert isinstance(fixture, providers_module.FixtureSearchProvider)
    assert providers_module.create_listing_reader({}, fixture) is None

    playwright = providers_module.create_search_provider(
        {
            "SEARCH_PROVIDER": "playwright-google",
            "PLAYWRIGHT_HEADED": "false",
            "PLAYWRIGHT_PROFILE_DIR": "./data/test-profile",
        }
    )
    assert isinstance(playwright, PlaywrightGoogleSearchProvider)
    assert playwright.browser.headed is False
    assert playwright.browser.profile_path.endswith("data/test-profile")
    reader = providers_module.create_listing_reader(
        {"SEARCH_PROVIDER": "playwright-google"}, playwright
    )
    assert callable(reader)

    cse = providers_module.create_search_provider(
        {"SEARCH_PROVIDER": "google-cse", "GOOGLE_CSE_API_KEY": "k", "GOOGLE_CSE_ID": "e"}
    )
    assert isinstance(cse, providers_module.GoogleCustomSearchProvider)
    cse.close()

    http_provider = providers_module.create_search_provider(
        {"SEARCH_PROVIDER": "http", "SEARCH_API_URL": "https://api", "SEARCH_API_TOKEN": "t"}
    )
    assert isinstance(http_provider, providers_module.HttpSearchProvider)
    http_provider.close()

    stopped = providers_module.with_pagination_stop(["a", "b"], "resumed")
    assert isinstance(stopped, list) and stopped.pagination_stop == "resumed"


# ------------------------------------------------------------- listing (HTTP)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_read_listing_prefers_json_ld_fields() -> None:
    html = (
        "<html><head><title>Fallback Title</title>"
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Backend Engineer", '
        '"description": "<p>Build &amp; ship services.</p>", '
        '"hiringOrganization": {"name": "Acme"}, '
        '"jobLocation": {"address": {"addressLocality": "Lagos"}}}'
        "</script></head><body>ignored</body></html>"
    )
    result = listing_module.read_listing(
        {
            "canonicalUrl": "https://example.com/job",
            "title": "Candidate title",
            "displayLink": "example.com",
        },
        client=_client(lambda request: httpx.Response(200, text=html, request=request)),
    )
    assert result["title"] == "Backend Engineer"
    assert result["company"] == "Acme"
    assert result["location"] == "Lagos"
    assert "Build &amp; ship services." in result["description"]


def test_read_listing_falls_back_to_html_title_and_display_link() -> None:
    html = "<html><head><title>  Some Job  </title></head><body><script>junk()</script><p>Hello</p></body></html>"
    result = listing_module.read_listing(
        {
            "canonicalUrl": "https://sub.example.com/job",
            "title": "",
            "displayLink": "www.sub.example.com",
        },
        client=_client(lambda request: httpx.Response(200, text=html, request=request)),
    )
    assert result["title"] == "Some Job"
    assert result["company"] == "sub"
    assert result["location"] == ""
    assert result["description"].startswith("Some Job")


def test_read_listing_ignores_malformed_json_ld_and_truncates_long_descriptions() -> None:
    malformed = '<script type="application/ld+json">{bad json</script><title>T</title>'
    result = listing_module.read_listing(
        {"canonicalUrl": "https://example.com/job", "title": "", "displayLink": "example.com"},
        client=_client(lambda request: httpx.Response(200, text=malformed, request=request)),
    )
    assert result["title"] == "T"

    long_html = "<html><body>" + ("word " * 9000) + "</body></html>"
    result = listing_module.read_listing(
        {"canonicalUrl": "https://example.com/long", "title": "", "displayLink": "example.com"},
        client=_client(lambda request: httpx.Response(200, text=long_html, request=request)),
    )
    assert len(result["description"]) == 35_000


def test_read_listing_reports_http_errors() -> None:
    with pytest.raises(RuntimeError, match="Listing returned HTTP 404"):
        listing_module.read_listing(
            {
                "canonicalUrl": "https://example.com/missing",
                "title": "",
                "displayLink": "example.com",
            },
            client=_client(lambda request: httpx.Response(404, request=request)),
        )


def test_extract_json_ld_handles_arrays_and_graphs() -> None:
    array_html = '<script type="application/ld+json">[{"@type": "Organization"}, {"@type": "JobPosting", "title": "X"}]</script>'
    assert listing_module.extract_json_ld(array_html)["title"] == "X"
    graph_html = '<script type="application/ld+json">{"@graph": [{"@type": "JobPosting", "title": "G"}]}</script>'
    assert listing_module.extract_json_ld(graph_html)["title"] == "G"
    assert listing_module.extract_json_ld("<html></html>") is None


def test_strip_tags_removes_scripts_styles_and_nbsp() -> None:
    assert listing_module.strip_tags("a<script>x</script>b<style>y</style>c&nbsp;d") == "a b c d"


def test_shallow_known_stop_stops_after_quiet_pages_within_frontier() -> None:
    known = {f"https://jobs.ashbyhq.com/acme/job-{number}" for number in range(1, 30)}
    pages = [
        {
            "hasNext": True,
            "results": [
                _valid_jobs(1)[0],
                {"title": "N", "link": "https://jobs.ashbyhq.com/acme/job-99", "snippet": "s"},
            ],
        },
        {"hasNext": True, "results": _valid_jobs(2, 3)},
        {"hasNext": True, "results": _valid_jobs(4)},
    ]
    browser = FakeBrowser(pages)
    checkpoints: list = []
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "knownUrls": known,
            "shallowStop": {
                "enabled": True,
                "quietThreshold": 1,
                "quietPages": 2,
                "frontierPage": 5,
            },
            "onPage": lambda checkpoint: checkpoints.append(
                {
                    "quietStreak": checkpoint["quietStreak"],
                    "paginationStop": checkpoint["paginationStop"],
                }
            ),
        },
    )
    assert results.pagination_stop == "known_frontier"
    assert results.last_page == 2
    assert len(browser.opened) == 3
    assert any(item["link"].endswith("job-99") for item in results)
    assert [checkpoint["quietStreak"] for checkpoint in checkpoints] == [0, 1, 2]
    assert checkpoints[-1]["paginationStop"] == "known_frontier"


def test_shallow_stop_not_applied_beyond_frontier() -> None:
    known = {f"https://jobs.ashbyhq.com/acme/job-{number}" for number in range(1, 30)}
    pages = [
        {
            "hasNext": True,
            "results": [
                _valid_jobs(1)[0],
                {"title": "N", "link": "https://jobs.ashbyhq.com/acme/job-99", "snippet": "s"},
            ],
        },
        {"hasNext": True, "results": _valid_jobs(2)},
        {"hasNext": True, "results": _valid_jobs(3)},
        {"hasNext": True, "results": _valid_jobs(4)},
        {"hasNext": False, "results": _valid_jobs(5)},
    ]
    browser = FakeBrowser(pages)
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "sparsePageResults": 0,
            "knownUrls": known,
            "shallowStop": {
                "enabled": True,
                "quietThreshold": 1,
                "quietPages": 2,
                "frontierPage": 1,
            },
        },
    )
    assert results.pagination_stop == "exhausted"
    assert results.last_page == 4
    assert len(browser.opened) == 5


def test_sparse_tail_options_are_configurable() -> None:
    pages = [
        {"hasNext": True, "results": _valid_jobs(1)},
        {"hasNext": True, "results": _valid_jobs(2)},
    ]
    browser = FakeBrowser(pages)
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {"minPageDelayMs": 0, "maxPageDelayMs": 0, "sparsePageResults": 2, "sparsePages": 2},
    )
    assert results.pagination_stop == "low_yield"
    assert results.last_page == 1
    assert len(browser.opened) == 2


def test_no_shallow_stop_without_frontier() -> None:
    known = {f"https://jobs.ashbyhq.com/acme/job-{number}" for number in range(1, 30)}
    pages = [
        {"hasNext": True, "results": _valid_jobs(1)},
        {"hasNext": True, "results": _valid_jobs(2)},
        {"hasNext": False, "results": _valid_jobs(3)},
    ]
    browser = FakeBrowser(pages)
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        QUERY,
        {
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "sparsePageResults": 0,
            "knownUrls": known,
            "shallowStop": {
                "enabled": True,
                "quietThreshold": 10,
                "quietPages": 1,
                "frontierPage": None,
            },
        },
    )
    assert results.pagination_stop == "exhausted"
    assert len(browser.opened) == 3


def test_provider_resolves_google_goto_links_before_host_filter() -> None:
    """Results behind google.com/goto passthroughs survive the host filter."""

    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "Search", "bodyText": "", **page}

    pages = [
        {
            "hasNext": False,
            "results": [
                {
                    "title": "Software Engineer I, Network @ Crusoe",
                    "link": "https://www.google.com/goto?url=TOKEN_CRUSOE",
                    "snippet": "Ashby",
                },
                {
                    "title": "Direct Ashby Result",
                    "link": "https://jobs.ashbyhq.com/direct/1",
                    "snippet": "Ashby",
                },
            ],
        }
    ]
    browser = Browser(pages)
    resolutions = {
        "https://www.google.com/goto?url=TOKEN_CRUSOE": "https://jobs.ashbyhq.com/crusoe/9a5223c4-9eb7-4fdb-b97c-f43525df35ed"
    }

    def resolver(value: str) -> str:
        assert value in resolutions, f"unexpected resolution request: {value}"
        return resolutions[value]

    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        {
            "id": "probe",
            "platform": "Ashby",
            "query": "site:jobs.ashbyhq.com probe",
            "allowedHosts": ["jobs.ashbyhq.com"],
        },
        {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0, "passthroughResolver": resolver},
    )
    assert [result["link"] for result in results] == [
        "https://jobs.ashbyhq.com/crusoe/9a5223c4-9eb7-4fdb-b97c-f43525df35ed",
        "https://jobs.ashbyhq.com/direct/1",
    ]


def test_provider_passthrough_failure_keeps_result_flow_alive() -> None:
    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "Search", "bodyText": "", **page}

    pages = [
        {
            "hasNext": False,
            "results": [
                {
                    "title": "Behind broken passthrough",
                    "link": "https://www.google.com/goto?url=BROKEN",
                    "snippet": "Ashby",
                }
            ],
        }
    ]
    browser = Browser(pages)

    def broken_resolver(value: str) -> str:
        raise RuntimeError("network down")

    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        {
            "id": "probe",
            "platform": "Ashby",
            "query": "site:jobs.ashbyhq.com probe",
            "allowedHosts": ["jobs.ashbyhq.com"],
        },
        {
            "maxPages": 1,
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "passthroughResolver": broken_resolver,
        },
    )
    # Resolution failed -> the unresolvable google URL is filtered out (google host),
    # but the search itself must not crash.
    assert results == []


def test_provider_caches_passthrough_resolutions_within_a_search() -> None:
    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "Search", "bodyText": "", **page}

    pages = [
        {
            "hasNext": False,
            "results": [
                {
                    "title": "First sighting",
                    "link": "https://www.google.com/goto?url=TOKEN_A",
                    "snippet": "Ashby",
                },
                {
                    "title": "Second sighting",
                    "link": "https://www.google.com/goto?url=TOKEN_A",
                    "snippet": "Ashby",
                },
            ],
        }
    ]
    browser = Browser(pages)
    calls: list[str] = []

    def resolver(value: str) -> str:
        calls.append(value)
        return "https://jobs.ashbyhq.com/acme/job-1"

    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(
        {
            "id": "probe",
            "platform": "Ashby",
            "query": "site:jobs.ashbyhq.com probe",
            "allowedHosts": ["jobs.ashbyhq.com"],
        },
        {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0, "passthroughResolver": resolver},
    )
    assert len(results) == 1  # same target URL -> deduped
    assert calls == ["https://www.google.com/goto?url=TOKEN_A"]  # resolved once


# ---------------------------------------------------------------- consent reload


def _consent_browser(pages, banner_states):
    """FakeBrowser that lets the REAL reject_google_cookies_if_present run.

    ``evaluate`` answers the banner check; ``_ensure_page`` returns a fake page
    whose button always clicks and whose reload is recorded.
    """

    class FakePage:
        def __init__(self, outer):
            self._outer = outer

        def get_by_role(self, _role, name=""):
            outer = self._outer

            class Button:
                def count(self):
                    return 1

                def click(self):
                    outer.clicks += 1

            return Button()

        def wait_for_timeout(self, _ms):
            return None

        def reload(self, **_kwargs):
            self._outer.reloads += 1

    class Browser(FakeBrowser):
        # Bind the REAL method so this test exercises the actual
        # settle-and-reload implementation, not a stub.
        reject_google_cookies_if_present = PlaywrightBrowser.reject_google_cookies_if_present

        def __init__(self):
            super().__init__(pages)
            self.reloads = 0
            self.clicks = 0
            self._banner_checks = 0
            self._states = list(banner_states)

        def _next_banner_state(self):
            state = self._states[min(self._banner_checks, len(self._states) - 1)]
            self._banner_checks += 1
            return state

        def _ensure_page(self):
            return FakePage(self)

        def evaluate(self, expression):
            if "Before you continue" in expression:
                return self._next_banner_state()
            return super().evaluate(expression)

    return Browser()


def test_consent_dismissal_reloads_when_banner_persists():
    browser = _consent_browser(
        [{"hasNext": False, "results": _valid_jobs(1)}],
        banner_states=[True, False, False, False],  # visible -> clicked -> gone
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert browser.clicks == 1
    assert browser.reloads == 0  # banner cleared after click + settle
    assert len(results) == 1


def test_consent_dismissal_reloads_when_banner_still_present_after_settling():
    browser = _consent_browser(
        [{"hasNext": False, "results": _valid_jobs(1)}],
        banner_states=[True, True, True, True],  # click never clears it -> reload
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    results = provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert browser.clicks == 1
    assert browser.reloads == 1  # the phase-2 fix
    assert len(results) == 1


def test_consent_skips_everything_when_banner_absent():
    browser = _consent_browser(
        [{"hasNext": False, "results": _valid_jobs(1)}],
        banner_states=[False],
    )
    provider = PlaywrightGoogleSearchProvider(browser=browser)
    provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert browser.clicks == 0
    assert browser.reloads == 0


# ---------------------------------------------------------------- quiet-zero telemetry


def test_page_telemetry_reports_raw_anchors_and_zero_results():
    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "probe", "bodyText": "", **page}

    pages = [
        {
            "hasNext": False,
            "rawAnchorCount": 59,
            "results": [],  # anchors on page, none usable: the quiet-zero signature
        }
    ]
    captured: list = []
    provider = PlaywrightGoogleSearchProvider(browser=Browser(pages))
    provider.search(
        QUERY,
        {
            "maxPages": 1,
            "minPageDelayMs": 0,
            "maxPageDelayMs": 0,
            "onPage": lambda checkpoint: captured.append(checkpoint),
        },
    )
    assert len(captured) == 1
    page_info = captured[0]["page"]
    assert page_info["rawAnchorCount"] == 59
    assert page_info["resultAnchors"] == 0
    assert page_info["validResults"] == 0
    assert page_info["requestedUrl"].startswith("https://www.google.com/search?q=")
    assert page_info["servedUrl"] == "https://google.com/search"
    assert page_info["serpTitle"] == "probe"


def test_quiet_zero_flag_set_when_results_present_but_all_filtered():
    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "probe", "bodyText": "", **page}

    pages = [
        {
            "hasNext": False,
            "rawAnchorCount": 59,
            "results": [
                {"title": "Wrong domain", "link": "https://example.com/x", "snippet": ""},
            ],
        }
    ]
    provider = PlaywrightGoogleSearchProvider(browser=Browser(pages))
    results = provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert results == []  # filtered out
    assert provider.browser_quiet_zero(results) is True


def test_no_quiet_zero_flag_on_a_genuinely_empty_serp():
    class Browser(FakeBrowser):
        def evaluate(self, expression):
            page = self._pages.pop(0)
            return {"url": "https://google.com/search", "title": "probe", "bodyText": "", **page}

    pages = [{"hasNext": False, "rawAnchorCount": 8, "results": []}]
    provider = PlaywrightGoogleSearchProvider(browser=Browser(pages))
    results = provider.search(QUERY, {"maxPages": 1, "minPageDelayMs": 0, "maxPageDelayMs": 0})
    assert results == []
    assert provider.browser_quiet_zero(results) is False
