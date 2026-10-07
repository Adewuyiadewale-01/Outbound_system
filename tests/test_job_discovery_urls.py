"""Tests for the ported URL helpers (``job_discovery/src/url.mjs``).

Every canonicalization expectation below was produced by running the reference
implementation under Node v22 (extraction probes in the Phase 2 golden harness);
the exhaustive cross-language comparison lives in the same harness.
"""

from __future__ import annotations

import pytest

from outbound.job_discovery import urls

CANONICALIZE_CASES = [
    # Structure: host case, trailing slashes, fragments, ports.
    ("https://Example.COM/Path/", "https://example.com/Path"),
    ("https://example.com", "https://example.com/"),
    ("https://example.com/", "https://example.com/"),
    ("https://example.com/a///", "https://example.com/a"),
    ("https://example.com/#frag", "https://example.com/"),
    ("https://example.com/x#", "https://example.com/x"),
    ("https://example.com/x?", "https://example.com/x?"),
    ("https://example.com:443/x", "https://example.com/x"),
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:8443/x/", "https://example.com:8443/x"),
    ("https://example.com:/x", "https://example.com/x"),
    ("HTTPS://Example.COM/Path/", "https://example.com/Path"),
    # Path encodings (raw vs percent-encoded).
    ("https://example.com/a%2fb", "https://example.com/a%2fb"),
    ("https://example.com/a%2Fb", "https://example.com/a%2Fb"),
    ("https://example.com/a%20b", "https://example.com/a%20b"),
    ("https://example.com/a b", "https://example.com/a%20b"),
    ("https://example.com/a\u00e9/b", "https://example.com/a%C3%A9/b"),
    (
        "https://example.com/a|b/c^d/e`f/g{h}i<j>k",
        "https://example.com/a|b/c%5Ed/e%60f/g%7Bh%7Di%3Cj%3Ek",
    ),
    ("https://example.com/a%zzb", "https://example.com/a%zzb"),
    ("https://example.com/a+b", "https://example.com/a+b"),
    (r"https://example.com/a[b]/c\d", "https://example.com/a[b]/c/d"),
    ('https://example.com/a"b', "https://example.com/a%22b"),
    (
        "https://example.com/a'b(c)*d,e;f:g@h=i$j!k~l",
        "https://example.com/a'b(c)*d,e;f:g@h=i$j!k~l",
    ),
    # Dot-segment resolution.
    ("https://example.com/a/./b", "https://example.com/a/b"),
    ("https://example.com/a/../b", "https://example.com/b"),
    ("https://example.com/x/..", "https://example.com/"),
    ("https://example.com/..", "https://example.com/"),
    ("https://example.com/a//b", "https://example.com/a//b"),
    ("https://example.com/a/b/..", "https://example.com/a"),
    # Query without tracking params: parse-time encoding only.
    ("https://example.com/?q=a b", "https://example.com/?q=a%20b"),
    ("https://example.com/?q=a+b", "https://example.com/?q=a+b"),
    ("https://example.com/?q=a%20b", "https://example.com/?q=a%20b"),
    ("https://example.com/?q=a%2Cb", "https://example.com/?q=a%2Cb"),
    ("https://example.com/?q=~!*'()", "https://example.com/?q=~!*%27()"),
    ("https://example.com/?q=a%2fb", "https://example.com/?q=a%2fb"),
    ("https://example.com/?a=1&b", "https://example.com/?a=1&b"),
    ("https://example.com/?=v&a=", "https://example.com/?=v&a="),
    ("https://example.com/?a=1&&b=2", "https://example.com/?a=1&&b=2"),
    ("https://example.com/?a=1&b=2&", "https://example.com/?a=1&b=2&"),
    ("https://example.com/?a=%zz", "https://example.com/?a=%zz"),
    ("https://example.com/?q=\u00e9", "https://example.com/?q=%C3%A9"),
    ("https://example.com/?q=caf\u00e9&q=2", "https://example.com/?q=caf%C3%A9&q=2"),
    ("https://example.com/?&", "https://example.com/?&"),
    ("https://example.com/?", "https://example.com/?"),
    ("https://example.com/?q=%C3%A9", "https://example.com/?q=%C3%A9"),
    ("https://example.com/?q={}|^`[]", "https://example.com/?q={}|^`[]"),
    (r"https://example.com/?q=a\b", r"https://example.com/?q=a\b"),
    # Query WITH tracking params: full re-serialization of the remainder.
    ("https://example.com/?utm_source=x&a=1&ref=y&b=2", "https://example.com/?a=1&b=2"),
    ("https://example.com/?utm_source=x&q=a b", "https://example.com/?q=a+b"),
    ("https://example.com/?utm_source=x&q=a+b", "https://example.com/?q=a+b"),
    ("https://example.com/?utm_source=x&z=~!*'()", "https://example.com/?z=%7E%21*%27%28%29"),
    ("https://example.com/?a=1&utm_source=x&b=2", "https://example.com/?a=1&b=2"),
    ("https://example.com/?utm_source=a&utm_source=b&q=1", "https://example.com/?q=1"),
    ("https://example.com/?utm_source=x", "https://example.com/"),
    ("https://example.com/?utm_source=x&", "https://example.com/"),
    ("https://example.com/?a=1&&utm_source=x&b=2", "https://example.com/?a=1&b=2"),
    ("https://example.com/?UTM_SOURCE=X&q=1", "https://example.com/?q=1"),
    ("https://example.com/?utm_x=1&q=2", "https://example.com/?q=2"),
    ("https://example.com/?q=a%2Fb&utm_source=x", "https://example.com/?q=a%2Fb"),
    ("https://example.com/?q=\u00e9&utm_source=x", "https://example.com/?q=%C3%A9"),
    ("https://example.com/?q=+&utm_source=x", "https://example.com/?q=+"),
    ("https://example.com/?q=%20&utm_source=x", "https://example.com/?q=+"),
    ("https://example.com/?a&utm_source=x&b", "https://example.com/?a=&b="),
    ("https://example.com/?a=%zz&utm_source=x", "https://example.com/?a=%25zz"),
    (
        "https://example.com/?utm_source=x&gh_src=ABC&display=en",
        "https://example.com/?gh_src=ABC&display=en",
    ),
    ("https://example.com/?q=a%2fb&utm_source=x", "https://example.com/?q=a%2Fb"),
    ("https://example.com/?q=%2B%2b&utm_source=x", "https://example.com/?q=%2B%2B"),
    ("https://example.com/?utm_source=x&q={}|^`[]", "https://example.com/?q=%7B%7D%7C%5E%60%5B%5D"),
    (r"https://example.com/?utm_source=x&q=a\b", "https://example.com/?q=a%5Cb"),
    ("https://example.com/?utm_source=x&q=a/../b", "https://example.com/?q=a%2F..%2Fb"),
    # Real-world shapes (untracked params are preserved).
    ("https://jobs.ashbyhq.com/cradlebio", "https://jobs.ashbyhq.com/cradlebio"),
    (
        "https://jobs.ashbyhq.com/tyba/6bf9a202-0b83-44c8-b06f-8f258fa8057e?utm_medium=x",
        "https://jobs.ashbyhq.com/tyba/6bf9a202-0b83-44c8-b06f-8f258fa8057e",
    ),
    (
        "https://job-boards.greenhouse.io/acme/jobs/4567890?gh_src=abc123",
        "https://job-boards.greenhouse.io/acme/jobs/4567890?gh_src=abc123",
    ),
    (
        "https://jobs.lever.co/acme/1234abcd-56ef-78ab-cdef-1234567890ab/"
        "apply?lever-origin=applied&lever-source%5B%5D=LinkedIn",
        "https://jobs.lever.co/acme/1234abcd-56ef-78ab-cdef-1234567890ab/"
        "apply?lever-origin=applied&lever-source%5B%5D=LinkedIn",
    ),
    (
        "https://apply.workable.com/acme/j/ABCDEF1234/",
        "https://apply.workable.com/acme/j/ABCDEF1234",
    ),
    (
        "https://jobs.smartrecruiters.com/Acme/744000012345678?oga=true",
        "https://jobs.smartrecruiters.com/Acme/744000012345678?oga=true",
    ),
    (
        "https://career.teamtailor.com/jobs/1234567-backend-engineer?utm_campaign=google_jobs_apply",
        "https://career.teamtailor.com/jobs/1234567-backend-engineer",
    ),
    (
        "https://acme.recruitee.com/o/backend-engineer",
        "https://acme.recruitee.com/o/backend-engineer",
    ),
    ("https://acme.pinpointhq.com/postings/12345", "https://acme.pinpointhq.com/postings/12345"),
    (
        "https://acme.breezy.hr/p/abc123-backend-engineer",
        "https://acme.breezy.hr/p/abc123-backend-engineer",
    ),
    (
        "https://www.comeet.com/jobs/acme/12.345/backend-engineer/AB.CD",
        "https://www.comeet.com/jobs/acme/12.345/backend-engineer/AB.CD",
    ),
    (
        "https://acme.jobs.personio.com/job/1234567?display=en",
        "https://acme.jobs.personio.com/job/1234567?display=en",
    ),
    (
        "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Backend-Engineer_R-12345",
        "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Backend-Engineer_R-12345",
    ),
    (
        "https://www.google.com/search?q=test&start=10",
        "https://www.google.com/search?q=test&start=10",
    ),
    (
        "https://www.google.com/url?q=https%3A%2F%2Fjobs.ashbyhq.com%2Facme%2Fabcdefgh&sa=U",
        "https://www.google.com/url?q=https%3A%2F%2Fjobs.ashbyhq.com%2Facme%2Fabcdefgh&sa=U",
    ),
    # IDN hosts.
    ("https://B\u00dcCHER.example/x", "https://xn--bcher-kva.example/x"),
    ("https://xn--bcher-kva.example/x", "https://xn--bcher-kva.example/x"),
]


