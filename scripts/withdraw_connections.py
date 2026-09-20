#!/usr/bin/env python3
"""Run prepared LinkedIn connection-request withdrawals.

Requires a prepared session from prepare_connection_withdrawals.py.
Writes sheet updates only after confirmed withdrawal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from linkedin_helper import (  # noqa: E402
    HumanSimulator,
    LinkedInSession,
    _navigate_with_readiness,
    check_circuit_breakers,
    inspect_profile_action_state,
)
from outreach_helper import CREDS_PATH, OBF_SHEET_URL, OUTREACH_LOG_TAB  # noqa: E402
from sheets_helper import get_client, get_worksheet, open_sheet  # noqa: E402

SENT_INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
WITHDRAWN_LEADS_TAB = "Withdrawn Leads"
DEFAULT_TIMEZONE = "Africa/Lagos"
RUN_GUARD_PATH = ROOT / "state" / "withdrawal_run_guard.json"
LIVE_RUN_COOLDOWN_SECONDS = 2 * 60 * 60


def parse_run_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_guard() -> dict[str, Any]:
    if not RUN_GUARD_PATH.exists():
        return {}
    try:
        return json.loads(RUN_GUARD_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_guard(payload: dict[str, Any]) -> None:
    write_json(RUN_GUARD_PATH, payload)


def check_live_run_cooldown(now: datetime) -> dict[str, Any] | None:
    guard = read_guard()
    raw_started = guard.get("last_live_run_started_at") or ""
    if not raw_started:
        return None
    try:
        started = datetime.fromisoformat(raw_started)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    elapsed = (now - started).total_seconds()
    remaining = LIVE_RUN_COOLDOWN_SECONDS - elapsed
    if remaining <= 0:
        return None
    return {
        "last_live_run_started_at": raw_started,
        "cooldown_seconds": LIVE_RUN_COOLDOWN_SECONDS,
        "remaining_seconds": int(remaining),
        "remaining_minutes": round(remaining / 60, 1),
    }


def append_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            # ensure_ascii=True safely escapes surrogates to \uXXXX to prevent UnicodeEncodeError
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def completed_prospect_ids(journal_path: Path, session_path: Path) -> set:
    try:
        session_texts = {str(session_path), str(session_path.resolve())}
    except Exception:
        session_texts = {str(session_path)}
    completed = set()
    for event in read_jsonl(journal_path):
        if event.get("event") != "withdrawal_target_result":
            continue
        if not event.get("confirmed"):
            continue
        event_session = str(event.get("session") or event.get("run_session") or "")
        if event_session not in session_texts:
            continue
        prospect_id = str(event.get("prospect_id") or "").strip()
        if prospect_id:
            completed.add(prospect_id)
    return completed


def header_index(headers: list[str]) -> dict[str, int]:
    return {str(header or "").strip(): i + 1 for i, header in enumerate(headers)}


def update_row_fields(worksheet: Any, row_number: int, fields: dict[str, Any]) -> None:
    headers = [str(header or "").strip() for header in worksheet.row_values(1)]
    index = header_index(headers)
    updates = []
    for field, value in fields.items():
        col = index.get(field)
        if not col:
            raise ValueError(f"{worksheet.title} is missing column: {field}")
        updates.append({"range": f"{_a1(row_number, col)}", "values": [[value]]})
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")


def _a1(row_number: int, col_number: int) -> str:
    letters = ""
    n = col_number
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_number}"


def ensure_withdrawn_tab(spreadsheet: Any) -> Any:
    headers = [
        "Prospect ID",
        "Company",
        "Person Engaged",
        "Contact Name",
        "Contact Linkedin",
        "Activity Since Sent",
        "Sent At",
        "Withdrawn At",
    ]
    try:
        worksheet = get_worksheet(spreadsheet, WITHDRAWN_LEADS_TAB)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=WITHDRAWN_LEADS_TAB, rows=1000, cols=len(headers)
        )
    current = [str(header or "").strip() for header in worksheet.row_values(1)]
    if current[: len(headers)] != headers:
        worksheet.update(range_name="A1:H1", values=[headers], value_input_option="USER_ENTERED")
    return worksheet


def upsert_withdrawn_lead(
    worksheet: Any, target: dict[str, Any], activity_value: str, withdrawn_at: str
) -> None:
    headers = [str(header or "").strip() for header in worksheet.row_values(1)]
    index = header_index(headers)
    prospect_id = str(target.get("prospect_id", "")).strip()
    existing_row = 0
    if prospect_id and "Prospect ID" in index and worksheet.row_count >= 2:
        values = worksheet.get_values(
            f"{_a1(2, index['Prospect ID'])}:{_a1(worksheet.row_count, index['Prospect ID'])}"
        )
        for offset, row in enumerate(values, start=2):
            if row and str(row[0]).strip() == prospect_id:
                existing_row = offset
                break
    payload = {
        "Prospect ID": prospect_id,
        "Company": target.get("company", ""),
        "Person Engaged": target.get("person_engaged", ""),
        "Contact Name": target.get("contact_name", ""),
        "Contact Linkedin": target.get("contact_linkedin", ""),
        "Activity Since Sent": activity_value,
        "Sent At": target.get("sent_at", ""),
        "Withdrawn At": withdrawn_at,
    }
    if existing_row:
        update_row_fields(worksheet, existing_row, payload)
        return
    row_values = [payload.get(header, "") for header in headers[: len(payload)]]
    worksheet.append_row(row_values, value_input_option="USER_ENTERED")


def classify_activity_since_sent(activity: dict[str, Any]) -> str:
    if not activity or activity.get("error"):
        return "Uncertain"
    if activity.get("early_stop_level"):
        return str(activity["early_stop_level"])

    def count_within(tab_key: str, days_limit: int) -> int:
        count = 0
        for item in ((activity.get("tabs") or {}).get(tab_key) or {}).get("activities", []) or []:
            days = item.get("days")
            if days is None:
                # Existing helper usually stores time_text, not days.
                days = _relative_days(str(item.get("time_text", "")))
            if days is not None and days <= days_limit:
                count += 1
        return count

    if count_within("posts", 7) >= 1:
        return "Very active"
    if count_within("comments", 7) + count_within("reactions", 7) >= 2:
        return "Very active"
    if count_within("posts", 14) >= 1:
        return "Active"
    if count_within("comments", 30) + count_within("reactions", 30) >= 5:
        return "Active"
    return "Not active"


def is_cdp_target_detached_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "cdp error" in text and "inspected target navigated or closed" in text


def _relative_days(text: str) -> int | None:
    value = text.lower().replace("ago", " ").replace("·", " ").replace("•", " ").strip()
    if not value:
        return None
    if "today" in value or "just now" in value:
        return 0
    if "yesterday" in value:
        return 1
    import re

    match = re.search(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs)\b", value)
    if match:
        return 0
    match = re.search(r"(\d+)\s*(d|day|days)\b", value)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*(w|week|weeks)\b", value)
    if match:
        return int(match.group(1)) * 7
    match = re.search(r"(\d+)\s*(mo|month|months)\b", value)
    if match:
        return int(match.group(1)) * 30
    match = re.search(r"(\d+)\s*(y|yr|yrs|year|years)\b", value)
    if match:
        return int(match.group(1)) * 365
    return None


def open_sent_invitations(session: LinkedInSession) -> dict[str, Any]:
    nav_result = _navigate_with_readiness(
        session.cdp,
        SENT_INVITATIONS_URL,
        expected_selector='main#workspace a[aria-label^="Withdraw invitation sent to"]',
        nav_timeout=20,
        ready_timeout=20,
    )
    time.sleep(1)
    danger = check_circuit_breakers(session.cdp)
    if danger:
        return {"ok": False, "danger": danger}
    result = session.cdp.evaluate(
        """
        (() => {
          const scroller = document.querySelector("main#workspace");
          return {
            ok: !!scroller && /\\/mynetwork\\/invitation-manager\\/sent\\/?/i.test(location.pathname),
            navResult: %s,
            url: location.href,
            onSentInvitations: /\\/mynetwork\\/invitation-manager\\/sent\\/?/i.test(location.pathname),
            withdrawCount: document.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]').length,
            scrollTop: scroller ? scroller.scrollTop : null,
            scrollHeight: scroller ? scroller.scrollHeight : null
          };
        })()
    """
        % json.dumps(nav_result)
    )
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def find_and_prepare_row(
    session: LinkedInSession, profile_url: str, max_scrolls: int = 80
) -> dict[str, Any]:
    target_slug = canonical_profile_url(profile_url).rstrip("/").rsplit("/", 1)[-1].lower()
    scan_script = f"""
    (() => {{
      const targetSlug = {json.dumps(target_slug)};
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {{
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      }};
      const profileSlug = href => {{
        try {{
          const u = new URL(href);
          const m = u.pathname.match(/^\\/in\\/([^/?#]+)/i);
          return m ? decodeURIComponent(m[1]).toLowerCase() : "";
        }} catch {{ return ""; }}
      }};
      const area = el => {{
        const r = el.getBoundingClientRect();
        return Math.round(r.width * r.height);
      }};
      const findRow = withdraw => {{
        let node = withdraw.parentElement;
        while (node && node !== document.body) {{
          const text = norm(node.innerText || node.textContent);
          const profileLinks = [...node.querySelectorAll('a[href*="/in/"]')].filter(visible);
          const rowWithdraws = [...node.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]')].filter(visible);
          if (profileLinks.length === 1 && rowWithdraws.length === 1 && /sent/i.test(text) && area(node) > 10000) return node;
          node = node.parentElement;
        }}
        return null;
      }};
      const scroller = document.querySelector("main#workspace");
      if (!scroller) return JSON.stringify({{found: false, reason: "workspace_scroller_missing"}});
      const withdraws = [...document.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]')].filter(visible);
      for (const withdraw of withdraws) {{
        const row = findRow(withdraw);
        if (!row) continue;
        const profile = [...row.querySelectorAll('a[href*="/in/"]')].filter(visible)[0];
        if (!profile || profileSlug(profile.href) !== targetSlug) continue;
        document.querySelectorAll('[data-codex-withdraw-target="true"]').forEach(el => el.removeAttribute('data-codex-withdraw-target'));
        row.setAttribute("data-codex-withdraw-target", "true");
        row.scrollIntoView({{block: "center"}});
        return JSON.stringify({{
          found: true,
          href: profile.href,
          targetSlug,
          name: norm((withdraw.getAttribute("aria-label") || "").replace(/^Withdraw invitation sent to/i, "")),
          rowText: norm(row.innerText || row.textContent),
          scrollTop: scroller.scrollTop,
          scrollHeight: scroller.scrollHeight,
          withdrawCount: withdraws.length,
          resourceCount: performance.getEntriesByType("resource")
            .filter(e => /invitationsList|invitation|mynetwork|voyager/i.test(e.name)).length
        }});
      }}
      const rect = scroller.getBoundingClientRect();
      return JSON.stringify({{
        found: false,
        targetSlug,
        scrollTop: scroller.scrollTop,
        scrollHeight: scroller.scrollHeight,
        clientHeight: scroller.clientHeight,
        withdrawCount: withdraws.length,
        resourceCount: performance.getEntriesByType("resource")
          .filter(e => /invitationsList|invitation|mynetwork|voyager/i.test(e.name)).length,
        wheelX: Math.round(rect.left + rect.width * 0.5),
        wheelY: Math.round(rect.top + Math.min(rect.height * 0.6, Math.max(140, rect.height - 120)))
      }});
    }})()
    """
    scroll_script = """
    (() => {
      const scroller = document.querySelector("main#workspace");
      if (!scroller) return JSON.stringify({ok: false, reason: "workspace_scroller_missing"});
      const beforeTop = Math.round(scroller.scrollTop);
      const beforeHeight = Math.round(scroller.scrollHeight);
      const clientHeight = Math.round(scroller.clientHeight);
      const delta = Math.max(360, Math.round(clientHeight * 0.85));
      scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + delta);
      scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
      window.dispatchEvent(new Event("scroll"));
      return JSON.stringify({
        ok: true,
        method: "js_scroll_event",
        beforeTop,
        afterTop: Math.round(scroller.scrollTop),
        beforeHeight,
        afterHeight: Math.round(scroller.scrollHeight),
        clientHeight,
        delta
      });
    })()
    """
    bottom_pulse_script = """
    (() => {
      const scroller = document.querySelector("main#workspace");
      if (!scroller) return JSON.stringify({ok: false, reason: "workspace_scroller_missing"});
      const beforeTop = Math.round(scroller.scrollTop);
      const beforeHeight = Math.round(scroller.scrollHeight);
      scroller.scrollTop = scroller.scrollHeight;
      scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
      window.dispatchEvent(new Event("scroll"));
      return JSON.stringify({
        ok: true,
        method: "bottom_pulse",
        beforeTop,
        afterTop: Math.round(scroller.scrollTop),
        beforeHeight,
        afterHeight: Math.round(scroller.scrollHeight),
        clientHeight: Math.round(scroller.clientHeight)
      });
    })()
    """

    stable_end_checks = 0
    max_withdraw_count = 0
    max_scroll_height = 0
    max_resource_count = 0
    stable_end_threshold = 6
    for scroll_index in range(max_scrolls):
        raw = session.cdp.evaluate(scan_script, timeout=12)
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"found": False, "reason": "sent_invitation_scan_invalid", "raw": raw}
        if not isinstance(result, dict):
            return {"found": False, "reason": "sent_invitation_scan_invalid", "raw": result}
        if result.get("found"):
            return {
                **result,
                "scrolls": scroll_index,
                "scroll_method": "js_scroll_event",
                "maxWithdrawCount": max(max_withdraw_count, int(result.get("withdrawCount") or 0)),
                "maxScrollHeight": max(max_scroll_height, int(result.get("scrollHeight") or 0)),
                "maxResourceCount": max(max_resource_count, int(result.get("resourceCount") or 0)),
            }

        before_height = int(result.get("scrollHeight") or 0)
        before_top = int(result.get("scrollTop") or 0)
        client_height = int(result.get("clientHeight") or 0)
        at_bottom = before_top + client_height >= before_height - 5
        max_withdraw_count = max(max_withdraw_count, int(result.get("withdrawCount") or 0))
        max_scroll_height = max(max_scroll_height, before_height)
        max_resource_count = max(max_resource_count, int(result.get("resourceCount") or 0))

        try:
            raw_scroll = session.cdp.evaluate(scroll_script, timeout=8)
            scroll_result = json.loads(raw_scroll) if isinstance(raw_scroll, str) else raw_scroll
        except Exception as exc:
            return {
                "found": False,
                "reason": "sent_invitation_scroll_failed",
                "error": str(exc),
                **result,
            }

        # LinkedIn appends the next invitation page after the workspace scroll event.
        scroll_after_top = (
            int((scroll_result or {}).get("afterTop") or 0)
            if isinstance(scroll_result, dict)
            else 0
        )
        scroll_after_height = (
            int((scroll_result or {}).get("afterHeight") or 0)
            if isinstance(scroll_result, dict)
            else 0
        )
        scroll_client_height = (
            int((scroll_result or {}).get("clientHeight") or client_height)
            if isinstance(scroll_result, dict)
            else client_height
        )
        at_bottom_after_scroll = scroll_after_top + scroll_client_height >= scroll_after_height - 5
        bottom_pulse_result = None
        if at_bottom or at_bottom_after_scroll:
            time.sleep(0.35)
            try:
                raw_pulse = session.cdp.evaluate(bottom_pulse_script, timeout=8)
                bottom_pulse_result = (
                    json.loads(raw_pulse) if isinstance(raw_pulse, str) else raw_pulse
                )
            except Exception:
                bottom_pulse_result = {"ok": False, "reason": "bottom_pulse_failed"}
            time.sleep(5.0)
        else:
            time.sleep(2.5)
        raw_after = session.cdp.evaluate(scan_script, timeout=12)
        try:
            after = json.loads(raw_after) if isinstance(raw_after, str) else raw_after
        except json.JSONDecodeError:
            return {"found": False, "reason": "sent_invitation_scan_invalid", "raw": raw_after}
        if isinstance(after, dict) and after.get("found"):
            return {
                **after,
                "scrolls": scroll_index + 1,
                "scroll_method": "js_scroll_event",
                "lastScroll": scroll_result,
                "lastBottomPulse": bottom_pulse_result,
                "maxWithdrawCount": max(max_withdraw_count, int(after.get("withdrawCount") or 0)),
                "maxScrollHeight": max(max_scroll_height, int(after.get("scrollHeight") or 0)),
                "maxResourceCount": max(max_resource_count, int(after.get("resourceCount") or 0)),
            }
        if not isinstance(after, dict):
            return {"found": False, "reason": "sent_invitation_scan_invalid", "raw": after}

        after_withdraw_count = int(after.get("withdrawCount") or 0)
        after_height = int(after.get("scrollHeight") or 0)
        after_resource_count = int(after.get("resourceCount") or 0)
        grew = (
            after_height > max_scroll_height
            or after_withdraw_count > max_withdraw_count
            or after_resource_count > max_resource_count
        )
        max_withdraw_count = max(max_withdraw_count, after_withdraw_count)
        max_scroll_height = max(max_scroll_height, after_height)
        max_resource_count = max(max_resource_count, after_resource_count)
        after_top = int(after.get("scrollTop") or 0)
        after_client_height = int(after.get("clientHeight") or client_height)
        at_bottom_after = after_top + after_client_height >= after_height - 5
        if grew:
            stable_end_checks = 0
            continue
        if at_bottom or at_bottom_after_scroll or at_bottom_after:
            stable_end_checks += 1
            if stable_end_checks >= stable_end_threshold:
                return {
                    "found": False,
                    "reason": "target_not_found_after_stable_end",
                    "scrolls": scroll_index + 1,
                    "stableBottomChecks": stable_end_checks,
                    "stableEndThreshold": stable_end_threshold,
                    "lastScroll": scroll_result,
                    "lastBottomPulse": bottom_pulse_result,
                    "maxWithdrawCount": max_withdraw_count,
                    "maxScrollHeight": max_scroll_height,
                    "maxResourceCount": max_resource_count,
                    **after,
                }
        else:
            stable_end_checks = 0

    return {
        "found": False,
        "reason": "target_not_found_before_max_scrolls",
        "targetSlug": target_slug,
        "max_scrolls": max_scrolls,
        "maxWithdrawCount": max_withdraw_count,
        "maxScrollHeight": max_scroll_height,
        "maxResourceCount": max_resource_count,
    }


def click_prepared_withdrawal(session: LinkedInSession, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "confirmed": False}
    result = session.cdp.evaluate(
        """
    (async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const norm = s => (s || "").replace(/\\s+/g, " ").trim();
      const visible = el => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      };
      const closeWithdrawalModal = async () => {
        const controls = [...document.querySelectorAll('button,a,[role="button"]')].filter(visible);
        const dismiss = controls.find(el => /^Dismiss$/i.test(norm(el.getAttribute("aria-label"))));
        const cancel = controls.find(el => /^Cancel$/i.test(norm(el.innerText || el.textContent)));
        const close = dismiss || cancel;
        if (close) {
          close.click();
          await sleep(500);
          return norm(close.getAttribute("aria-label")) || norm(close.innerText || close.textContent);
        }
        document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
        await sleep(500);
        return "escape";
      };
      const findConfirm = targetAria => {
        const confirms = [...document.querySelectorAll('button,[role="button"]')].filter(visible)
          .filter(el => {
            const text = norm(el.innerText || el.textContent);
            const aria = norm(el.getAttribute("aria-label"));
            return /^Withdraw$/i.test(text) || /withdraw invitation sent to/i.test(aria);
          });
        return confirms.find(btn => norm(btn.getAttribute("aria-label")) === targetAria) ||
          confirms.find(btn => /withdraw invitation sent to/i.test(norm(btn.getAttribute("aria-label")))) ||
          confirms.find(btn => /^Withdraw$/i.test(norm(btn.innerText || btn.textContent))) ||
          confirms[0] || null;
      };
      const waitForConfirm = async targetAria => {
        for (let i = 0; i < 10; i++) {
          const confirm = findConfirm(targetAria);
          if (confirm) return confirm;
          await sleep(1000);
        }
        return null;
      };
      const row = document.querySelector('[data-codex-withdraw-target="true"]');
      if (!row) return {ok: false, reason: "prepared_row_missing"};
      const open = [...row.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]')].filter(visible)[0];
      if (!open) return {ok: false, reason: "row_withdraw_link_missing"};
      const targetAria = norm(open.getAttribute("aria-label"));
      let confirm = null;
      let confirmOpenAttempts = 0;
      for (let attempt = 0; attempt < 3; attempt++) {
        open.scrollIntoView({block: "center", inline: "center"});
        await sleep(500);
        open.click();
        confirmOpenAttempts += 1;
        await sleep(1500);
        confirm = await waitForConfirm(targetAria);
        if (confirm) break;
        await closeWithdrawalModal();
        await sleep(800);
      }
      if (!confirm) {
        return {
          ok: false,
          reason: "confirm_button_missing",
          targetAria,
          confirmOpenAttempts,
          confirmCandidates: [...document.querySelectorAll('button,a,[role="button"]')].filter(visible)
            .map(el => ({
              tag: el.tagName,
              text: norm(el.innerText || el.textContent),
              aria: norm(el.getAttribute("aria-label")),
              role: norm(el.getAttribute("role")),
              type: norm(el.getAttribute("type"))
            }))
            .filter(item => /withdraw|pending|cancel|dismiss/i.test(`${item.text} ${item.aria}`))
        };
      }
      confirm.click();
      let stillThere = true;
      for (let i = 0; i < 12; i++) {
        await sleep(1000);
        stillThere = [...document.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]')]
          .filter(visible)
          .some(a => norm(a.getAttribute("aria-label")) === targetAria);
        if (!stillThere) break;
      }
      const modalClose = !stillThere ? await closeWithdrawalModal() : "";
      return {ok: !stillThere, confirmed: !stillThere, targetAria, stillThere, modalClose, confirmOpenAttempts};
    })()
    """,
        await_promise=True,
        timeout=75,
    )
    return result if isinstance(result, dict) else {"ok": False, "raw": result}


def canonical_profile_url(url: str) -> str:
    match = re.search(
        r"https?://(?:(?:[a-z]{2,3}|www)\.)?linkedin\.com/in/([^/?#]+)", str(url or ""), re.I
    )
    if not match:
        return str(url or "").strip()
    return f"https://www.linkedin.com/in/{match.group(1).rstrip('/')}/"


def name_similarity(expected: str, actual: str) -> float:
    def tokens(value: str) -> set[str]:
        return {
            token for token in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(token) > 1
        }

    expected_tokens = tokens(expected)
    actual_tokens = tokens(actual)
    if not expected_tokens or not actual_tokens:
        return 0.0
    return len(expected_tokens & actual_tokens) / len(expected_tokens)


def withdraw_from_profile_fallback(
    session: LinkedInSession,
    target: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Withdraw via the target profile's top card and verify it becomes Connect."""
    profile_url = target.get("contact_linkedin", "")
    expected_name = target.get("contact_name", "")
    target_url = canonical_profile_url(profile_url)
    state = inspect_profile_action_state(session.cdp, profile_url)
    current_url = canonical_profile_url(state.get("url", ""))
    similarity = name_similarity(expected_name, state.get("profileName", ""))
    url_matches = bool(target_url and current_url == target_url)
    name_matches = similarity >= 0.67
    if not url_matches and not name_matches:
        return {
            "ok": False,
            "reason": "profile_identity_mismatch",
            "target_url": target_url,
            "current_url": current_url,
            "expected_name": expected_name,
            "profile_name": state.get("profileName", ""),
            "name_similarity": similarity,
            "state": state,
        }

    observed_state = state.get("state", "unknown")
    if observed_state == "already_pending":
        if dry_run:
            return {
                "ok": True,
                "confirmed": False,
                "dry_run": True,
                "terminal_state": "profile_pending",
                "closure_reason": "profile_top_card_pending",
                "via": "profile_fallback",
                "target_url": target_url,
                "current_url": current_url,
                "profile_name": state.get("profileName", ""),
                "name_similarity": similarity,
                "state": state,
            }
        pending_aria = f"Pending, click to withdraw invitation sent to {expected_name}"
        confirm_aria = f"Withdraw invitation sent to {expected_name}"
        raw = session.cdp.evaluate(f"""(() => {{
          const pending = [...document.querySelectorAll('a,button,[role="button"]')]
            .find(el => (el.getAttribute('aria-label') || '') === {json.dumps(pending_aria)});
          if (!pending) return JSON.stringify({{ok:false, reason:'target_pending_control_missing'}});
          pending.click();
          return JSON.stringify({{ok:true, pendingAria: pending.getAttribute('aria-label')}});
        }})()""")
        try:
            action = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            action = {"ok": False, "reason": "profile_pending_click_invalid"}
        if not isinstance(action, dict) or not action.get("ok"):
            return {
                "ok": False,
                "confirmed": False,
                "reason": "profile_pending_click_failed",
                "action": action,
                "state": state,
            }
        selector = f'dialog[data-testid="dialog"] button[aria-label="{confirm_aria}"]'
        visible = False
        for _ in range(16):
            time.sleep(0.5)
            if session.cdp.evaluate(f"document.querySelector({json.dumps(selector)}) !== null"):
                visible = True
                break
        if not visible:
            return {
                "ok": False,
                "confirmed": False,
                "reason": "profile_withdraw_confirmation_missing",
                "action": action,
                "state": state,
            }
        clicked = HumanSimulator(session.cdp).click_element(selector, hover_first=True)
        time.sleep(3)
        verified_state = inspect_profile_action_state(session.cdp, profile_url)
        verified = verified_state.get("state") in {"connect_direct", "connect_in_more"}
        return {
            "ok": bool(verified),
            "confirmed": bool(verified),
            "dry_run": False,
            "terminal_state": "profile_connect"
            if verified
            else "profile_pending_after_confirmation",
            "closure_reason": "profile_top_card_connect"
            if verified
            else "profile_top_card_did_not_change",
            "via": "profile_fallback",
            "action": action,
            "confirmation_click_dispatched": bool(clicked),
            "target_url": target_url,
            "current_url": canonical_profile_url(verified_state.get("url", "")),
            "profile_name": verified_state.get("profileName", ""),
            "name_similarity": similarity,
            "state": verified_state,
        }
    if observed_state in {"connect_direct", "connect_in_more"}:
        return {
            "ok": True,
            "confirmed": False,
            "dry_run": dry_run,
            "terminal_state": "profile_connect",
            "closure_reason": "profile_top_card_connect",
            "via": "profile_fallback",
            "target_url": target_url,
            "current_url": current_url,
            "profile_name": state.get("profileName", ""),
            "name_similarity": similarity,
            "state": state,
        }
    return {
        "ok": False,
        "confirmed": False,
        "dry_run": dry_run,
        "reason": "profile_top_card_state_unresolved",
        "via": "profile_fallback",
        "target_url": target_url,
        "current_url": current_url,
        "profile_name": state.get("profileName", ""),
        "name_similarity": similarity,
        "state": state,
    }


