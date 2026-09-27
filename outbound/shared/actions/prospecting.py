"""People-you-may-know suggestion box scan.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S23). Pure move.
"""

import json
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import detect_page
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def scan_prospect_box(
    cdp: CDPConnection,
    sim: HumanSimulator,
) -> dict[str, Any]:
    """Scan the 'People you may know' suggestions box on profiles or My Network.

    Approach D: Prospect box scan — find related prospects from LinkedIn's suggestions.
    Returns suggestion data for the agent to evaluate.
    """
    result = {"action": "scan_prospect_box", "suggestions": []}

    # Check current page for suggestions or navigate to My Network
    page = detect_page(cdp)
    if page["page_type"] not in ("profile", "my_network"):
        cdp.navigate("https://www.linkedin.com/mynetwork/")
        human_delay(2, 4)

    suggestion_data = cdp.evaluate("""
        (() => {
            const suggestions = [];
            const cards = document.querySelectorAll(
                '.discover-entity-card, ' +
                '[class*="pymk"], ' +
                '.mn-pymk-list__card, ' +
                '[class*="people-you-may-know"] li'
            );
            cards.forEach((card, i) => {
                if (i >= 10) return;
                const linkEl = card.querySelector('a[href*="/in/"]');
                const nameEl = card.querySelector(
                    '[class*="discover-person-card__name"], ' +
                    '[class*="entity-result__title"], ' +
                    'span[dir="ltr"]'
                );
                const headlineEl = card.querySelector(
                    '[class*="discover-person-card__occupation"], ' +
                    '[class*="entity-result__summary"], ' +
                    '[class*="subline"]'
                );
                const mutualEl = card.querySelector(
                    '[class*="member-insights"], ' +
                    '[class*="mutual"]'
                );

                const url = linkEl ? linkEl.href.split('?')[0] : '';
                const name = nameEl ? nameEl.innerText.trim() : '';
                const headline = headlineEl ? headlineEl.innerText.trim() : '';
                const mutual = mutualEl ? mutualEl.innerText.trim() : '';

                if (url && name) {
                    suggestions.push({
                        url: url,
                        name: name,
                        headline: headline.substring(0, 120),
                        mutual_connections: mutual.substring(0, 80),
                        index: i,
                    });
                }
            });
            return JSON.stringify(suggestions);
        })()
    """)

    suggestions = json.loads(suggestion_data) if suggestion_data else []
    result["suggestions"] = suggestions
    result["count"] = len(suggestions)

    return result
