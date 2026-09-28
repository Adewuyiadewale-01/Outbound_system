"""Search-query builder ported from ``job_discovery/src/query-builder.mjs``
plus the runtime configuration reader from ``runtime-configuration.mjs``.

Builds the 12-platform x 8-role x 3-strategy query inventory (288 queries),
alternating engineering and design fields; optionally derives the runtime
inventory from Sheet rows instead.
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


_DEFAULT_ROLE_FIELDS = {role["name"]: role.get("field") for role in ROLES}


def hosts_from_site_target(target: str = "") -> list[str]:
    """Reference ``hostsFromSiteTarget`` (same regex as ``hosts_from_target``)."""
    return hosts_from_target(target)


def _field_for_role(name: str) -> str:
    return "design" if _DEFAULT_ROLE_FIELDS.get(name) == "design" else "engineering"


def _enabled(value: object, fallback: bool = True) -> bool:
    if value is None or value == "":
        return fallback
    return not re.fullmatch(r"(false|no|0|disabled)", str(value).strip(), re.IGNORECASE)


def _vocabulary(value: object = "") -> list[str]:
    return [item.strip() for item in str(value).split("|") if item.strip()]


def _cell(row: list, index: int, default: str = "") -> object:
    return row[index] if index < len(row) else default


def configuration_from_rows(configuration: dict | None = None) -> dict:
    """Port of ``configurationFromRows``: Sheet rows -> runtime inventory.

    Falls back to the compiled defaults whenever the corresponding rows are
    empty (local-first mode).
    """
    configuration = configuration or {}
    platform_rows = configuration.get("platformRows") or []
    role_rows = configuration.get("roleRows") or []
    query_rows = configuration.get("queryRows") or []
    rule_rows = configuration.get("ruleRows") or []
    if platform_rows:
        platforms = [
            {
                "name": row[0],
                "siteTarget": _cell(row, 1) or "",
                "enabled": _enabled(_cell(row, 2)),
                "allowedHosts": hosts_from_site_target(str(_cell(row, 1) or "")),
            }
            for row in platform_rows
            if row and row[0]
        ]
    else:
        platforms = [
            {**platform, "allowedHosts": hosts_from_site_target(platform["siteTarget"])}
            for platform in PLATFORMS
        ]
    role_map: dict[str, dict] = {}
    for row in role_rows:
        if not row or not row[0]:
            continue
        role = role_map.get(row[0]) or {
            "name": row[0],
            "field": _field_for_role(row[0]),
            "junior": [],
            "unfiltered": [],
            "strongSignals": [],
        }
        if _cell(row, 1) == "junior":
            role["junior"].extend(_vocabulary(_cell(row, 2)))
        if _cell(row, 1) == "unfiltered":
            role["unfiltered"].extend(_vocabulary(_cell(row, 2)))
        role_map[role["name"]] = role
    roles = list(role_map.values()) if role_map else ROLES
    platform_map = {platform["name"]: platform for platform in platforms}
    if query_rows:
        queries = [
            {
                "id": row[0],
                "platform": _cell(row, 1),
                "role": _cell(row, 2),
                "field": _field_for_role(str(_cell(row, 2))),
                "type": _cell(row, 3),
                "query": _cell(row, 4),
                "allowedHosts": (platform_map.get(_cell(row, 1)) or {}).get("allowedHosts") or [],
            }
            for row in query_rows
            if row
            and row[0]
            and _enabled(_cell(row, 5))
            and platform_map.get(_cell(row, 1), {}).get("enabled") is not False
        ]
    else:
        queries = build_queries(platforms, roles)
    rules = {str(row[0]).lower(): _vocabulary(_cell(row, 1)) for row in rule_rows if row and row[0]}
    return {
        "platforms": platforms,
        "roles": roles,
        "queries": queries,
        "signalRules": {
            "junior": rules.get("junior signals"),
            "senior": rules.get("senior signals"),
            "remote": rules.get("remote signals"),
            "hybrid": rules.get("hybrid signals"),
            "onsite": rules.get("onsite signals"),
            "nonRemote": rules.get("non-remote signals"),
            "python": rules.get("python signals"),
        },
    }
