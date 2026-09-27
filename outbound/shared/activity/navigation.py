"""Activity-tab navigation — DOM click paths and profile activity entry.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S11). Pure move.
"""

import json
import time
from typing import Any

from outbound.shared.browser.connection import CDPConnection


def _open_activity_tab(cdp: CDPConnection, tab_label: str) -> dict[str, Any]:
    """Open a specific activity sub-tab by visible label."""
    requested = str(tab_label).strip().lower()
    if requested in {"all", "current"}:
        return {
            "found": True,
            "clicked": False,
            "via": "current_all_activity_url",
            "selected_before": True,
        }
    if requested not in {"posts", "comments", "reactions"}:
        return {
            "found": False,
            "clicked": False,
            "via": "blocked_disallowed_activity_tab",
            "text": requested,
        }
    raw = cdp.evaluate(
        f"""
        (async () => {{
            const label = {json.dumps(tab_label)};
            const normalized = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const target = normalized(label);
            const disallowedPath = /\\/recent-activity\\/(articles|videos|images|documents)\\/?/i;
            const visible = (el) => {{
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            }};
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

            const isSelected = (el) => {{
                if (!el) return false;
                if (el.getAttribute('aria-pressed') === 'true') return true;
                if (el.getAttribute('aria-selected') === 'true') return true;
                const classes = (el.className || '').toString().toLowerCase();
                return classes.includes('selected') || classes.includes('active');
            }};

            const roots = () => {{
                const out = [document];
                const walk = (node) => {{
                    if (!node) return;
                    if (node.shadowRoot) out.push(node.shadowRoot);
                    for (const child of node.children || []) walk(child);
                }};
                walk(document.documentElement);
                return out;
            }};

            const isAllowedActivityCandidate = (el) => {{
                if (!el) return false;
                const text = normalized(el.innerText || el.textContent);
                const aria = normalized(el.getAttribute('aria-label'));
                const href = el.href || el.getAttribute('href') || '';
                if (disallowedPath.test(href)) return false;
                return text === target || aria === target;
            }};

            const clickCandidate = (root) => {{
                if (!root) return null;
                const candidates = root.querySelectorAll('button, a, [role="tab"], [role="menuitem"], [role="button"], .artdeco-dropdown__item, span, div[role="button"]');
                for (const el of candidates) {{
                    if (!visible(el)) continue;
                    if (!isAllowedActivityCandidate(el)) continue;
                    const clickable = el.closest('button, a, [role="tab"], [role="menuitem"], [role="button"], .artdeco-dropdown__item') || el;
                    const href = clickable.href || clickable.getAttribute('href') || '';
                    if (disallowedPath.test(href)) continue;
                    clickable.scrollIntoView({{block: 'center', inline: 'center'}});
                    clickable.click();
                    return {{
                        clicked: true,
                        selected_before: isSelected(clickable),
                        text: normalized(clickable.innerText),
                        id: clickable.id || '',
                        role: clickable.getAttribute('role') || '',
                        href,
                    }};
                }}
                return null;
            }};

            for (const root of roots()) {{
                const direct = clickCandidate(root);
                if (direct) {{
                    return JSON.stringify({{
                        found: true,
                        clicked: true,
                        via: 'direct',
                        selected_before: !!direct.selected_before,
                        text: direct.text,
                    }});
                }}
            }}

            const clickReactionsFromOpenMenu = () => {{
                const menuSelectors = [
                    '.artdeco-dropdown__content--is-open',
                    '.artdeco-dropdown__content',
                    '[role="menu"]',
                    '[id*="dropdown"]',
                    '[class*="dropdown"]'
                ];
                for (const root of roots()) {{
                    for (const menu of root.querySelectorAll(menuSelectors.join(','))) {{
                        if (!visible(menu)) continue;
                        const hit = clickCandidate(menu);
                        if (hit) return hit;
                    }}
                }}
                return null;
            }};

            const isMoreButton = (el) => {{
                if (!visible(el)) return false;
                const text = normalized(el.innerText || el.textContent);
                const aria = normalized(el.getAttribute('aria-label'));
                const classes = (el.className || '').toString();
                return (text === 'more' || aria === 'more') && (
                    (el.id || '').startsWith('overflow-button-') ||
                    classes.includes('profile-creator-shared-pills__pill') ||
                    classes.includes('artdeco-pill') ||
                    el.getAttribute('aria-expanded') !== null
                );
            }};

            const alreadyOpen = target === 'reactions' ? clickReactionsFromOpenMenu() : null;
            if (alreadyOpen) {{
                return JSON.stringify({{
                    found: true,
                    clicked: true,
                    via: 'open_more_menu',
                    selected_before: !!alreadyOpen.selected_before,
                    text: alreadyOpen.text,
                    id: alreadyOpen.id,
                    role: alreadyOpen.role,
                    href: alreadyOpen.href,
                }});
            }}

            if (target === 'reactions') {{
                const moreButtons = [];
                for (const root of roots()) {{
                    moreButtons.push(...Array.from(root.querySelectorAll('button, [role="button"], a')).filter(isMoreButton));
                }}
                for (const more of moreButtons) {{
                    more.scrollIntoView({{block: 'center', inline: 'center'}});
                    more.click();
                    await sleep(700);
                    const opened = clickReactionsFromOpenMenu();
                    if (opened) {{
                        return JSON.stringify({{
                            found: true,
                            clicked: true,
                            via: 'more_menu',
                            selected_before: !!opened.selected_before,
                            text: opened.text,
                            id: opened.id,
                            role: opened.role,
                            href: opened.href,
                        }});
                    }}
                }}
            }}

            return JSON.stringify({{found: false, clicked: false, via: '', selected_before: false, text: ''}});
        }})()
    """,
        await_promise=True,
    )
    return json.loads(raw) if raw else {"found": False, "clicked": False}


