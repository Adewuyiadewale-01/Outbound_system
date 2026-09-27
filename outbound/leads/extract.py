"""Executive candidate extraction, scoring, and reconciliation.

Extracted from scripts/lead_exec_research.py during the leads carve
(docs/CARVE-LEADS.md, Stage 1, slice S6). Pure move.
"""

import re
from typing import Any

from outbound.leads.config import ROLE_KEYWORDS
from outbound.leads.search import SearchClient, search_query_variants
from outbound.leads.text import (
    clean_person_name,
    clean_text,
    is_linkedin_profile_url,
    is_plausible_person_name,
    linkedin_url_name_score,
    normalize_key,
    normalize_url,
    title_weight,
    token_overlap_score,
)


def seniority_score(person: dict[str, Any]) -> int:
    return title_weight(clean_text(person.get("title") or person.get("role") or ""))


def extract_exec_candidates(
    company_name: str, results: list[dict[str, str]]
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for result in results:
        haystack = f"{result.get('title', '')} {result.get('snippet', '')}"
        for name, role in extract_name_role_pairs(haystack):
            key = normalize_key(name)
            if not key:
                continue
            score = title_weight(role or haystack)
            if normalize_key(company_name) and normalize_key(company_name) in normalize_key(
                haystack
            ):
                score += 8
            if is_linkedin_profile_url(result.get("url", "")):
                score += 6
            existing = candidates.get(key)
            item = {
                "name": name,
                "role": role,
                "source": "exec_search",
                "source_url": result.get("url", ""),
                "source_title": result.get("title", ""),
                "source_snippet": result.get("snippet", ""),
                "score": score,
            }
            if existing is None or item["score"] > existing["score"]:
                candidates[key] = item
    return sorted(candidates.values(), key=lambda item: item["score"], reverse=True)


def extract_name_role_pairs(text: str) -> list[tuple[str, str]]:
    cleaned = clean_text(text)
    pairs: list[tuple[str, str]] = []
    name_pattern = r"([A-Z][a-zA-Z'`.-]+(?:\s+(?:van|von|de|den|der|[A-Z][a-zA-Z'`.-]+)){1,4})"
    role_pattern = r"\b(CEO|Chief Executive Officer|Founder|Co-Founder|Managing Director|President|Owner|Partner|CTO|COO|CFO|Head of [A-Za-z &]+|Director\b[^,;|.-]*)"

    for match in re.finditer(name_pattern + r".{0,80}?" + role_pattern, cleaned):
        pairs.append((clean_person_name(match.group(1)), clean_text(match.group(2))))
    for match in re.finditer(role_pattern + r".{0,80}?" + name_pattern, cleaned):
        pairs.append((clean_person_name(match.group(2)), clean_text(match.group(1))))
    return [(name, role) for name, role in pairs if is_plausible_person_name(name)]


def search_execs_for_company(
    search_client: SearchClient, company: dict[str, str], max_execs: int = 3
) -> list[dict[str, Any]]:
    company_name = company.get("name", "")
    queries = [
        f'"{company_name}" CEO founder managing director',
        f'"{company_name}" leadership executive team',
        f'site:linkedin.com/in "{company_name}" CEO founder',
    ]
    all_results: list[dict[str, str]] = []
    for query in queries:
        try:
            all_results.extend(search_client.search(query, limit=5))
        except Exception as exc:
            all_results.append({"title": "", "url": "", "snippet": f"SEARCH_ERROR: {exc}"})
    candidates = extract_exec_candidates(company_name, all_results)
    return candidates[:max_execs]


def employee_role_score(role: str) -> int:
    return title_weight(role)


def fallback_execs_from_employees(
    employees: list[dict[str, Any]], max_execs: int = 3
) -> list[dict[str, Any]]:
    ranked = []
    for employee in employees:
        if not employee.get("name"):
            continue
        ranked.append(
            {
                "name": employee.get("name", ""),
                "role": employee.get("role", ""),
                "source": "employee_rows",
                "score": employee_role_score(employee.get("role", "")),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:max_execs]


def match_employee(
    exec_item: dict[str, Any], employees: list[dict[str, Any]]
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for employee in employees:
        score = token_overlap_score(exec_item.get("name", ""), employee.get("name", ""))
        if score > best_score:
            best = employee
            best_score = score
    if best and best_score >= 0.45:
        matched = dict(best)
        matched["match_score"] = round(best_score, 3)
        return matched
    return None


def score_search_candidate(
    exec_item: dict[str, Any], company: dict[str, str], result: dict[str, str]
) -> float:
    name = exec_item.get("name", "")
    title = result.get("title", "")
    snippet = result.get("snippet", "")
    url = result.get("url", "")
    haystack = normalize_key(f"{title} {snippet}")
    score = 0.0
    if is_linkedin_profile_url(url):
        score += 0.35
    score += linkedin_url_name_score(name, url) * 0.30
    if token_overlap_score(name, f"{title} {snippet}") >= 0.45:
        score += 0.20
    company_name = normalize_key(company.get("name", ""))
    if company_name and company_name in haystack:
        score += 0.10
    if any(keyword in haystack for keyword in ROLE_KEYWORDS):
        score += 0.05
    return round(min(score, 1.0), 3)


def search_linkedin_for_exec(
    search_client: SearchClient,
    exec_item: dict[str, Any],
    company: dict[str, str],
) -> dict[str, Any]:
    queries = search_query_variants(exec_item.get("name", ""), company.get("name", ""))
    query = queries[0] if queries else ""
    all_results: list[dict[str, str]] = []
    errors = []
    try:
        for current_query in queries:
            try:
                results = search_client.search(current_query, limit=5)
            except Exception as exc:
                errors.append(f"{current_query}: {exc}")
                continue
            all_results.extend({**result, "query": current_query} for result in results)
            if results:
                break
    except Exception as exc:  # defensive only; per-query errors are handled above
        errors.append(str(exc))
    if not all_results and errors:
        return {
            "query": query,
            "queries": queries,
            "results": [],
            "selected": None,
            "error": "; ".join(errors),
        }
    scored = []
    for result in all_results:
        item = dict(result)
        item["score"] = score_search_candidate(exec_item, company, result)
        scored.append(item)
    scored.sort(key=lambda item: item["score"], reverse=True)
    selected = scored[0] if scored and scored[0]["score"] >= 0.45 else None
    return {
        "query": query,
        "queries": queries,
        "results": scored,
        "selected": selected,
        "error": None,
    }


def reconcile_exec(
    search_client: SearchClient,
    group: dict[str, Any],
    exec_item: dict[str, Any],
) -> dict[str, Any]:
    company = group["company"]
    employees = group.get("employees_from_sheet", [])
    matched = match_employee(exec_item, employees)
    finalized = {
        "name": exec_item.get("name", ""),
        "role": exec_item.get("role", ""),
        "email": "",
        "linkedin": "",
        "source": exec_item.get("source", "web_search"),
        "matched_sheet_employee": False,
        "match_notes": [],
        "confidence": 0.45,
        "search": None,
    }

    if matched:
        finalized["matched_sheet_employee"] = True
        finalized["email"] = matched.get("email", "")
        if matched.get("role") and not finalized["role"]:
            finalized["role"] = matched.get("role", "")
        url_score = linkedin_url_name_score(finalized["name"], matched.get("linkedin", ""))
        if matched.get("linkedin") and url_score >= 0.34:
            finalized["linkedin"] = matched.get("linkedin", "")
            finalized["confidence"] = max(finalized["confidence"], 0.82 + min(url_score, 1.0) * 0.1)
            finalized["match_notes"].append("Used reconciled LinkedIn from source employee row.")
        elif matched.get("linkedin"):
            finalized["match_notes"].append(
                "Source employee LinkedIn existed but did not match enough name parts."
            )
        else:
            finalized["match_notes"].append("Matched source employee row but LinkedIn was blank.")

    if not finalized["linkedin"]:
        search = search_linkedin_for_exec(search_client, exec_item, company)
        finalized["search"] = search
        selected = search.get("selected")
        if selected:
            finalized["linkedin"] = normalize_url(selected.get("url", ""))
            finalized["confidence"] = max(finalized["confidence"], selected.get("score", 0.0))
            finalized["match_notes"].append("Selected LinkedIn from top search results.")
        elif search.get("error"):
            finalized["match_notes"].append(f"LinkedIn search failed: {search['error']}")
        else:
            finalized["match_notes"].append("No high-confidence LinkedIn result found.")

    finalized["confidence"] = round(float(finalized["confidence"]), 3)
    return finalized
