"""Playwright browser + Google search + listing reader.

Port of ``job_discovery/src/playwright-provider.mjs``. The one intentional
transport change (approved in the port plan): the reference drove the
``playwright-cli`` shell; this port uses the ``playwright`` Python package
directly (sync API). The page-side extraction snippets are kept verbatim from
the reference so DOM behavior stays identical; orchestration that was passed as
``run-code`` strings (scrolling, dwell, cookie rejection) is expressed as
regular methods on the browser wrapper.

The ``playwright`` import is lazy: smoke tests and non-browser commands never
pay for it, mirroring how the reference kept the browser external to the core.

Pacing helpers are intentionally faithful (including the sparse-tail rule and
the zero-page-ceiling default); sleeps are synchronous, as the runner is.
"""

from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from outbound.job_discovery.urls import canonicalize_url, is_career_landing_page_url

_CHALLENGE_RE = re.compile(
    r"/sorry/|unusual traffic|verify (?:that )?you(?:'re| are) human|not a robot|captcha",
    re.IGNORECASE,
)

_EXTRACT_RESULTS_SCRIPT = r"""(() => {
  const unwrap = (value) => {
    try { const url = new URL(value, location.href); return url.hostname.endsWith('google.com') && url.pathname === '/url' ? (url.searchParams.get('q') || url.searchParams.get('url') || value) : url.href; }
    catch { return value; }
  };
  return {
    url: location.href,
    title: document.title,
    bodyText: document.body.innerText.slice(0, 12000),
    hasNext: Boolean(document.querySelector('#pnnext, a[aria-label="Next page"], a[aria-label^="Next"]')),
    results: [...document.querySelectorAll('a')].flatMap((anchor) => {
      const heading = anchor.querySelector('h3');
      if (!heading) return [];
      const container = anchor.closest('div');
      return [{ title: heading.innerText.trim(), link: unwrap(anchor.href), snippet: (container?.parentElement?.innerText || container?.innerText || '').replace(/\s+/g, ' ').trim() }];
    })
  };
})()"""

_EXTRACT_LISTING_SCRIPT = r"""(() => {
  const body = document.body.innerText.slice(0, 100000);
  let schema;
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const parsed = JSON.parse(script.textContent);
      const values = Array.isArray(parsed) ? parsed : [parsed, ...(Array.isArray(parsed?.['@graph']) ? parsed['@graph'] : [])];
      schema = values.find((item) => item?.['@type'] === 'JobPosting' || item?.['@type']?.includes?.('JobPosting'));
      if (schema) break;
    } catch { /* malformed third-party schema */ }
  }
  const address = Array.isArray(schema?.jobLocation) ? schema.jobLocation[0]?.address : schema?.jobLocation?.address;
  return {
    title: schema?.title || document.querySelector('h1')?.innerText?.trim() || document.title,
    description: schema?.description ? new DOMParser().parseFromString(schema.description, 'text/html').body.innerText : body,
    company: schema?.hiringOrganization?.name || '',
    location: address?.addressLocality || schema?.applicantLocationRequirements?.name || (body.match(/Location\s+([^\n]+)/i) || [])[1] || '',
    closed: /job (?:is )?no longer available|position (?:has been|is) filled|application closed/i.test(body),
    blocked: /access denied|verify (?:that )?you(?:'re| are) human|captcha|checking your browser/i.test(body),
    isJobPosting: Boolean(schema) || !/\bopen positions?\s*\(\d+\)/i.test(body)
  };
})()"""


class SearchBlockedError(RuntimeError):
    """Google presented a verification/unusual-traffic page; query stays pending."""

    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "SearchBlockedError"


class SearchSafetyLimitError(RuntimeError):
    """A configured pagination safety ceiling was reached."""

    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "SearchSafetyLimitError"


def next_pagination_state(
    previous_sparse_pages: int,
    valid_result_count: int,
    *,
    sparse_page_results: int = 2,
    sparse_pages: int = 3,
) -> dict:
    """Sparse-tail counter (docs/SEARCH-DEPTH-FIX.md): consecutive pages with fewer
    than ``sparse_page_results`` valid results; ``sparse_pages`` such pages complete
    a query."""
    sparse = previous_sparse_pages + 1 if valid_result_count < sparse_page_results else 0
    return {"sparseStreak": sparse, "complete": sparse >= sparse_pages}


def _number(value: object, fallback: float) -> float:
    """JS ``Number(value)`` with a fallback for None/unparsable (keeps explicit 0)."""
    if value is None:
        return fallback
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return int(number) if number.is_integer() else number


