"""Connections-page DOM extractor (LazyColumn + legacy fallbacks).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S21). Pure move.
"""

from outbound.shared.browser.connection import CDPConnection


def _extract_connections_dom(cdp: CDPConnection) -> str | None:
    """Extract connection card data from the DOM via Runtime.evaluate.

    Factored out of check_acceptances to enable retry logic.

    Strategy: LinkedIn uses obfuscated class names that rotate with deploys,
    so we anchor on stable structural attributes:
    - data-component-type="LazyColumn" for the list container
    - data-display-contents="true" for individual card wrappers
    - a[href*="/in/"] for profile links
    - innerText parsing for name, headline, and connection date
    Falls back to scanning all profile links if the structural selectors change.
    """
    return cdp.evaluate("""
        (() => {
            const connections = [];
            let cards = [];

            // Strategy 1: Find cards via LazyColumn > data-display-contents
            const lazyCol = document.querySelector('[data-component-type="LazyColumn"]');
            if (lazyCol) {
                cards = lazyCol.querySelectorAll('[data-display-contents="true"]');
            }

            // Strategy 2 (legacy fallback): old-style selectors
            if (cards.length === 0) {
                cards = document.querySelectorAll(
                    '.mn-connection-card, ' +
                    '[class*="connection-card"], ' +
                    '.scaffold-finite-scroll__content li'
                );
            }

            // Strategy 3 (broad fallback): any container with a /in/ link
            // Group profile links by their nearest shared ancestor
            if (cards.length === 0) {
                const allLinks = document.querySelectorAll('a[href*="/in/"]');
                const seen = new Set();
                allLinks.forEach(a => {
                    // Walk up to find a container-level parent
                    let container = a.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (container && container.parentElement &&
                            container.parentElement.children.length >= 5) {
                            break;
                        }
                        if (container) container = container.parentElement;
                    }
                    if (container && !seen.has(container)) {
                        seen.add(container);
                        cards = [...(cards || []), container];
                    }
                });
            }

            const cardArr = Array.from(cards);
            cardArr.forEach((card, i) => {
                if (i >= 30) return;

                // Find profile link (prefer the one with text content)
                const allLinks = card.querySelectorAll('a[href*="/in/"]');
                let linkEl = null;
                let nameFromLink = '';
                for (const a of allLinks) {
                    const text = a.innerText.trim();
                    if (text.length > 0) {
                        linkEl = a;
                        nameFromLink = text.split('\\n')[0].trim();
                        break;
                    }
                }
                // Fall back to first link if none had text
                if (!linkEl && allLinks.length > 0) {
                    linkEl = allLinks[0];
                }

                const url = linkEl ? linkEl.href.split('?')[0] : '';

                // Parse card text for name, time, etc.
                const fullText = card.innerText || '';
                const lines = fullText.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l.length > 0 && l !== 'Message');

                // Name is the first meaningful line (or from link text)
                const name = nameFromLink || (lines.length > 0 ? lines[0] : '');

                // Time/date: look for "Connected on" or relative time patterns
                let timeText = '';
                for (const line of lines) {
                    const lower = line.toLowerCase();
                    if (lower.includes('connected on') ||
                        lower.includes('ago') ||
                        lower.includes('today') ||
                        lower.includes('yesterday') ||
                        lower.includes('just now')) {
                        timeText = lower;
                        break;
                    }
                }

                if (url && name) {
                    connections.push({
                        url: url,
                        name: name,
                        time_text: timeText,
                        index: i,
                    });
                }
            });
            return JSON.stringify(connections);
        })()
    """)
