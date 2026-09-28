"""Search-provider ports from ``job_discovery/src/search-provider.mjs``.

Also exposes ``SearchResults`` (the checkpoint-carrying list returned by the
Google provider) and ``with_pagination_stop`` for resumed checkpoints, matching
the runner's use of the reference's ``asArrayWithStop`` helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from outbound.job_discovery.browser import (
    PlaywrightBrowser,
    PlaywrightGoogleSearchProvider,
    SearchResults,
    create_playwright_listing_reader,
)


def with_pagination_stop(values, pagination_stop: str) -> SearchResults:
    """Reference ``asArrayWithStop``: copy values, attach the stop reason."""
    return SearchResults(values, pagination_stop)


def _default_fixture_path() -> Path:
    # The JS reference stays the single source of truth for fixture data until
    # cutover; the file moves into this package when the reference is archived.
    return (
        Path(__file__).resolve().parents[2] / "job_discovery" / "fixtures" / "search-results.json"
    )


class FixtureSearchProvider:
    """Smoke-test transport: reads canned SERPs from a fixture file."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path = Path(file_path) if file_path else _default_fixture_path()

    def search(self, query: dict) -> list:
        fixture = json.loads(self.file_path.read_text(encoding="utf-8"))
        return fixture.get(query.get("id")) or []


class GoogleCustomSearchProvider:
    """Google Custom Search JSON API (10 results per page)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        engine_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.engine_id = engine_id
        self._owns_client = client is None
        self._client = client or httpx.Client()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(self, query: dict) -> list:
        response = self._client.get(
            "https://customsearch.googleapis.com/customsearch/v1",
            params={
                "key": self.api_key,
                "cx": self.engine_id,
                "q": query.get("query", ""),
                "num": "10",
            },
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Search API returned HTTP {response.status_code}")
        body = response.json()
        return [
            {
                "title": item.get("title"),
                "link": item.get("link"),
                "snippet": item.get("snippet"),
                "displayLink": item.get("displayLink"),
            }
            for item in (body.get("items") or [])
        ]


class HttpSearchProvider:
    """Generic adapters: POST ``{query, queryId}`` -> ``{results: [...]}``."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(self, query: dict) -> list:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        response = self._client.post(
            self.endpoint,
            json={"query": query.get("query", ""), "queryId": query.get("id", "")},
            headers=headers,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Search provider returned HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body.get("results"), list):
            raise RuntimeError("Search provider response must include a results array")
        return body["results"]


def create_search_provider(environment: dict):
    """Port of ``createSearchProvider`` (fixture is the default)."""
    if environment.get("SEARCH_PROVIDER") == "google-cse":
        return GoogleCustomSearchProvider(
            api_key=environment.get("GOOGLE_CSE_API_KEY"),
            engine_id=environment.get("GOOGLE_CSE_ID"),
        )
    if environment.get("SEARCH_PROVIDER") == "http":
        return HttpSearchProvider(
            endpoint=environment.get("SEARCH_API_URL"),
            token=environment.get("SEARCH_API_TOKEN"),
        )
    if environment.get("SEARCH_PROVIDER") == "playwright-google":
        return PlaywrightGoogleSearchProvider(
            browser=PlaywrightBrowser(
                headed=environment.get("PLAYWRIGHT_HEADED") != "false",
                profile_path=environment.get("PLAYWRIGHT_PROFILE_DIR") or "./data/browser-profile",
            )
        )
    return FixtureSearchProvider()


def create_listing_reader(environment: dict, search_provider):
    """Port of ``createListingReader`` (browser reader only for Playwright)."""
    if environment.get("SEARCH_PROVIDER") == "playwright-google" and getattr(
        search_provider, "browser", None
    ):
        return create_playwright_listing_reader(search_provider.browser)
    return None
