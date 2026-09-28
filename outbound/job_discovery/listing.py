"""HTTP listing reader ported from ``job_discovery/src/listing-reader.mjs``.

The reference used regex extraction (``<title>`` + JSON-LD scan + tag
stripping); this port keeps the same semantics so identical HTML produces
identical fields.
"""

from __future__ import annotations

import json
import re

import httpx

from outbound.job_discovery.urls import canonicalize_url

_JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>", re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.IGNORECASE)


def strip_tags(value: str = "") -> str:
    """Port of the reference ``stripTags`` (scripts/styles removed, tags spaced)."""
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("&nbsp;", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _is_job_posting(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    type_value = item.get("@type")
    if type_value == "JobPosting":
        return True
    if isinstance(type_value, str):
        return "JobPosting" in type_value
    if isinstance(type_value, list):
        return "JobPosting" in type_value
    return False


def extract_json_ld(html: str):
    """First parseable JobPosting schema in the document (or None)."""
    for block in _JSON_LD_RE.findall(html):
        try:
            parsed = json.loads(block)
        except ValueError:
            continue  # Ignore malformed third-party schema.
        if isinstance(parsed, list):
            values = parsed
        elif isinstance(parsed, dict):
            graph = parsed.get("@graph")
            values = [parsed, *(graph if isinstance(graph, list) else [])]
        else:
            continue
        job = next((item for item in values if _is_job_posting(item)), None)
        if job is not None:
            return job
    return None


def read_listing(
    candidate: dict, *, client: httpx.Client | None = None, timeout_ms: int = 15_000
) -> dict:
    """Port of ``readListing``: fetch + JSON-LD/text extraction."""
    owns_client = client is None
    http = client or httpx.Client()
    try:
        response = http.get(
            candidate["canonicalUrl"],
            headers={"Accept": "text/html,application/xhtml+xml"},
            timeout=timeout_ms / 1000,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Listing returned HTTP {response.status_code}")
        html = response.text
        final_url = str(response.url)
    finally:
        if owns_client:
            http.close()
    schema = extract_json_ld(html)
    title_match = _TITLE_RE.search(html)
    title = (
        (schema or {}).get("title")
        or candidate.get("title")
        or strip_tags(title_match.group(1) if title_match else "")
        or "Untitled job"
    )
    description = (
        strip_tags(schema.get("description"))
        if isinstance(schema, dict) and schema.get("description")
        else strip_tags(html)[:35_000]
    )
    organization = schema.get("hiringOrganization") if isinstance(schema, dict) else None
    company = organization.get("name") if isinstance(organization, dict) else None
    if not company:
        company = re.sub(r"^www\.", "", candidate.get("displayLink") or "").split(".")[0]
    location = ""
    job_location = schema.get("jobLocation") if isinstance(schema, dict) else None
    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            location = address.get("addressLocality") or ""
    if not location:
        requirements = (
            schema.get("applicantLocationRequirements") if isinstance(schema, dict) else None
        )
        if isinstance(requirements, dict):
            location = requirements.get("name") or ""
    return {
        "title": title,
        "description": description,
        "company": company,
        "location": location,
        "canonicalUrl": canonicalize_url(final_url or candidate["canonicalUrl"]),
    }