def process_target(
    session: LinkedInSession,
    target: dict[str, Any],
    dry_run: bool,
    activity_timeout: float,
    force_profile_fallback: bool = False,
    require_sent_invitations_row: bool = False,
) -> dict[str, Any]:
    time.sleep(max(0, int(target.get("delay_sec", 0))))
    activity_result = session.read_activity_detail(
        target["contact_linkedin"],
        max_seconds=activity_timeout,
        navigation_type=target.get("navigation_type", "selector_based"),
    )
    activity_value = classify_activity_since_sent(activity_result)
    if force_profile_fallback:
        profile_result = withdraw_from_profile_fallback(session, target, dry_run=dry_run)
        return {
            "ok": bool(profile_result.get("ok")),
            "confirmed": bool(profile_result.get("confirmed")),
            "blocked": not bool(profile_result.get("ok")),
            "dry_run": dry_run,
            "target": target,
            "reason": "forced_profile_fallback",
            "profile_fallback_result": profile_result,
            "activity": activity_result,
            "activity_value": activity_value,
        }
    open_result = open_sent_invitations(session)
    if not open_result.get("ok"):
        return {
            "ok": False,
            "blocked": True,
            "reason": "sent_invitations_not_ready",
            "open_result": open_result,
            "activity": activity_result,
            "activity_value": activity_value,
        }
    row_result = find_and_prepare_row(session, target["contact_linkedin"])
    if not row_result.get("found"):
        if not require_sent_invitations_row:
            profile_result = withdraw_from_profile_fallback(session, target, dry_run=dry_run)
            return {
                "ok": bool(profile_result.get("ok")),
                "confirmed": bool(profile_result.get("confirmed")),
                "blocked": not bool(profile_result.get("ok")),
                "dry_run": dry_run,
                "target": target,
                "reason": "target_not_found_in_sent_invitations_profile_fallback",
                "row_result": row_result,
                "profile_fallback_result": profile_result,
                "activity": activity_result,
                "activity_value": activity_value,
            }
        return {
            "ok": bool(profile_result.get("ok")),
            "confirmed": bool(profile_result.get("confirmed")),
            "blocked": True,
            "dry_run": dry_run,
            "target": target,
            "reason": "target_not_found_in_sent_invitations",
            "row_result": row_result,
            "activity": activity_result,
            "activity_value": activity_value,
        }
    try:
        withdrawal_result = click_prepared_withdrawal(session, dry_run=dry_run)
    except RuntimeError as exc:
        if not is_cdp_target_detached_error(exc):
            raise
        reconnect_result: dict[str, Any] = {"attempted": True}
        try:
            session.disconnect()
            time.sleep(2)
            reconnect_result["connected"] = bool(session.connect(skip_rate_check=True).get("ok"))
        except Exception as reconnect_exc:
            return {
                "ok": False,
                "confirmed": False,
                "blocked": True,
                "dry_run": dry_run,
                "target": target,
                "reason": "cdp_target_detached_reconnect_failed",
                "error": str(exc),
                "reconnect_error": str(reconnect_exc),
                "row_result": row_result,
                "activity": activity_result,
                "activity_value": activity_value,
            }
        profile_result = withdraw_from_profile_fallback(session, target, dry_run=dry_run)
        return {
            "ok": False,
            "confirmed": False,
            "dry_run": dry_run,
            "target": target,
            "reason": "cdp_target_detached_during_sent_invitations_click_profile_recovery",
            "error": str(exc),
            "row_result": row_result,
            "profile_fallback_result": profile_result,
            "terminal_state": profile_result.get("terminal_state"),
            "closure_reason": profile_result.get("closure_reason"),
            "reconnect_result": reconnect_result,
            "activity": activity_result,
            "activity_value": activity_value,
            "blocked": not bool(profile_result.get("ok")),
        }
    return {
        "ok": bool(withdrawal_result.get("ok")),
        "confirmed": bool(withdrawal_result.get("confirmed")),
        "dry_run": dry_run,
        "target": target,
        "activity": activity_result,
        "activity_value": activity_value,
        "row_result": row_result,
        "withdrawal_result": withdrawal_result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run prepared LinkedIn connection withdrawals.")
    parser.add_argument(
        "--session",
        default="",
        help="Prepared session path. Defaults to today's withdrawal session.",
    )
    parser.add_argument("--credentials", default=CREDS_PATH)
    parser.add_argument("--sheet-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--date",
        type=parse_run_date,
        default=None,
        help="Accounting date as YYYY-MM-DD. Defaults to current date in timezone.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip targets already confirmed in this session's withdrawal journal.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--activity-timeout", type=float, default=180.0)
    parser.add_argument(
        "--cooldown-hours",
        type=float,
        default=2.0,
        help="Minimum hours between live withdrawal runs.",
    )
    parser.add_argument("--max-consecutive-profile-fallbacks", type=int, default=5)
    parser.add_argument(
        "--force-profile-fallback",
        action="store_true",
        help="Bypass Sent Invitations row matching and withdraw through the profile fallback path.",
    )
    parser.add_argument(
        "--require-sent-invitations-row",
        action="store_true",
        help="Block instead of using profile fallback when the target is not found in Sent Invitations.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(ZoneInfo(args.timezone))
    run_date = args.date or now.date()
    run_date_text = run_date.isoformat()
    session_path = (
        Path(args.session)
        if args.session
        else ROOT / "state" / "withdrawal_sessions" / f"{run_date_text}.json"
    )
    journal_path = ROOT / "state" / "withdrawal_journal" / f"{run_date_text}.jsonl"
    payload = load_json(session_path)
    queue = payload.get("runtime_plan", {}).get("queue", [])
    completed_ids = completed_prospect_ids(journal_path, session_path) if args.resume else set()
    if completed_ids:
        queue = [
            target
            for target in queue
            if str(target.get("prospect_id") or "").strip() not in completed_ids
        ]
    if args.limit > 0:
        queue = queue[: args.limit]
    if not queue:
        print(
            json.dumps(
                {
                    "ok": False,
                    "blockers": ["No prepared withdrawal targets found"],
                    "session": str(session_path),
                },
                indent=2,
            )
        )
        return 1

    global LIVE_RUN_COOLDOWN_SECONDS
    LIVE_RUN_COOLDOWN_SECONDS = int(max(0, args.cooldown_hours) * 60 * 60)
    if not args.dry_run and LIVE_RUN_COOLDOWN_SECONDS > 0:
        cooldown = check_live_run_cooldown(now)
        if cooldown:
            output = {
                "ok": False,
                "blockers": ["Withdrawal live-run cooldown active"],
                "session": str(session_path),
                **cooldown,
            }
            append_jsonl(journal_path, [{"event": "withdrawal_run_blocked_cooldown", **output}])
            print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
            return 1

    client = get_client(args.credentials)
    spreadsheet = open_sheet(client, args.sheet_url)
    outreach_ws = get_worksheet(spreadsheet, OUTREACH_LOG_TAB)
    withdrawn_ws = ensure_withdrawn_tab(spreadsheet)

    linkedin = LinkedInSession()
    connected = linkedin.connect(skip_rate_check=True)
    if not connected.get("ok"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "blockers": ["LinkedIn connect/preflight failed"],
                    "detail": connected,
                },
                indent=2,
            )
        )
        return 1
    if not args.dry_run:
        write_guard(
            {
                **read_guard(),
                "last_live_run_started_at": now.isoformat(),
                "last_live_run_session": str(session_path),
                "cooldown_seconds": LIVE_RUN_COOLDOWN_SECONDS,
            }
        )

    result = {
        "ok": True,
        "dry_run": args.dry_run,
        "processed": 0,
        "confirmed": 0,
        "failed": 0,
        "blocked": 0,
        "profile_fallbacks": 0,
        "forced_profile_fallback": args.force_profile_fallback,
        "require_sent_invitations_row": args.require_sent_invitations_row,
        "max_consecutive_profile_fallbacks": args.max_consecutive_profile_fallbacks,
        "date": run_date_text,
        "session": str(session_path),
        "resume": args.resume,
        "resume_skips": len(completed_ids),
    }
    events: list[dict[str, Any]] = []
    consecutive_profile_fallbacks = 0
    try:
        for batch in payload.get("runtime_plan", {}).get("batches", []):
            targets = [
                target
                for target in batch.get("targets", [])
                if str(target.get("prospect_id") or "").strip() not in completed_ids
            ]
            if args.limit > 0:
                remaining = args.limit - result["processed"]
                targets = targets[: max(0, remaining)]
            if not targets:
                continue
            for target in targets:
                if args.limit > 0 and result["processed"] >= args.limit:
                    break
                try:
                    processed = process_target(
                        linkedin,
                        target,
                        args.dry_run,
                        args.activity_timeout,
                        force_profile_fallback=args.force_profile_fallback,
                        require_sent_invitations_row=args.require_sent_invitations_row,
                    )
                except Exception as exc:
                    processed = {
                        "ok": False,
                        "confirmed": False,
                        "blocked": True,
                        "dry_run": args.dry_run,
                        "target": target,
                        "reason": "target_processing_exception",
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                    }
                result["processed"] += 1
                used_profile_fallback = processed.get("reason") in {
                    "target_not_found_in_sent_invitations_profile_fallback",
                    "forced_profile_fallback",
                }
                if used_profile_fallback:
                    consecutive_profile_fallbacks += 1
                    result["profile_fallbacks"] += 1
                    processed["consecutive_profile_fallbacks"] = consecutive_profile_fallbacks
                else:
                    consecutive_profile_fallbacks = 0
                event = {
                    "event": "withdrawal_target_result",
                    "prospect_id": target.get("prospect_id"),
                    "session": str(session_path),
                    **processed,
                }
                events.append(event)
                append_jsonl(journal_path, [event])
                if processed.get("blocked"):
                    result["blocked"] += 1
                    result["ok"] = False
                    break
                if processed.get("confirmed"):
                    result["confirmed"] += 1
                    withdrawn_at = run_date_text
                    update_row_fields(
                        outreach_ws,
                        int(target["row_number"]),
                        {
                            "Current Progress": "Request Withdrawn",
                            "Last Action Date": withdrawn_at,
                        },
                    )
                    upsert_withdrawn_lead(
                        withdrawn_ws,
                        target,
                        processed.get("activity_value", "Uncertain"),
                        withdrawn_at,
                    )
                elif not args.dry_run:
                    result["failed"] += 1
                else:
                    result["confirmed"] += 0

                if (
                    args.max_consecutive_profile_fallbacks > 0
                    and consecutive_profile_fallbacks >= args.max_consecutive_profile_fallbacks
                ):
                    block_event = {
                        "event": "withdrawal_run_blocked_consecutive_profile_fallbacks",
                        "ok": False,
                        "reason": "consecutive_profile_fallback_limit_reached",
                        "consecutive_profile_fallbacks": consecutive_profile_fallbacks,
                        "max_consecutive_profile_fallbacks": args.max_consecutive_profile_fallbacks,
                        "last_prospect_id": target.get("prospect_id"),
                    }
                    events.append(block_event)
                    append_jsonl(journal_path, [block_event])
                    result["blocked"] += 1
                    result["ok"] = False
                    break
            if result["blocked"]:
                break
            delay = int(batch.get("inter_batch_delay_sec") or 0)
            if (
                delay > 0
                and not args.dry_run
                and (args.limit <= 0 or result["processed"] < args.limit)
            ):
                time.sleep(delay)
    finally:
        linkedin.disconnect()

    if not args.dry_run:
        guard = read_guard()
        guard.update(
            {
                "last_live_run_finished_at": datetime.now(ZoneInfo(args.timezone)).isoformat(),
                "last_live_run_summary": result,
            }
        )
        write_guard(guard)
    append_jsonl(journal_path, [{"event": "withdrawal_run_summary", **result}])
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