def _open_profile_activity_from_profile(
    cdp: CDPConnection, timeout: float = 25.0
) -> dict[str, Any]:
    """Check immediately, then every five seconds before URL fallback."""
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    checks = 0
    while True:
        checks += 1
        result = _try_open_profile_activity_from_profile(cdp)
        result.update(checks=checks, elapsed_sec=round(time.monotonic() - started, 2))
        if result.get("clicked") or time.monotonic() >= deadline:
            return result
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def _try_open_profile_activity_from_profile(cdp: CDPConnection) -> dict[str, Any]:
    """Click the profile page's visible Show all posts link into activity."""
    raw = cdp.evaluate("""
        (() => {
            const normalized = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            };
            const candidates = Array.from(document.querySelectorAll(
                'a[aria-label="Show all posts"][href*="/recent-activity/all/"], ' +
                'a[href*="/recent-activity/all/"], ' +
                'a[aria-label="Show all posts"]'
            )).filter(visible);
            const preferred = candidates.find((el) => normalized(el.innerText) === 'show all posts') || candidates[0];
            if (!preferred) {
                return JSON.stringify({found: false, clicked: false, reason: 'show_all_posts_not_found'});
            }
            preferred.scrollIntoView({block: 'center', inline: 'center'});
            const rect = preferred.getBoundingClientRect();
            const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(x, y);
            if (!hit || !preferred.contains(hit) || preferred.getAttribute('aria-disabled') === 'true') {
                return JSON.stringify({found: true, clicked: false, reason: 'show_all_posts_obstructed'});
            }
            const href = preferred.href || preferred.getAttribute('href') || '';
            preferred.click();
            return JSON.stringify({
                found: true,
                clicked: true,
                text: normalized(preferred.innerText),
                aria: preferred.getAttribute('aria-label') || '',
                href
            });
        })()
    """)
    return json.loads(raw) if raw else {"found": False, "clicked": False}