@pytest.mark.parametrize(("raw", "expected"), CANONICALIZE_CASES)
def test_canonicalize_url_matches_node_reference(raw: str, expected: str) -> None:
    assert urls.canonicalize_url(raw) == expected


def test_canonicalize_url_rejects_unparsable_input() -> None:
    with pytest.raises(ValueError):
        urls.canonicalize_url("example.com")
    with pytest.raises(ValueError):
        urls.canonicalize_url("http://exa mple.com/")


def test_stable_hash_is_sha256_prefix_24() -> None:
    assert urls.stable_hash("abc") == "ba7816bf8f01cfea414140de"


def test_normalized_text_collapses_non_alphanumerics() -> None:
    assert urls.normalized_text("Junior  Python-Developer!") == "junior python developer"
    assert urls.normalized_text("caf\u00e9") == "caf"
    assert urls.normalized_text() == ""


def test_find_ats_job_id_prefers_explicit_params() -> None:
    assert (
        urls.find_ats_job_id("https://jobs.ashbyhq.com/tyba/6bf9a202-0b83-44c8-b06f-8f258fa8057e")
        == "jobs.ashbyhq.com:6bf9a202-0b83-44c8-b06f-8f258fa8057e"
    )
    assert (
        urls.find_ats_job_id("https://job-boards.greenhouse.io/acme/jobs/9?gh_jid=4567890")
        == "4567890"
    )
    assert urls.find_ats_job_id("https://jobs.lever.co/x?a=1&jobId=abc123") == "abc123"
    assert urls.find_ats_job_id("https://x.com/a/b/12345") == "x.com:12345"
    assert urls.find_ats_job_id("https://x.com/a/1234") is None
    assert urls.find_ats_job_id("https://x.com/?jobId=&job_id=zvb1") == "zvb1"


