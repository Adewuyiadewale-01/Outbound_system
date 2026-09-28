"""Tests for the ported query builder (``job_discovery/src/query-builder.mjs``)."""

from __future__ import annotations

from outbound.job_discovery import queries as query_builder
from outbound.job_discovery.settings import PLATFORMS, ROLES


def test_builds_one_junior_unfiltered_and_junior_signal_query_for_every_platform_and_role() -> None:
    built = query_builder.build_queries()
    assert len(built) == 288
    assert sum(query["type"] == "junior" for query in built) == 96
    assert sum(query["field"] == "engineering" for query in built) == 180
    assert sum(query["field"] == "design" for query in built) == 108
    assert [query["field"] for query in built[:2]] == ["engineering", "design"]
    lever = next(query for query in built if query["id"] == "Lever:Python Developer:junior")
    assert "site:jobs.lever.co" in lever["query"]
    assert lever["allowedHosts"] == ["jobs.lever.co", "jobs.eu.lever.co"]
    designer = next(query for query in built if query["id"] == "Ashby:Product Designer:junior")
    assert "product designer" in designer["query"]
    signal = next(
        query for query in built if query["id"] == "Ashby:Backend Developer:junior-signal"
    )
    assert '"early career"' in signal["query"]


def test_query_ids_are_unique_and_cover_the_full_matrix() -> None:
    built = query_builder.build_queries()
    assert len({query["id"] for query in built}) == 288
    assert {query["type"] for query in built} == {"junior", "unfiltered", "junior-signal"}
    assert all(query["allowedHosts"] for query in built)


def test_exact_query_string_for_ashby_python_developer_junior() -> None:
    built = query_builder.build_queries()
    junior = next(query for query in built if query["id"] == "Ashby:Python Developer:junior")
    assert junior["query"] == (
        'site:jobs.ashbyhq.com (intitle:"junior python developer" OR '
        'intitle:"junior python engineer" OR intitle:"jr python developer" OR '
        'intitle:"associate python developer" OR intitle:"associate python engineer" OR '
        'intitle:"entry level python developer" OR intitle:"python engineer I" OR '
        'intitle:"python engineer 1") remote -"no remote" -intitle:senior '
        "-intitle:staff -intitle:principal"
    )


def test_disabled_platforms_and_custom_roles_are_respected() -> None:
    active_platforms = [
        {"name": "Enabled", "siteTarget": "site:foo.com", "enabled": True},
        {"name": "Skipped", "siteTarget": "site:bar.com", "enabled": False},
    ]
    active_roles = [
        {"name": "Only Role", "field": "engineering", "junior": ["jr x"], "unfiltered": ["x"]}
    ]
    built = query_builder.build_queries(active_platforms, active_roles)
    assert [query["id"] for query in built] == [
        "Enabled:Only Role:junior",
        "Enabled:Only Role:unfiltered",
        "Enabled:Only Role:junior-signal",
    ]
    assert built[0]["allowedHosts"] == ["foo.com"]


def test_hosts_from_target_parses_site_expressions() -> None:
    assert query_builder.hosts_from_target("(site:jobs.lever.co OR site:jobs.eu.lever.co)") == [
        "jobs.lever.co",
        "jobs.eu.lever.co",
    ]
    assert query_builder.hosts_from_target("SITE:Foo.COM") == ["foo.com"]
    assert query_builder.hosts_from_target("") == []


def test_defaults_compile_from_settings_platforms_and_roles() -> None:
    built = query_builder.build_queries()
    assert len(PLATFORMS) == 12
    assert len(ROLES) == 8
    assert built[0]["platform"] == PLATFORMS[0]["name"]
