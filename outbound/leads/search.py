"""Web search client and result parsers for the leads workflow.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S5). Pure move.
"""

import html
import random
import re
import time
import urllib.parse
import urllib.request

from outbound.leads.text import clean_text, compact_company_name, strip_accents


class SearchClient:
    def __init__(self, delay_min: float = 2.0, delay_max: float = 5.0, timeout: int = 25):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self.last_request_at = 0.0

    def search(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        self._delay()
        errors = []
        for engine in ("bing", "yahoo", "duckduckgo"):
            try:
                results = self._search_engine(engine, query, limit)
                self.last_request_at = time.time()
                return results
            except Exception as exc:
                errors.append(f"{engine}: {exc}")
        raise RuntimeError("; ".join(errors))

    def _search_engine(self, engine: str, query: str, limit: int) -> list[dict[str, str]]:
        if engine == "bing":
            url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
        elif engine == "yahoo":
            url = "https://search.yahoo.com/search?" + urllib.parse.urlencode({"p": query})
        else:
            url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", errors="ignore")
        if engine == "bing":
            return parse_bing_results(body, limit=limit)
        if engine == "yahoo":
            return parse_yahoo_results(body, limit=limit)
        return parse_duckduckgo_results(body, limit=limit)

    def _delay(self) -> None:
        if self.last_request_at <= 0:
            return
        target = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.time() - self.last_request_at
        if elapsed < target:
            time.sleep(target - elapsed)


def parse_duckduckgo_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>', body)
    for block in blocks:
        if "result__a" not in block:
            continue
        link_match = re.search(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not link_match:
            continue
        raw_url = html.unescape(link_match.group(1))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S
        )
        if not snippet_match:
            snippet_match = re.search(
                r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>', block, re.S
            )
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        url = unwrap_duckduckgo_url(raw_url)
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_bing_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>', body)
    for block in blocks:
        link_match = re.search(
            r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>\s*</h2>", block, re.S
        )
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_yahoo_results(body: str, limit: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blocks = re.split(r'<li>\s*<div[^>]+class="[^"]*\balgo\b[^"]*"[^>]*>', body)
    for block in blocks:
        link_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', block, re.S)
        if not link_match:
            continue
        url = unwrap_yahoo_url(html.unescape(link_match.group(1)))
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(
            r'<div[^>]+class="[^"]*\bcompText\b[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.S
        )
        snippet = strip_tags(snippet_match.group(1)) if snippet_match else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def unwrap_yahoo_url(raw_url: str) -> str:
    match = re.search(r"/RU=([^/]+)/", raw_url)
    if match:
        return urllib.parse.unquote(match.group(1))
    return raw_url


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html.unescape(text))


def unwrap_duckduckgo_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return html.unescape(query["uddg"][0])
    return raw_url


def search_query_variants(person_name: str, company_name: str) -> list[str]:
    name = clean_text(person_name)
    company = clean_text(company_name)
    short_company = compact_company_name(company)
    ascii_name = clean_text(strip_accents(name))
    ascii_company = clean_text(strip_accents(short_company))
    candidates = [
        f'"{name}" "{company}" LinkedIn',
        f"{name} {company} LinkedIn",
        f'"{name}" "{short_company}" LinkedIn',
        f"{name} {short_company} LinkedIn",
    ]
    if ascii_name != name or ascii_company != short_company:
        candidates.extend(
            [
                f'"{ascii_name}" "{ascii_company}" LinkedIn',
                f"{ascii_name} {ascii_company} LinkedIn",
            ]
        )
    seen = set()
    variants = []
    for query in candidates:
        query = clean_text(query)
        key = query.lower()
        if query and key not in seen:
            variants.append(query)
            seen.add(key)
    return variants