def _num_or(value: object, fallback: float) -> float:
    """JS ``Number(value) || fallback`` (0 and NaN are falsy)."""
    parsed = _number(value, math.nan)  # type: ignore[arg-type]
    if isinstance(parsed, float) and math.isnan(parsed):
        return fallback
    if parsed == 0:
        return fallback
    return parsed


def _random_between(minimum: float, maximum: float) -> float:
    """JS ``randomBetween`` inclusive range."""
    return minimum + math.floor(random.random() * max(1, maximum - minimum + 1))


def _js_number(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _encode_uri_component(value: str) -> str:
    """``encodeURIComponent`` equivalent (quote keeps ``_.-~``; add ``!*'()``)."""
    return quote(value, safe="!*'()")


class PlaywrightBrowser:
    """Wraps a Playwright persistent-context session (direct Python API).

    Mirrors the reference's visible, persistent local profile: cookies and
    consent state survive across runs. This is deliberately ordinary browser
    state retention -- not an automation-concealment mechanism.
    """

    def __init__(
        self,
        *,
        headed: bool = True,
        profile_path: str = "./data/browser-profile",
        timeout_ms: int = 45_000,
        record_video_dir: str | None = None,
    ) -> None:
        self.headed = headed
        self.profile_path = str(Path(profile_path).resolve()) if profile_path else ""
        self.timeout_ms = timeout_ms
        # Optional video capture for verification/debug runs (one file per page).
        self.record_video_dir = str(Path(record_video_dir).resolve()) if record_video_dir else None
        self._playwright = None
        self._owns_playwright = False
        self._context = None
        self._page = None
        self.started = False

    def _ensure_playwright(self):
        if self._playwright is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._owns_playwright = True
        return self._playwright

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        playwright = self._ensure_playwright()
        video = {"record_video_dir": self.record_video_dir} if self.record_video_dir else {}
        if self.profile_path:
            self._context = playwright.chromium.launch_persistent_context(
                self.profile_path, headless=not self.headed, **video
            )
        else:
            browser = playwright.chromium.launch(headless=not self.headed)
            self._context = browser.new_context(**video)
        self._context.set_default_timeout(self.timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def open(self, url: str) -> None:
        page = self._ensure_page()
        page.goto(url)
        self.started = True

    def evaluate(self, expression: str):
        return self._ensure_page().evaluate(expression)

    def reject_google_cookies_if_present(self) -> None:
        has_banner = self.evaluate(
            "document.body.innerText.includes('Before you continue to Google')"
        )
        if has_banner:
            button = self._ensure_page().get_by_role("button", name="Reject all")
            if button.count():
                button.click()

    def scroll_search_results(self) -> None:
        page = self._ensure_page()
        metrics = page.evaluate(
            "() => ({ height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight), viewport: Math.max(window.innerHeight, 1) })"
        )
        steps = min(4, max(1, math.ceil(metrics["height"] / metrics["viewport"]) - 1))
        for step in range(1, steps + 1):
            page.evaluate(
                "top => window.scrollTo({ top, behavior: 'smooth' })",
                min(metrics["height"], step * metrics["viewport"]),
            )
            page.wait_for_timeout(650 + random.randint(0, 649))
        page.evaluate("() => window.scrollTo({ top: 0, behavior: 'smooth' })")
        page.wait_for_timeout(450 + random.randint(0, 499))

    def dwell_and_scroll(self, dwell_ms: float) -> None:
        page = self._ensure_page()
        page.wait_for_timeout(dwell_ms)
        metrics = page.evaluate(
            "() => ({ height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight), viewport: window.innerHeight })"
        )
        target = min(
            max(0, metrics["height"] - metrics["viewport"]),
            math.floor(metrics["viewport"] * 1.4 + 0.5),
        )
        if target > 0:
            page.evaluate("top => window.scrollTo({ top, behavior: 'smooth' })", target)
            page.wait_for_timeout(600 + random.randint(0, 699))

    def close(self) -> None:
        if not self.started and self._context is None:
            return
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._owns_playwright and self._playwright is not None:
                self._playwright.stop()
            self._playwright = None
            self._owns_playwright = False
            self._context = None
            self._page = None
            self.started = False


class PlaywrightGoogleSearchProvider:
    """Port of ``PlaywrightGoogleSearchProvider`` (paced, checkpointed, resumable)."""

    def __init__(
        self, *, browser: PlaywrightBrowser | None = None, max_results: int | None = None
    ) -> None:
        self.browser = browser if browser is not None else PlaywrightBrowser()
        self.max_results = max_results

    def search(self, query: dict, options: dict | None = None) -> list:
        options = options or {}
        resume = options.get("resume") or {}
        all_results = SearchResults()
        seen_urls: set[str] = set()
        for result in resume.get("partialResults") or []:
            try:
                link = canonicalize_url(result.get("link") or "")
            except ValueError:
                continue  # discard malformed checkpoint URLs
            if link not in seen_urls:
                seen_urls.add(link)
                all_results.append({**result, "link": link})
        page_number = _num_or(resume.get("nextPage"), 0)
        sparse_streak = _num_or(resume.get("sparseStreak"), _num_or(resume.get("thinPages"), 0))
        quiet_streak = _num_or(resume.get("quietStreak"), 0)
        # Tail rule: ``sparsePages`` consecutive pages with fewer than ``sparsePageResults``
        # valid results complete a query (docs/SEARCH-DEPTH-FIX.md). A zero page ceiling
        # means paginate until Google has no next page, the tail rule, or a shallow stop.
        sparse_page_results = max(0, int(_number(options.get("sparsePageResults"), 2)))
        sparse_pages_limit = max(1, int(_number(options.get("sparsePages"), 3)))
        shallow = options.get("shallowStop") or {}
        shallow_enabled = bool(shallow.get("enabled"))
        quiet_threshold = max(0, int(_number(shallow.get("quietThreshold"), 10)))
        quiet_pages_limit = max(1, int(_number(shallow.get("quietPages"), 3)))
        frontier_page = shallow.get("frontierPage")
        known_urls = set(options.get("knownUrls") or [])
        maximum_pages = max(0, _num_or(options.get("maxPages"), 0))
        maximum_ms = max(60_000, _num_or(options.get("maxMinutes"), 20) * 60_000)
        started_at = time.time() * 1000
        pagination_stop = ""
        while True:
            if maximum_pages and page_number >= maximum_pages:
                raise SearchSafetyLimitError(
                    f"Query reached the {_js_number(maximum_pages)}-page safety limit"
                )
            if time.time() * 1000 - started_at >= maximum_ms:
                raise SearchSafetyLimitError(
                    f"Query reached the {_js_number(math.floor(maximum_ms / 60_000 + 0.5))}-minute safety limit"
                )
            search_url = (
                f"https://www.google.com/search?q={_encode_uri_component(query.get('query') or '')}"
                f"&start={_js_number(page_number * 10)}"
            )
            self.browser.open(search_url)
            self.browser.reject_google_cookies_if_present()
            scroll = getattr(self.browser, "scroll_search_results", None)
            if scroll:
                scroll()
            page = self.browser.evaluate(_EXTRACT_RESULTS_SCRIPT)
            challenge_text = (
                f"{page.get('url') or ''}\n{page.get('title') or ''}\n{page.get('bodyText') or ''}"
            )
            if _CHALLENGE_RE.search(challenge_text):
                raise SearchBlockedError(
                    "Google presented a verification or unusual-traffic page; query remains pending"
                )
            allowed_hosts = (
                query.get("allowedHosts")
                if query.get("allowedHosts")
                else [
                    match.lower()
                    for match in re.findall(
                        r"site:([a-z0-9.-]+)", query.get("query") or "", re.IGNORECASE
                    )
                ]
            )

            def host_allowed(hostname: str) -> bool:
                return not allowed_hosts or any(
                    hostname == allowed or hostname.endswith("." + allowed)
                    for allowed in allowed_hosts
                )

            resolved = []
            for result in page.get("results") or []:
                if self.max_results and len(all_results) + len(resolved) >= self.max_results:
                    break
                try:
                    canonical = canonicalize_url(result.get("link") or "")
                except ValueError:
                    continue
                parts = urlsplit(canonical)
                hostname = (parts.hostname or "").lower()
                if (
                    parts.scheme not in ("http", "https")
                    or hostname.endswith("google.com")
                    or not host_allowed(hostname)
                    or canonical in seen_urls
                ):
                    continue
                seen_urls.add(canonical)
                resolved.append(
                    {
                        "title": result.get("title"),
                        "snippet": result.get("snippet"),
                        "link": canonical,
                        "displayLink": hostname,
                    }
                )
            all_results.extend(resolved)
            new_count = sum(1 for item in resolved if item["link"] not in known_urls)
            quiet_streak = (
                quiet_streak + 1 if shallow_enabled and new_count < quiet_threshold else 0
            )
            state = next_pagination_state(
                sparse_streak,
                len(resolved),
                sparse_page_results=sparse_page_results,
                sparse_pages=sparse_pages_limit,
            )
            sparse_streak = state["sparseStreak"]
            reached_result_limit = bool(self.max_results and len(all_results) >= self.max_results)
            if reached_result_limit:
                pagination_stop = "result_limit"
            elif not page.get("hasNext"):
                pagination_stop = "exhausted"
            elif state["complete"]:
                pagination_stop = "low_yield"
            elif (
                shallow_enabled
                and frontier_page is not None
                and quiet_streak >= quiet_pages_limit
                and page_number <= int(frontier_page)
            ):
                pagination_stop = "known_frontier"
            on_page = options.get("onPage")
            if on_page:
                on_page(
                    {
                        "partialResults": all_results,
                        "nextPage": page_number + 1,
                        "thinPages": sparse_streak,  # legacy alias for ``sparseStreak``
                        "sparseStreak": sparse_streak,
                        "quietStreak": quiet_streak,
                        "newResults": new_count,
                        "lastPage": page_number,
                        "validResults": len(resolved),
                        "paginationStop": pagination_stop,
                    }
                )
            if pagination_stop:
                break
            page_number += 1
            time.sleep(
                _random_between(
                    _num_or(options.get("minPageDelayMs"), 0),
                    _num_or(
                        options.get("maxPageDelayMs"), _num_or(options.get("minPageDelayMs"), 0)
                    ),
                )
                / 1000
            )
            page_burst_size = max(1, _num_or(options.get("searchPageBurstSize"), 0))
            if page_burst_size > 0 and page_number % page_burst_size == 0:
                time.sleep(
                    _random_between(
                        _num_or(options.get("minSearchPageCooldownMs"), 0),
                        _num_or(
                            options.get("maxSearchPageCooldownMs"),
                            _num_or(options.get("minSearchPageCooldownMs"), 0),
                        ),
                    )
                    / 1000
                )
        all_results.pagination_stop = pagination_stop
        all_results.last_page = page_number
        return all_results

    def close(self) -> None:
        self.browser.close()


class SearchResults(list):
    """List carrying the reference's ``paginationStop`` array property."""

    def __init__(self, values=(), pagination_stop: str = "") -> None:
        super().__init__(values)
        self.pagination_stop = pagination_stop
        self.last_page: int | None = None


def create_playwright_listing_reader(browser):
    """Port of ``createPlaywrightListingReader`` (dwell + JSON-LD extraction)."""

    def read_listing(candidate: dict, options: dict | None = None) -> dict:
        options = options or {}
        min_dwell_ms = _number(options.get("minDwellMs"), 4_000)
        max_dwell_ms = _number(options.get("maxDwellMs"), 7_000)
        if is_career_landing_page_url(
            candidate.get("canonicalUrl", ""), candidate.get("platform", "")
        ):
            return {
                "isJobPosting": False,
                "canonicalUrl": candidate["canonicalUrl"],
                "exclusionReason": "company_careers_landing_page",
            }
        browser.open(candidate["canonicalUrl"])
        dwell_ms = _random_between(min_dwell_ms, max(min_dwell_ms, max_dwell_ms))
        browser.dwell_and_scroll(dwell_ms)
        listing = browser.evaluate(_EXTRACT_LISTING_SCRIPT)
        if listing.get("blocked"):
            raise RuntimeError("ATS page presented an access or verification challenge")
        if not listing.get("isJobPosting"):
            return {
                **listing,
                "canonicalUrl": candidate["canonicalUrl"],
                "exclusionReason": "careers_landing_page",
            }
        parts = urlsplit(candidate["canonicalUrl"])
        segments = [segment for segment in parts.path.split("/") if segment]
        path_part = segments[0] if segments else ""
        hostname = parts.hostname or ""
        tenant = hostname.split(".")[0] if hostname else ""
        if re.search(
            r"(?:myworkdayjobs|recruitee|breezy|pinpointhq)\.com$|breezy\.hr$",
            hostname,
            re.IGNORECASE,
        ) and not re.fullmatch(r"(www|jobs|apply|career)", tenant, re.IGNORECASE):
            inferred = tenant
        else:
            inferred = path_part or candidate.get("displayLink") or ""
        company = listing.get("company") or re.sub(
            r"\b\w", lambda match: match.group(0).upper(), re.sub(r"[-_]", " ", inferred)
        )
        return {**listing, "company": company, "canonicalUrl": candidate["canonicalUrl"]}

    return read_listing
