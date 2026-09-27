"""Engagement approaches (feed like, engagement trail, reaction mining).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S23c). Pure move.
"""

import json
import random
from typing import Any

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.danger.detection import check_circuit_breakers
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator
from outbound.shared.quota import (
    MAX_PROFILE_VIEWS_PER_DAY,
    get_counter,
    increment_counter,
)


def like_post(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Like a post visible in the current viewport or navigate to a specific post URL.

    Approach E: Feed engagement / filler activity between connection requests.
    """
    result = {"action": "like_post", "success": False}

    if post_url:
        cdp.navigate(post_url)
        human_delay(2, 4)
        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            return result

    # Find and click the like button
    like_info = cdp.evaluate("""
        (() => {
            // Find the like button (not already liked)
            const likeBtns = document.querySelectorAll(
                'button[aria-label*="Like"], button[aria-label*="like"]'
            );
            for (const btn of likeBtns) {
                const label = btn.getAttribute('aria-label') || '';
                const pressed = btn.getAttribute('aria-pressed');
                // Skip if already liked
                if (pressed === 'true') continue;
                // Skip "Unlike" buttons
                if (label.toLowerCase().startsWith('unlike')) continue;
                return JSON.stringify({
                    found: true,
                    label: label.substring(0, 100),
                    already_liked: false,
                });
            }
            // Check if already liked
            const alreadyLiked = document.querySelector(
                'button[aria-pressed="true"][aria-label*="like"]'
            );
            if (alreadyLiked) {
                return JSON.stringify({found: true, already_liked: true});
            }
            return JSON.stringify({found: false});
        })()
    """)

    info = json.loads(like_info) if like_info else {}

    if not info.get("found"):
        result["error"] = "like_button_not_found"
        return result

    if info.get("already_liked"):
        result["error"] = "already_liked"
        return result

    # Human-like reading pause before liking
    human_delay(2, 6, distribution="gaussian")

    clicked = sim.click_element(
        'button[aria-label*="Like"]:not([aria-pressed="true"]), '
        'button[aria-label*="like"]:not([aria-pressed="true"])'
    )

    if clicked:
        result["success"] = True
        increment_counter(state, "likes")
        human_delay(0.5, 2)
    else:
        result["error"] = "like_click_failed"

    return result


def follow_engagement_trail(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    post_url: str,
    max_profiles: int = 3,
) -> dict[str, Any]:
    """Visit profiles of people who engaged with a prospect's post.

    Approach B: Follow the engagement trail — visit likers/commenters
    to build a natural browsing pattern before connecting with the prospect.

    Returns list of profiles visited (for the agent to potentially use).
    """
    result = {
        "action": "follow_engagement_trail",
        "post_url": post_url,
        "profiles_visited": [],
    }

    cdp.navigate(post_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Read the post content first (natural behavior)
    human_delay(3, 8, distribution="gaussian")
    sim.scroll(random.randint(100, 300))
    human_delay(1, 3)

    # Extract commenter/reactor profile links
    trail_data = cdp.evaluate(f"""
        (() => {{
            const profiles = [];
            const seen = new Set();

            // Get commenters
            const commentAuthors = document.querySelectorAll(
                '.comments-comment-item__post-meta a[href*="/in/"], ' +
                '.comments-post-meta__profile-info-wrapper a[href*="/in/"]'
            );
            commentAuthors.forEach(a => {{
                const href = a.href.split('?')[0];
                if (!seen.has(href)) {{
                    seen.add(href);
                    const name = a.innerText.trim().split('\\n')[0];
                    profiles.push({{url: href, name: name, source: 'commenter'}});
                }}
            }});

            // Get reactor profile links (from reaction overlay if visible)
            const reactorLinks = document.querySelectorAll(
                '.social-details-reactors-tab-body a[href*="/in/"], ' +
                'a.social-details-social-counts__count-value'
            );
            reactorLinks.forEach(a => {{
                const href = a.href.split('?')[0];
                if (href.includes('/in/') && !seen.has(href)) {{
                    seen.add(href);
                    const name = a.innerText.trim().split('\\n')[0];
                    profiles.push({{url: href, name: name, source: 'reactor'}});
                }}
            }});

            return JSON.stringify(profiles.slice(0, {max_profiles + 2}));
        }})()
    """)

    profiles = json.loads(trail_data) if trail_data else []

    if not profiles:
        result["profiles_found"] = 0
        return result

    # Shuffle and visit a subset
    random.shuffle(profiles)
    to_visit = profiles[:max_profiles]

    for profile in to_visit:
        # Check view quota
        if get_counter(state, "profile_views") >= MAX_PROFILE_VIEWS_PER_DAY:
            break

        human_delay(2, 5, distribution="gaussian")
        cdp.navigate(profile["url"])
        human_delay(2, 4)

        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            break

        # Brief natural scroll
        scroll_depth = random.uniform(0.2, 0.5)
        sim.scroll_to_bottom(fraction=scroll_depth, speed="slow")
        human_delay(3, 10, distribution="gaussian")

        increment_counter(state, "profile_views")
        result["profiles_visited"].append(
            {
                "url": profile["url"],
                "name": profile.get("name", ""),
                "source": profile.get("source", ""),
            }
        )

    return result


def scan_reaction_list(
    cdp: CDPConnection,
    sim: HumanSimulator,
    post_url: str,
) -> dict[str, Any]:
    """Open the reaction list on a post and extract reactor profiles.

    Approach C: Reaction mining — find new prospects from post reactions.
    Returns reactor data for the agent to evaluate.
    """
    result = {"action": "scan_reaction_list", "post_url": post_url, "reactors": []}

    cdp.navigate(post_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Read post naturally first
    human_delay(2, 5)

    # Click on the reaction count to open the reactor list
    clicked = sim.click_element(
        "button.social-details-social-counts__reactions-count, "
        'button[aria-label*="reaction"], '
        "span.social-details-social-counts__reactions-count"
    )

    if not clicked:
        result["error"] = "reaction_count_not_clickable"
        return result

    human_delay(1.5, 3)

    # Scroll the reactor list a bit
    sim.scroll(random.randint(200, 400))
    human_delay(1, 3)

    # Extract reactor profiles
    reactor_data = cdp.evaluate("""
        (() => {
            const reactors = [];
            const items = document.querySelectorAll(
                '.social-details-reactors-tab-body__profile-link, ' +
                '.social-details-reactors-tab-body li a[href*="/in/"], ' +
                '[class*="reactor"] a[href*="/in/"]'
            );
            items.forEach((item, i) => {
                if (i >= 20) return;
                const href = item.href ? item.href.split('?')[0] : '';
                const nameEl = item.querySelector('span[class*="name"], span[dir="ltr"]');
                const name = nameEl ? nameEl.innerText.trim() : item.innerText.trim().split('\\n')[0];
                const headlineEl = item.closest('li')?.querySelector('[class*="headline"], [class*="subline"]');
                const headline = headlineEl ? headlineEl.innerText.trim() : '';
                if (href.includes('/in/')) {
                    reactors.push({
                        url: href,
                        name: name,
                        headline: headline.substring(0, 100),
                        index: i,
                    });
                }
            });
            return JSON.stringify(reactors);
        })()
    """)

    reactors = json.loads(reactor_data) if reactor_data else []
    result["reactors"] = reactors
    result["count"] = len(reactors)

    # Dismiss the modal
    human_delay(1, 2)
    sim.click_element('button[aria-label="Dismiss"], button[aria-label="Close"]')
    human_delay(0.5, 1.5)

    return result
