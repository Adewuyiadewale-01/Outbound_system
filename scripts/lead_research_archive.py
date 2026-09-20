#!/usr/bin/env python3
"""Durable, model-free reuse archive for lead executive research."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

ARCHIVE_SCHEMA_VERSION = 1
MATCH_AVAILABLE = "archive_match"
MATCH_CONSUMED = "archive_consumed"
MATCH_CONFLICT = "conflict"
MATCH_FRESH = "fresh"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def canonical_domain(value: Any) -> str:
    raw = clean_text(value).lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        host = urllib.parse.urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def canonical_linkedin_company(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if "linkedin." not in host:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in {"company", "school"}:
        return ""
    return f"{parts[0].lower()}/{urllib.parse.unquote(parts[1]).lower()}"


def normalized_lead(lead: dict[str, Any]) -> dict[str, str]:
    company_value = lead.get("company", "")
    company = company_value if isinstance(company_value, dict) else {}
    company_name = company.get("name", "") if company else company_value
    website = company.get("website", "") if company else lead.get("website", "")
    linkedin = company.get("linkedin", "") if company else lead.get("company_linkedin", "")
    lead_id = lead.get("id") or lead.get("lead_id") or lead.get("ID") or ""
    return {
        "lead_id": clean_text(lead_id),
        "company_name": clean_text(company_name),
        "company_name_key": normalize_name(company_name),
        "website": clean_text(website),
        "website_domain": canonical_domain(website),
        "company_linkedin": clean_text(linkedin),
        "company_linkedin_key": canonical_linkedin_company(linkedin),
    }


def archive_entry_id_for(lead: dict[str, Any]) -> str:
    identity = normalized_lead(lead)
    strongest = (
        identity["company_linkedin_key"]
        or identity["website_domain"]
        or identity["lead_id"]
        or identity["company_name_key"]
    )
    digest = hashlib.sha256(strongest.encode("utf-8")).hexdigest()[:16]
    return f"research-{digest}"


def has_reusable_research(lead: dict[str, Any]) -> bool:
    executives = lead.get("executives") or []
    if any(
        clean_text(executive.get("name"))
        and clean_text(executive.get("linkedin_url") or executive.get("linkedin"))
        for executive in executives
        if isinstance(executive, dict)
    ):
        return True
    destination = lead.get("destination_row") or {}
    return bool(
        clean_text(destination.get("P1 Name")) and clean_text(destination.get("P1 LinkedIn"))
    )


def empty_archive() -> dict[str, Any]:
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "updated_at": "",
        "entries": {},
    }


class ResearchArchive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_archive()
        payload = json.loads(self.path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f"Research archive must be a JSON object: {self.path}")
        payload.setdefault("schema_version", ARCHIVE_SCHEMA_VERSION)
        payload.setdefault("updated_at", "")
        payload.setdefault("entries", {})
        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["schema_version"] = ARCHIVE_SCHEMA_VERSION
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")

    def entries(self) -> Iterable[dict[str, Any]]:
        return self.data.get("entries", {}).values()

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return self.data.get("entries", {}).get(clean_text(entry_id))

    def stats(self) -> dict[str, Any]:
        entries = list(self.entries())
        available = [entry for entry in entries if entry.get("status", "available") == "available"]
        consumed = [entry for entry in entries if entry.get("status") == "consumed"]
        return {
            "enabled": bool(available),
            "total": len(entries),
            "available": len(available),
            "consumed": len(consumed),
            "conflicts": len([entry for entry in entries if entry.get("status") == "conflict"]),
            "updated_at": self.data.get("updated_at", ""),
            "path": str(self.path),
        }

    def match(self, lead: dict[str, Any]) -> dict[str, Any]:
        identity = normalized_lead(lead)
        matches: dict[str, list[str]] = {}
        name_matches: list[str] = []

        for entry in self.entries():
            entry_id = clean_text(entry.get("archive_entry_id"))
            entry_identity = entry.get("identity", {})
            reasons = []
            if identity["lead_id"] and identity["lead_id"] in entry_identity.get("lead_ids", []):
                reasons.append("lead_id")
            if identity["company_linkedin_key"] and identity[
                "company_linkedin_key"
            ] == entry_identity.get("company_linkedin_key"):
                reasons.append("company_linkedin")
            if identity["website_domain"] and identity["website_domain"] == entry_identity.get(
                "website_domain"
            ):
                reasons.append("website_domain")
            if reasons:
                matches[entry_id] = reasons
            elif identity["company_name_key"] and identity[
                "company_name_key"
            ] == entry_identity.get("company_name_key"):
                name_matches.append(entry_id)

        if len(matches) == 1:
            entry_id, matched_on = next(iter(matches.items()))
            entry = self.get(entry_id) or {}
            status = MATCH_CONSUMED if entry.get("status") == "consumed" else MATCH_AVAILABLE
            return {
                "status": status,
                "archive_entry_id": entry_id,
                "matched_on": matched_on,
                "confidence": "exact",
            }
        if len(matches) > 1:
            return {
                "status": MATCH_CONFLICT,
                "archive_entry_id": "",
                "candidate_entry_ids": sorted(matches),
                "matched_on": sorted(
                    {reason for reasons in matches.values() for reason in reasons}
                ),
                "confidence": "conflict",
            }
        if name_matches:
            return {
                "status": MATCH_CONFLICT,
                "archive_entry_id": "",
                "candidate_entry_ids": sorted(name_matches),
                "matched_on": ["company_name"],
                "confidence": "name_only",
            }
        return {
            "status": MATCH_FRESH,
            "archive_entry_id": "",
            "matched_on": [],
            "confidence": "none",
        }

    def upsert_computation_lead(
        self,
        lead: dict[str, Any],
        *,
        source_computation_file: str = "",
        source_run_file: str = "",
        reason: str = "unreviewed_processing",
    ) -> dict[str, Any]:
        identity = normalized_lead(lead)
        match = self.match(lead)
        if match["status"] in {MATCH_AVAILABLE, MATCH_CONSUMED}:
            entry_id = match["archive_entry_id"]
            entry = self.get(entry_id) or {}
        else:
            entry_id = archive_entry_id_for(lead)
            entry = self.get(entry_id) or {}

        now = datetime.now().isoformat(timespec="seconds")
        lead_ids = set(entry.get("identity", {}).get("lead_ids", []))
        if identity["lead_id"]:
            lead_ids.add(identity["lead_id"])
        status = entry.get("status", "available")
        if status not in {"available", "consumed"}:
            status = "available"
        entry.update(
            {
                "archive_entry_id": entry_id,
                "status": status,
                "created_at": entry.get("created_at", now),
                "updated_at": now,
                "reason": reason,
                "identity": {
                    "lead_ids": sorted(lead_ids),
                    "company_name": identity["company_name"],
                    "company_name_key": identity["company_name_key"],
                    "website": identity["website"],
                    "website_domain": identity["website_domain"],
                    "company_linkedin": identity["company_linkedin"],
                    "company_linkedin_key": identity["company_linkedin_key"],
                },
                "research": deepcopy(lead),
                "source": {
                    "computation_file": source_computation_file,
                    "run_file": source_run_file,
                },
            }
        )
        self.data.setdefault("entries", {})[entry_id] = entry
        return entry

    def archive_computation(
        self,
        computation: dict[str, Any],
        *,
        computation_file: str = "",
        reason: str = "unreviewed_processing",
    ) -> dict[str, Any]:
        archived = []
        skipped = []
        for lead in computation.get("leads", []):
            if not has_reusable_research(lead):
                skipped.append(
                    {
                        "lead_id": clean_text(lead.get("lead_id")),
                        "reason": "research_not_available",
                    }
                )
                continue
            entry = self.upsert_computation_lead(
                lead,
                source_computation_file=computation_file,
                source_run_file=clean_text(computation.get("source_run_file")),
                reason=reason,
            )
            archived.append(entry["archive_entry_id"])
        self.save()
        return {
            "archived_count": len(archived),
            "archive_entry_ids": archived,
            "skipped_count": len(skipped),
            "skipped": skipped,
            "stats": self.stats(),
        }

    def mark_consumed(
        self,
        entry_ids: Iterable[str],
        *,
        computation_file: str = "",
        destination: str = "",
    ) -> int:
        changed = 0
        now = datetime.now().isoformat(timespec="seconds")
        for entry_id in set(clean_text(value) for value in entry_ids if clean_text(value)):
            entry = self.get(entry_id)
            if not entry or entry.get("status") == "consumed":
                continue
            entry["status"] = "consumed"
            entry["consumed_at"] = now
            entry["consumed_by"] = {
                "computation_file": computation_file,
                "destination": destination,
            }
            entry["updated_at"] = now
            changed += 1
        if changed:
            self.save()
        return changed


def hydrate_computation_lead(
    current: dict[str, Any],
    archive_entry: dict[str, Any],
) -> dict[str, Any]:
    archived = deepcopy(archive_entry.get("research", {}))
    hydrated = deepcopy(current)
    for field in ("executives", "search_tasks", "search_results", "destination_row", "notes"):
        if field in archived:
            hydrated[field] = deepcopy(archived.get(field))
    hydrated["archive_entry_id"] = archive_entry.get("archive_entry_id", "")
    hydrated["research_source"] = "archive"
    hydrated["archive_reused_at"] = datetime.now().isoformat(timespec="seconds")
    if hydrated.get("executives") or hydrated.get("destination_row"):
        hydrated["status"] = "archive_reused"
    return hydrated
