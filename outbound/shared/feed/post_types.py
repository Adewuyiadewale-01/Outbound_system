"""Feed post-type detection and content-aware scroll sessions.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S22). Pure move.
"""

import random

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def detect_post_type(cdp: CDPConnection) -> str:
    """Detect the type of post currently visible in the viewport center."""
    result = cdp.evaluate("""
        (() => {
            const vh = window.innerHeight;
            const centerY = vh / 2;

            // Find the feed post element nearest to viewport center
            const posts = document.querySelectorAll(
                '.feed-shared-update-v2, .occludable-update, [data-urn*="activity"]'
            );

            let closest = null;
            let closestDist = Infinity;
            posts.forEach(post => {
                const rect = post.getBoundingClientRect();
                const postCenter = rect.top + rect.height / 2;
                const dist = Math.abs(postCenter - centerY);
                if (dist < closestDist && rect.top < vh && rect.bottom > 0) {
                    closest = post;
                    closestDist = dist;
                }
            });

            if (!closest) return 'none';

            // Detect post type
            const hasCarousel = !!(
                closest.querySelector('.feed-shared-carousel') ||
                closest.querySelector('[class*="carousel"]') ||
                closest.querySelector('.feed-shared-document')
            );
            const hasVideo = !!(
                closest.querySelector('video') ||
                closest.querySelector('.feed-shared-linkedin-video') ||
                closest.querySelector('[class*="video-player"]')
            );
            const hasImage = !!(
                closest.querySelector('.feed-shared-image') ||
                closest.querySelector('img.feed-shared-image__image')
            );

            // Check text length for long-form detection
            const textEl = closest.querySelector(
                '.feed-shared-text, .feed-shared-update-v2__description, .break-words'
            );
            const textLen = textEl ? textEl.innerText.length : 0;

            if (hasCarousel) return 'carousel';
            if (hasVideo) return 'video';
            if (textLen > 500) return 'long_form';
            if (hasImage) return 'image';
            if (textLen > 0) return 'short_text';
            return 'unknown';
        })()
    """)
    return result or "unknown"


def generate_scroll_stop_sequence(num_stops: int = 5) -> list[dict]:
    """Generate a unique scroll-stop sequence for this session.

    Returns a list of dicts like:
    [{"scrolls_before_pause": 3, "pause_type": "read"}, ...]
    """
    sequence = []
    for _ in range(num_stops):
        sequence.append(
            {
                "scrolls_before_pause": random.randint(1, 6),
                "pause_type": random.choice(["read", "skim", "linger"]),
            }
        )
    return sequence


def execute_feed_scroll(sim: HumanSimulator, scroll_stop_sequence: list[dict]) -> list[dict]:
    """Execute a feed scroll session following the generated sequence.

    Returns list of observed posts with their types.
    """
    observed_posts = []
    total_scrolls = 0

    for stop in scroll_stop_sequence:
        # Scroll the specified number of times before pausing
        for _ in range(stop["scrolls_before_pause"]):
            scroll_amount = random.randint(300, 600)
            sim.scroll(scroll_amount)
            human_delay(0.5, 1.5)
            total_scrolls += 1

        # Detect what post is in view and react appropriately
        post_type = detect_post_type(sim.cdp)
        observed_posts.append({"type": post_type, "scroll_position": total_scrolls})

        # Content-aware pause
        if post_type == "long_form":
            human_delay(8, 20)
            # Slow mid-read scrolls
            for _ in range(random.randint(1, 2)):
                sim.scroll(random.randint(100, 200))
                human_delay(2, 5)
        elif post_type == "carousel":
            # Scroll through slides
            for _ in range(random.randint(2, 3)):
                sim.scroll(random.randint(80, 150))
                human_delay(1.5, 4)
            # Sometimes skip the last slide
            if random.random() < 0.3:
                sim.scroll(random.randint(200, 400))
        elif post_type == "video":
            human_delay(3, 8)
            # Occasionally linger longer on video
            if random.random() < 0.2:
                human_delay(5, 15)
        elif post_type == "image":
            human_delay(3, 8)
        elif post_type == "short_text":
            human_delay(2, 5)
        else:
            human_delay(1, 3)

        # Between scroll groups
        human_delay(1, 4)

    return observed_posts
