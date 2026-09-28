"""Search-query builder ported from ``job_discovery/src/query-builder.mjs``.

Builds the 12-platform x 8-role x 3-strategy query inventory (288 queries),
alternating engineering and design fields.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, TypedDict

from outbound.job_discovery.settings import PLATFORMS, ROLES


class Query(TypedDict):
    id: str
    platform: str
    role: str
    field: str
    type: str
    query: str
    allowedHosts: list[str]


_QUERY_SIGNALS = ["junior", '"entry level"', '"new grad"', "associate", '"early career"']


def hosts_from_target(target: str = "") -> list[str]:
    """Extract allowed hosts from a ``site:`` target expression."""
    return [match.lower() for match in re.findall(r"site:([a-z0-9.-]+)", target, re.IGNORECASE)]


def _quoted(values: list[str], *, title_only: bool = True) -> str:
    prefix = "intitle:" if title_only else ""
    return " OR ".join(f'{prefix}"{value}"' for value in values)


def _role_field(role: dict[str, Any]) -> str:
    return "design" if role.get("field") == "design" else "engineering"


def _alternate_fields(queries: list[Query]) -> list[Query]:
    buckets: dict[str, deque[Query]] = {"engineering": deque(), "design": deque()}
    for query in queries:
        buckets[_role_field(query)].append(query)
    ordered: list[Query] = []
    while buckets["engineering"] or buckets["design"]:
        for field in ("engineering", "design"):
            if buckets[field]:
                ordered.append(buckets[field].popleft())
    return ordered


def build_queries(
    active_platforms: list[dict[str, Any]] | None = None,
    active_roles: list[dict[str, Any]] | None = None,
) -> list[Query]:
    """Port of ``buildQueries``: one junior, unfiltered, and junior-signal query
    for every enabled platform x role, alternated by field."""
    platforms_in = PLATFORMS if active_platforms is None else active_platforms
    roles_in = ROLES if active_roles is None else active_roles
    queries: list[Query] = []
    for platform in platforms_in:
        if not platform.get("enabled"):
            continue
        allowed_hosts = platform.get("allowedHosts") or hosts_from_target(
            platform.get("siteTarget", "")
        )
        for role in roles_in:
            role_name = role["name"]
            field = _role_field(role)
            queries.append(
                {
                    "id": f"{platform['name']}:{role_name}:junior",
                    "platform": platform["name"],
                    "role": role_name,
                    "field": field,
                    "type": "junior",
                    "query": (
                        f"{platform.get('siteTarget', '')} ({_quoted(role['junior'])}) "
                        'remote -"no remote" -intitle:senior -intitle:staff -intitle:principal'
                    ),
                    "allowedHosts": list(allowed_hosts),
                }
            )
            queries.append(
                {
                    "id": f"{platform['name']}:{role_name}:unfiltered",
                    "platform": platform["name"],
                    "role": role_name,
                    "field": field,
                    "type": "unfiltered",
                    "query": (
                        f"{platform.get('siteTarget', '')} ({_quoted(role['unfiltered'])}) "
                        'remote -"no remote"'
                    ),
                    "allowedHosts": list(allowed_hosts),
                }
            )
            queries.append(
                {
                    "id": f"{platform['name']}:{role_name}:junior-signal",
                    "platform": platform["name"],
                    "role": role_name,
                    "field": field,
                    "type": "junior-signal",
                    "query": (
                        f"{platform.get('siteTarget', '')} "
                        f"({_quoted(role['unfiltered'], title_only=False)}) "
                        f'({" OR ".join(_QUERY_SIGNALS)}) remote -"no remote"'
                    ),
                    "allowedHosts": list(allowed_hosts),
                }
            )
    return _alternate_fields(queries)