def test_is_career_landing_page_url_distinguishes_ashby_boards() -> None:
    assert urls.is_career_landing_page_url("https://jobs.ashbyhq.com/cradlebio", "Ashby") is True
    assert (
        urls.is_career_landing_page_url(
            "https://jobs.ashbyhq.com/tyba/6bf9a202-0b83-44c8-b06f-8f258fa8057e", "Ashby"
        )
        is False
    )
    assert urls.is_career_landing_page_url("https://jobs.ashbyhq.com/cradlebio", "") is True
    assert urls.is_career_landing_page_url("https://jobs.ashbyhq.com/cradlebio", "Lever") is False
    assert urls.is_career_landing_page_url("https://x.com/whatever", "Ashby") is False


def test_is_job_posting_candidate_wraps_career_landing_check() -> None:
    assert (
        urls.is_job_posting_candidate(
            {"canonicalUrl": "https://jobs.ashbyhq.com/acme", "platform": "Ashby"}
        )
        is False
    )
    assert (
        urls.is_job_posting_candidate(
            {"canonicalUrl": "https://jobs.ashbyhq.com/acme/abcdefgh", "platform": "Ashby"}
        )
        is True
    )


def test_extract_passthrough_token_modern_goto() -> None:
    assert (
        urls.extract_passthrough_token("https://www.google.com/goto?url=CAESfwHrOzAVomMWRE")
        == "CAESfwHrOzAVomMWRE"
    )


def test_extract_passthrough_token_legacy_url_form() -> None:
    assert (
        urls.extract_passthrough_token(
            "https://www.google.com/url?q=https%3A%2F%2Fjobs.ashbyhq.com%2Facme"
        )
        == "https://jobs.ashbyhq.com/acme"
    )


def test_extract_passthrough_token_ignores_normal_links() -> None:
    assert urls.extract_passthrough_token("https://jobs.ashbyhq.com/acme/job-1") is None
    assert urls.extract_passthrough_token("https://www.google.com/search?q=anything") is None


def test_resolve_passthrough_url_resolves_and_caches() -> None:
    calls: list[str] = []

    def resolver(value: str) -> str:
        calls.append(value)
        return "https://jobs.ashbyhq.com/crusoe/9a5223c4-9eb7-4fdb-b97c-f43525df35ed"

    cache: dict[str, str] = {}
    passthrough = "https://www.google.com/goto?url=TOKEN123"
    first = urls.resolve_passthrough_url(passthrough, cache=cache, resolver=resolver)
    second = urls.resolve_passthrough_url(passthrough, cache=cache, resolver=resolver)
    assert first == second == "https://jobs.ashbyhq.com/crusoe/9a5223c4-9eb7-4fdb-b97c-f43525df35ed"
    assert calls == [passthrough]


def test_resolve_passthrough_url_falls_back_to_original_on_failure() -> None:
    def broken_resolver(value: str) -> str:
        raise RuntimeError("network down")

    passthrough = "https://www.google.com/goto?url=BROKEN"
    assert urls.resolve_passthrough_url(passthrough, resolver=broken_resolver) == passthrough


def test_resolve_passthrough_url_passes_through_normal_links_untouched() -> None:
    def failing_resolver(value: str) -> str:
        raise AssertionError("resolver must not be called for normal links")

    assert (
        urls.resolve_passthrough_url(
            "https://jobs.ashbyhq.com/acme/job-1", resolver=failing_resolver
        )
        == "https://jobs.ashbyhq.com/acme/job-1"
    )
