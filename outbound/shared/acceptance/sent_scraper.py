"""Sent-invitations scraper (LazyColumn-aware, accumulates across snapshots).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S20). Pure move.
"""

import json
import random
from typing import Any

from outbound.shared.acceptance.normalization import (
    _normalize_linkedin_profile_url,
    _normalize_sent_invitation_name,
)
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _navigate_with_readiness, _safe_scroll_or_js
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def scrape_sent_invitations(
    cdp: CDPConnection,
    sim: HumanSimulator,
    max_scroll_passes: int = 20,
) -> dict[str, Any]:
    """Scrape the Sent Invitations page for all currently pending invitations.

    Returns a dict with:
    - 'invitations': list of {url, name, headline, sent_text} for each pending invite
    - 'urls': set of normalized profile URLs for fast lookup

    Uses LazyColumn structure. Scrolls to load more if needed and accumulates
    unique invitations across snapshots because LinkedIn can virtualize the list.
    """
    result: dict[str, Any] = {"action": "scrape_sent_invitations", "invitations": []}

    SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
    EXPECTED_SELECTOR = '[data-component-type="LazyColumn"]'

    nav = _navigate_with_readiness(
        cdp,
        SENT_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            SENT_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Sent invitations page not ready: "
            f"readyState={nav.get('readyState')}, "
            f"overlay={nav.get('overlay_detected')}, "
            f"selector_found={nav.get('selector_found')}"
        )
        return result

    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    cdp.evaluate("window.scrollTo(0, 0)")
    human_delay(1, 2)

    # LinkedIn's sent-invitations page can keep only part of the list stable in
    # the DOM while new rows appear during scrolling, so count unique rows across
    # every observed snapshot instead of trusting the final LazyColumn contents.
    seen_keys: set = set()
    seen_urls: set = set()
    unique: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    stale_passes = 0

    for scroll_pass in range(max_scroll_passes):
        raw = _extract_sent_invitations_dom(cdp)
        invitations = json.loads(raw) if raw else []
        new_count = 0

        for inv in invitations:
            url = _normalize_linkedin_profile_url(inv.get("url", ""))
            name = _normalize_sent_invitation_name(inv.get("name", ""))
            sent_text = (inv.get("sent_text") or "").strip().lower()
            key = url or f"name:{name}|sent:{sent_text}"
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            if url:
                seen_urls.add(url)
            unique.append(inv)
            new_count += 1

        metrics_raw = cdp.evaluate("""
            (() => JSON.stringify({
                scrollY: Math.round(window.scrollY || 0),
                innerHeight: Math.round(window.innerHeight || 0),
                scrollHeight: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0)
            }))()
        """)
        metrics = json.loads(metrics_raw) if metrics_raw else {}
        at_bottom = (
            metrics.get("scrollY", 0) + metrics.get("innerHeight", 0)
            >= metrics.get("scrollHeight", 0) - 50
        )
        snapshots.append(
            {
                "pass": scroll_pass + 1,
                "snapshot_count": len(invitations),
                "new_count": new_count,
                "unique_count": len(unique),
                "scrollY": metrics.get("scrollY", 0),
                "scrollHeight": metrics.get("scrollHeight", 0),
                "at_bottom": at_bottom,
            }
        )

        if new_count == 0:
            stale_passes += 1
        else:
            stale_passes = 0

        if at_bottom and stale_passes >= 2:
            break
        if stale_passes >= 4:
            break

        _safe_scroll_or_js(cdp, sim, random.randint(800, 1400))
        human_delay(1.5, 3)

    result["invitations"] = unique
    result["count"] = len(unique)
    result["urls"] = seen_urls
    result["scroll_snapshots"] = snapshots
    return result


def _extract_sent_invitations_dom(cdp: CDPConnection) -> str | None:
    """Extract sent invitation data from the LazyColumn DOM structure.

    Each invitation card is a div child of LazyColumn (alternating with hr dividers).
    Card innerText format: "Name\\n\\nHeadline\\n\\nSent X ago\\n\\nWithdraw"
    Profile URL is in a[href*="/in/"] (avatar link, no text).
    """
    return cdp.evaluate("""
        (() => {
            const invitations = [];
            const lazy = document.querySelector('[data-component-type="LazyColumn"]');
            if (!lazy) return JSON.stringify(invitations);

            const directChildren = Array.from(lazy.children).filter(
                c => c.tagName.toLowerCase() !== 'hr'
            );
            const nestedCards = Array.from(lazy.querySelectorAll(
                '[data-display-contents="true"], li, .artdeco-list__item'
            ));
            const children = [...directChildren, ...nestedCards].filter((card, idx, arr) =>
                card && arr.indexOf(card) === idx
            );

            children.forEach((card, i) => {
                // Find profile URL from avatar link
                const linkEl = card.querySelector('a[href*="/in/"]');
                const url = linkEl ? linkEl.href.split('?')[0] : '';

                // Parse innerText for name, headline, sent time
                const fullText = card.innerText || '';
                if (!/\\bwithdraw\\b/i.test(fullText) || !/\\bsent\\b/i.test(fullText)) {
                    return;
                }
                const lines = fullText.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l.length > 0 && l.toLowerCase() !== 'withdraw');

                const name = lines.length > 0 ? lines[0] : '';
                let headline = '';
                let sentText = '';

                for (const line of lines) {
                    const lower = line.toLowerCase();
                    if (lower.startsWith('sent ')) {
                        sentText = lower;
                    } else if (line !== name) {
                        headline = headline || line;
                    }
                }

                if (url && name) {
                    invitations.push({
                        url: url,
                        name: name,
                        headline: headline.substring(0, 150),
                        sent_text: sentText,
                        index: i,
                    });
                }
            });

            return JSON.stringify(invitations);
        })()
    """)
