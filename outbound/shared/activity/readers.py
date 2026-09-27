"""Activity readers — visible-entry extraction and the tab readers.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S13). Pure move.
"""

import json
import random
import re
import time
from typing import Any

from outbound.shared.activity.feed_state import (
    _activity_invalid_result,
    _activity_scroll_snapshot,
    _wait_for_activity_destination,
    _wait_for_activity_feed_state,
)
from outbound.shared.activity.navigation import (
    _open_activity_tab,
    _open_profile_activity_from_profile,
)
from outbound.shared.activity.tab_policy import (
    ACTIVITY_RANKING_TAB_ORDER,
    ACTIVITY_SELECTOR_RANKING_TAB_ORDER,
    ACTIVITY_TAB_ORDER,
)
from outbound.shared.activity.url_utils import (
    _activity_url_for_tab,
    _current_activity_url_is_disallowed,
    _page_still_loading,
    canonicalize_linkedin_profile_url,
)
from outbound.shared.browser.connection import CDPConnection
from outbound.shared.browser.readiness import _wait_for_linkedin_ready
from outbound.shared.human.delays import human_delay
from outbound.shared.human.simulator import HumanSimulator


def relative_days_from_time_text(time_text: str) -> int | None:
    """Convert LinkedIn relative time text into an approximate day count."""
    text = str(time_text or "").strip().lower()
    if not text:
        return None
    text = text.replace("ago", " ").replace("·", " ").replace("•", " ").strip()

    if "just now" in text or text == "now" or "today" in text:
        return 0
    if "yesterday" in text:
        return 1
    second_match = re.search(r"(\d+)\s*(s|sec|secs|second|seconds)\b", text)
    if second_match:
        return 0
    minute_match = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)\b", text)
    if minute_match:
        return 0
    hour_match = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours)\b", text)
    if hour_match:
        return 0

    day_match = re.search(r"(\d+)\s*(d|day|days)\b", text)
    if day_match:
        return int(day_match.group(1))
    week_match = re.search(r"(\d+)\s*(w|week|weeks)\b", text)
    if week_match:
        return int(week_match.group(1)) * 7
    month_match = re.search(r"(\d+)\s*(mo|month|months)\b", text)
    if month_match:
        return int(month_match.group(1)) * 30
    year_match = re.search(r"(\d+)\s*(y|yr|yrs|year|years)\b", text)
    if year_match:
        return int(year_match.group(1)) * 365
    return None


def classify_activity_windows(activities: list[dict[str, Any]]) -> dict[str, int]:
    """Summarize activity windows from parsed activity entries."""
    within_7d = 0
    within_30d = 0
    parseable = 0
    unparsed = 0
    for act in activities:
        days = relative_days_from_time_text(act.get("time_text", ""))
        if days is None:
            unparsed += 1
            continue
        parseable += 1
        if days <= 30:
            within_30d += 1
        if days <= 7:
            within_7d += 1
    return {
        "within_7d": within_7d,
        "within_30d": within_30d,
        "parseable": parseable,
        "unparsed": unparsed,
    }


def _extract_visible_activity_entries(
    cdp: CDPConnection,
    tab_key: str,
    timeout: float = 8.0,
    minimum_direct_comments: int = 2,
) -> dict[str, Any]:
    """Extract visible entries from the current activity tab."""
    raw = cdp.evaluate(
        f"""
        (() => {{
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const lower = (value) => normalize(value).toLowerCase();
            const visible = (el) => {{
                if (!el || el.nodeType !== 1) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            }};
            const ignoredActivityNode = (el) => {{
                if (!el || el.nodeType !== 1) return false;
                return !!el.closest(
                    '.msg-overlay-list-bubble, ' +
                    '.msg-overlay-conversation-bubble, ' +
                    '.msg-s-message-list, ' +
                    '[class*="msg-overlay"], ' +
                    '[class*="msg-s-message"]'
                );
            }};
            const extractRelativeTime = (value) => {{
                const text = lower(value);
                if (!text) return '';
                if (/^\\s*just now\\s*(?:[·•])?\\s*$/.test(text)) return 'just now';
                if (/^\\s*today\\s*(?:[·•])?\\s*$/.test(text)) return 'today';
                if (/^\\s*yesterday\\s*(?:[·•])?\\s*$/.test(text)) return 'yesterday';
                const match = text.match(/\\b\\d+\\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|yr|yrs|year|years)\\b(?:\\s+ago)?/);
                return match ? match[0] : '';
            }};
            const extractStrictRelativeTime = (value) => {{
                const text = lower(value);
                if (!text) return '';
                const strict = text.match(/^\\s*(just now|today|yesterday|\\d+\\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|yr|yrs|year|years)(?:\\s+ago)?)\\s*(?:[·•])?\\s*$/);
                return strict ? strict[1] : '';
            }};
            const roots = [];
            const collectRoots = (root) => {{
                roots.push(root);
                root.querySelectorAll?.('*').forEach((el) => {{
                    if (el.shadowRoot) collectRoots(el.shadowRoot);
                }});
            }};
            collectRoots(document);
            const queryAllRoots = (selector) => roots.flatMap((root) => Array.from(root.querySelectorAll?.(selector) || []));
            const profileName =
                (queryAllRoots('h1')[0]?.innerText || '').trim() ||
                ((document.title || '').match(/Activity \\| (.*?) \\| LinkedIn/i)?.[1] || '').trim();
            const strictSelector = '.feed-shared-update-v2[data-urn*="activity"]';
            const fallbackSelector = '.feed-shared-update-v2, [data-urn*="activity"]';
            const isAggregateShell = (cardText) => {{
                const sample = lower(cardText).slice(0, 120);
                return /^all activity\\s+posts\\s+comments\\s+documents\\s+images/.test(sample) ||
                    /^all activity\\s+posts\\s+comments\\s+reactions/.test(sample) ||
                    /^all activity\\s+posts\\s+comments/.test(sample);
            }};
            const isPlaceholderCard = (card, cardText) => {{
                const r = card.getBoundingClientRect();
                const sample = lower(cardText);
                if (extractRelativeTime(cardText)) return false;
                return r.height < 120 ||
                    sample.length < 30 ||
                    /^(show all posts|connect|follow|message|open to work)$/i.test(sample);
            }};

            const strictNodes = queryAllRoots(strictSelector)
                .filter((node) => visible(node) && !ignoredActivityNode(node));
            const fallbackNodes = strictNodes.length ? [] : queryAllRoots(fallbackSelector)
                .filter((node) => visible(node) && !ignoredActivityNode(node));
            const baseNodes = strictNodes.length
                ? strictNodes.map((node) => ({{card: node, timeText: '', mode: 'strict_feed_urn'}}))
                : fallbackNodes.map((node) => ({{card: node, timeText: '', mode: 'fallback_feed_update'}}));

            const seen = new Set();
            const items = baseNodes.filter((item) => {{
                const card = item.card;
                if (!visible(card)) return false;
                const cardText = normalize(card.innerText || card.textContent);
                if (!item.timeText && cardText.length < 20) return false;
                if (isAggregateShell(cardText) || isPlaceholderCard(card, cardText)) return false;
                const key = card.getAttribute('data-urn') || `${{item.mode}}:${{item.timeText}}:${{cardText.slice(0, 180)}}`;
                if (seen.has(key)) return false;
                seen.add(key);
                item.card = card;
                return true;
            }});

            const activities = [];
            // LinkedIn's current Comments UI uses comment entities and thread
            // entities.  The older `comments-comment-item` selector still
            // occurs in some variants, so retain it as a compatibility path.
            // Reply entries are collected separately and are used only when
            // the profile does not have enough direct comments to assess its
            // activity on their own.
            const commentEntitySelector = [
                '.comments-comment-entity',
                '.comments-thread-entity',
                '.comments-comment-item',
            ].join(', ');
            const commentReplySelector = [
                '.comments-comment-entity--reply',
                '.comment-social-activity--is-reply',
                '.comments-replies-list',
            ].join(', ');
            const commentActorSelector = [
                '.comments-comment-meta__description-title',
                '.comments-comment-meta__actor',
                '.comments-comment-meta__image-link',
            ].join(', ');
            items.forEach((item, i) => {{
                if (i >= 15) return;
                const card = item.card;
                if (ignoredActivityNode(card)) return;
                const cardText = normalize(card.innerText || card.textContent);
                let commentKind = 'comment';
                if ({json.dumps(tab_key)} === 'comments' && profileName) {{
                    const profileCommentMarker = profileName.toLowerCase() + ' commented on this';
                    const cardLooksLikeProfileComment = cardText.toLowerCase().includes(profileCommentMarker);
                    const entityNodes = [card, ...Array.from(card.querySelectorAll(commentEntitySelector))]
                        .filter((node, index, list) => list.indexOf(node) === index);
                    const ownerCommentTimes = entityNodes
                        .map((entity) => {{
                            const actorText = normalize(
                                entity.querySelector(commentActorSelector)?.innerText ||
                                entity.querySelector(commentActorSelector)?.textContent ||
                                ''
                            );
                            const entityText = normalize(entity.innerText || entity.textContent);
                            const ownsComment = actorText.toLowerCase().includes(profileName.toLowerCase()) ||
                                entityText.toLowerCase().includes(profileName.toLowerCase());
                            const timeEl = entity.querySelector('.comments-comment-meta__data, time.comments-comment-meta__data, time');
                            const time = extractStrictRelativeTime(
                                timeEl?.innerText || timeEl?.textContent || timeEl?.getAttribute('datetime') || ''
                            );
                            return {{
                                time,
                                ownsComment,
                                isReply: entity.matches(commentReplySelector) || !!entity.closest(commentReplySelector),
                                sample: entityText,
                            }};
                        }})
                        .filter((entry) => entry.time && entry.ownsComment);
                    if (ownerCommentTimes.length === 0 && !cardLooksLikeProfileComment) return;
                    item.timeText = item.timeText || (ownerCommentTimes[0] || {{}}).time || '';
                    commentKind = ownerCommentTimes.some((entry) => entry.isReply) ||
                        card.matches(commentReplySelector) || !!card.querySelector(commentReplySelector)
                        ? 'reply'
                        : 'comment';
                }}

                const textEl = card.querySelector(
                    '.feed-shared-text, .break-words, .update-components-text'
                );
                const text = textEl ? textEl.innerText.substring(0, 200) : '';

                const timeEl = card.querySelector(
                    '.update-components-actor__sub-description, ' +
                    '.feed-shared-actor__sub-description, ' +
                    'time, [aria-label*="ago"]'
                );
                const rawTime = timeEl ? timeEl.innerText.trim() : '';
                const ariaTime = Array.from(card.querySelectorAll('[aria-label]'))
                    .map((el) => el.getAttribute('aria-label') || '')
                    .find((value) => extractStrictRelativeTime(value) || extractRelativeTime(value)) || '';
                const strictNodeTime = Array.from(card.querySelectorAll('span, time, div'))
                    .map((el) => el.innerText || el.textContent || '')
                    .find((value) => extractStrictRelativeTime(value)) || '';
                const timeText =
                    item.timeText ||
                    extractStrictRelativeTime(rawTime) ||
                    extractStrictRelativeTime(ariaTime) ||
                    extractStrictRelativeTime(strictNodeTime) ||
                    extractRelativeTime(rawTime) ||
                    extractRelativeTime(ariaTime);

                const likesEl = card.querySelector(
                    '[class*="social-counts"] span, ' +
                    '.social-details-social-counts__reactions-count, ' +
                    'button[aria-label*="reaction"]'
                );
                const likes = likesEl ? likesEl.innerText : '';

                const commentsEl = card.querySelector(
                    '[class*="comments-count"], button[aria-label*="comment"]'
                );
                const comments = commentsEl ? commentsEl.innerText : '';

                const urn = card.getAttribute('data-urn') || '';
                const postUrl = urn ? 'https://www.linkedin.com/feed/update/' + urn + '/' : '';

                activities.push({{
                    type: {json.dumps(tab_key)},
                    text: text.trim(),
                    time_text: timeText.trim(),
                    likes: likes.trim(),
                    comments: comments.trim(),
                    raw_time_text: rawTime.trim(),
                    card_text_sample: cardText.substring(0, 300).trim(),
                    extractor_mode: item.mode,
                    post_url: postUrl,
                    index: i,
                    comment_kind: {json.dumps(tab_key)} === 'comments' ? commentKind : '',
                }});
            }});

            let returnedActivities = activities;
            let commentReplyFallback = {{used: false, direct_comments: 0, reply_comments: 0, minimum_direct_comments: {int(minimum_direct_comments)}}};
            if ({json.dumps(tab_key)} === 'comments') {{
                const directComments = activities.filter((activity) => activity.comment_kind !== 'reply');
                const replyComments = activities.filter((activity) => activity.comment_kind === 'reply');
                commentReplyFallback = {{
                    used: directComments.length < {int(minimum_direct_comments)} && replyComments.length > 0,
                    direct_comments: directComments.length,
                    reply_comments: replyComments.length,
                    minimum_direct_comments: {int(minimum_direct_comments)},
                }};
                // Replies supplement a sparse comment history. When two or
                // more direct comments are available, they remain excluded so
                // replies cannot inflate the activity score.
                returnedActivities = commentReplyFallback.used
                    ? activities
                    : directComments;
            }}

            return JSON.stringify({{
                total_visible: items.length,
                activities: returnedActivities,
                comment_reply_fallback: commentReplyFallback,
                profile_name: profileName,
                extractor_mode: strictNodes.length ? 'strict_feed_urn' : 'fallback_feed_update',
                strict_visible: strictNodes.length,
                fallback_visible: fallbackNodes.length,
                shadow_roots_scanned: roots.length - 1,
            }});
        }})()
    """,
        timeout=timeout,
    )
    return json.loads(raw) if raw else {"activities": [], "total_visible": 0, "profile_name": ""}


def _scroll_activity_with_lazy_patience(
    cdp: CDPConnection,
    sim: HumanSimulator,
    max_seconds: float,
    max_distance: int = 1800,
) -> dict[str, Any]:
    """Activity-specific scroll: patient for lazy-load, finite at real page end."""
    started = time.time()
    deadline = started + max(0.5, max_seconds)
    distance = 0
    no_progress = 0
    last = _activity_scroll_snapshot(cdp)
    best_card_count = int(last.get("cardCount", 0) or 0)

    while time.time() < deadline and distance < max_distance:
        viewport = int(last.get("viewportHeight", 0) or 900)
        scroll_y = int(last.get("scrollY", 0) or 0)
        scroll_height = int(last.get("scrollHeight", 0) or 0)
        near_bottom = scroll_y + viewport >= max(0, scroll_height - 90)

        if last.get("emptyState") and not last.get("loading") and best_card_count == 0:
            return {
                "stopped": "empty_state",
                "elapsed_sec": round(time.time() - started, 2),
                "distance": distance,
                "card_count": best_card_count,
            }
        if near_bottom and no_progress >= 3 and not last.get("loading"):
            return {
                "stopped": "bottom_stable",
                "elapsed_sec": round(time.time() - started, 2),
                "distance": distance,
                "card_count": best_card_count,
            }

        chunk = min(random.randint(320, 680), max_distance - distance)
        before = last
        try:
            sim.scroll(chunk)
        except TimeoutError:
            cdp.evaluate(f"window.scrollBy(0, {int(chunk)})", timeout=8)
        distance += chunk
        human_delay(0.55, 1.2)
        if before.get("loading"):
            human_delay(0.7, 1.6)
        last = _activity_scroll_snapshot(cdp)

        moved = abs(int(last.get("scrollY", 0) or 0) - int(before.get("scrollY", 0) or 0)) > 20
        card_count = int(last.get("cardCount", 0) or 0)
        new_cards = card_count > best_card_count
        if new_cards:
            best_card_count = card_count
        if moved or new_cards or last.get("loading"):
            no_progress = 0
        else:
            no_progress += 1

    return {
        "stopped": "budget_or_distance",
        "elapsed_sec": round(time.time() - started, 2),
        "distance": distance,
        "card_count": best_card_count,
    }


def read_activity_tab(
    cdp: CDPConnection,
    sim: HumanSimulator,
    profile_url: str,
    max_seconds: float = 45.0,
) -> dict[str, Any]:
    """Navigate to a profile's activity endpoints and read all/comments/reactions."""
    profile_base = canonicalize_linkedin_profile_url(profile_url)
    if not profile_base:
        return {
            "error": True,
            "danger": "invalid_profile_url",
            "reason": "invalid_profile_url",
            "original_profile_url": str(profile_url or ""),
        }
    started_at = time.time()
    deadline = started_at + max_seconds
    grace_used = 0
    MAX_GRACE_PER_TAB = 3
    GRACE_SECONDS = 15.0

    def maybe_grace() -> bool:
        """..."""
        nonlocal deadline, grace_used
        if grace_used >= MAX_GRACE_PER_TAB:
            return False
        if _page_still_loading(cdp):
            deadline += GRACE_SECONDS
            grace_used += 1
            return True
        return False

    tabs_checked: list[dict[str, Any]] = []

    tabs_checked: list[dict[str, Any]] = []
    best_result: dict[str, Any] | None = None

    def elapsed() -> float:
        return time.time() - started_at

    def remaining() -> float:
        return max(0.0, deadline - time.time())

    def timeout_result(tab_key: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "error": True,
            "danger": "activity_read_timeout",
            "source_tab": tab_key,
            "reason": reason,
            "elapsed_sec": round(elapsed(), 2),
            "timeout_sec": max_seconds,
            "tabs_checked": tabs_checked,
            **extra,
        }

    def bounded_pause(min_s: float, max_s: float) -> bool:
        budget = remaining()
        if budget <= 0:
            return False
        time.sleep(min(random.uniform(min_s, max_s), budget))
        return remaining() > 0

    for tab_key, tab_label in ACTIVITY_TAB_ORDER:
        if remaining() < 4:
            if not maybe_grace():
                return timeout_result(tab_key, "insufficient_budget_before_tab")
        activity_url = _activity_url_for_tab(profile_base, tab_key)
        try:
            cdp.navigate(activity_url, wait_load=False, timeout=min(8, max(2, remaining())))
        except (TimeoutError, RuntimeError) as exc:
            return timeout_result(tab_key, "navigation_failed_or_timed_out", error_detail=str(exc))
        navigation_state = _wait_for_activity_destination(
            cdp,
            profile_base,
            tab_key,
            timeout=min(8, max(2, remaining())),
        )
        if navigation_state.get("invalid"):
            return {
                **navigation_state["invalid"],
                "source_tab": tab_key,
                "tabs_checked": tabs_checked,
                "navigation_state": navigation_state,
            }
        if not navigation_state.get("arrived"):
            return timeout_result(
                tab_key, "activity_page_not_ready", navigation_state=navigation_state
            )
        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector="main, .scaffold-layout, .profile-creator-shared-feed-update__container, .feed-shared-update-v2, .artdeco-card",
            timeout=min(12, max(3, remaining())),
            # Activity feeds keep hydrating/lazy-loading even after useful content is visible.
            # Selector readiness plus the bounded pause/scroll below is a better signal here.
            stable_for=0.0,
            ignored_overlays={".authentication-outlet"},
            ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
        )
        if not ready_state.get("ready"):
            return timeout_result(tab_key, "activity_page_not_ready", load_state=ready_state)
        if not bounded_pause(0.5, 1.25):
            return timeout_result(tab_key, "timeout_after_ready")

        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {**invalid, "source_tab": tab_key, "tabs_checked": tabs_checked}
        if remaining() < 3:
            if not maybe_grace():
                return timeout_result(tab_key, "insufficient_budget_before_feed_state")
        feed_state = _wait_for_activity_feed_state(cdp, timeout=min(12, max(3, remaining())))
        if feed_state.get("invalid"):
            return {
                **feed_state["invalid"],
                "source_tab": tab_key,
                "tabs_checked": tabs_checked,
                "feed_state": feed_state,
            }
        if not feed_state.get("ready"):
            return timeout_result(tab_key, "activity_feed_not_hydrated", feed_state=feed_state)

        open_result = {
            "found": True,
            "clicked": False,
            "via": "direct_activity_url",
            "url": activity_url,
        }

        if not bounded_pause(0.5, 1.5):
            if not maybe_grace():
                return timeout_result(tab_key, "timeout_before_scroll")
        scroll_budget = max(1.0, min(random.uniform(3, 6), remaining() - 2))
        if scroll_budget <= 0:
            if not maybe_grace():
                return timeout_result(tab_key, "insufficient_budget_before_scroll")
        scroll_result = _scroll_activity_with_lazy_patience(
            cdp,
            sim,
            max_seconds=scroll_budget,
            max_distance=1800,
        )
        if not bounded_pause(0.5, 1.5):
            return timeout_result(tab_key, "timeout_before_extract")

        try:
            tab_result = _extract_visible_activity_entries(
                cdp,
                tab_key,
                timeout=min(8, max(3, remaining())),
            )
        except (TimeoutError, RuntimeError) as exc:
            return timeout_result(
                tab_key, "activity_extract_failed_or_timed_out", error_detail=str(exc)
            )
        window_counts = classify_activity_windows(tab_result.get("activities", []))
        recent_items = []
        for act in tab_result.get("activities", []):
            days = relative_days_from_time_text(act.get("time_text", ""))
            if days is not None and days <= 7:
                recent_items.append(act)

        tab_summary = {
            "tab": tab_key,
            "label": tab_label,
            "found": True,
            "tab_elapsed_sec": round(elapsed(), 2),
            "grace_used": grace_used,
            "url": activity_url,
            "total_visible": tab_result.get("total_visible", 0),
            "within_7d": window_counts.get("within_7d", 0),
            "within_30d": window_counts.get("within_30d", 0),
            "parseable": window_counts.get("parseable", 0),
            "unparsed": window_counts.get("unparsed", 0),
            "open_result": open_result,
            "scroll_result": scroll_result,
            "feed_state": feed_state,
        }
        tabs_checked.append(tab_summary)

        total_visible = int(tab_result.get("total_visible", 0) or 0)
        parseable = int(window_counts.get("parseable", 0) or 0)
        candidate = {
            **tab_result,
            "is_active": window_counts.get("within_7d", 0) > 0,
            "recent_items": recent_items,
            "activity_window_counts": window_counts,
            "activity_classification_uncertain": total_visible > 0 and parseable == 0,
            "source_tab": tab_key,
            "source_tab_label": tab_label,
            "tabs_checked": list(tabs_checked),
            "elapsed_sec": round(elapsed(), 2),
            "timeout_sec": max_seconds,
        }

        if best_result is None and tab_result.get("activities"):
            best_result = candidate

        if window_counts.get("within_30d", 0) > 0:
            return candidate

    if best_result is None:
        best_result = {
            "activities": [],
            "total_visible": 0,
            "profile_name": "",
            "is_active": False,
            "recent_items": [],
            "activity_window_counts": {"within_7d": 0, "within_30d": 0},
            "activity_classification_uncertain": False,
            "source_tab": "",
            "source_tab_label": "",
        }

    best_result["tabs_checked"] = tabs_checked
    best_result["elapsed_sec"] = round(elapsed(), 2)
    best_result["timeout_sec"] = max_seconds
    return best_result


def read_activity_tabs_detail(
    cdp: CDPConnection,
    sim: HumanSimulator,
    profile_url: str,
    max_seconds: float = 45.0,
    navigation_type: str = "direct_url",
    tab_order: list[tuple[str, str]] | None = None,
    minimum_direct_comments: int = 2,
    disable_early_stop: bool = False,
) -> dict[str, Any]:
    """Read posts/comments/reactions separately for contact ranking."""
    profile_base = canonicalize_linkedin_profile_url(profile_url)
    if not profile_base:
        return {
            "error": True,
            "danger": "invalid_profile_url",
            "reason": "invalid_profile_url",
            "original_profile_url": str(profile_url or ""),
        }
    started_at = time.time()
    deadline = started_at + max_seconds
    tabs: dict[str, Any] = {}
    navigation_type = (
        (navigation_type or "direct_url").strip().lower().replace("-", "_").replace(" ", "_")
    )
    selector_based = navigation_type in {
        "selector_based",
        "selector",
        "click_through",
        "clickthrough",
    }
    profile_activity_open_result: dict[str, Any] = {}

    def elapsed() -> float:
        return time.time() - started_at

    def remaining() -> float:
        return max(0.0, deadline - time.time())

    def timeout_result(tab_key: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "error": True,
            "danger": "activity_read_timeout",
            "source_tab": tab_key,
            "reason": reason,
            "elapsed_sec": round(elapsed(), 2),
            "timeout_sec": max_seconds,
            "tabs": tabs,
            **extra,
        }

    def bounded_pause(min_s: float, max_s: float) -> bool:
        budget = remaining()
        if budget <= 0:
            return False
        time.sleep(min(random.uniform(min_s, max_s), budget))
        return remaining() > 0

    def partial_activity_level() -> str:
        def count_within(tab_key: str, days_limit: int) -> int:
            count = 0
            for activity in tabs.get(tab_key, {}).get("activities", []) or []:
                days = relative_days_from_time_text(activity.get("time_text", ""))
                if days is not None and days <= days_limit:
                    count += 1
            return count

        if count_within("posts", 7) >= 1:
            return "Very active"
        if count_within("comments", 7) >= 2:
            return "Very active"
        if count_within("reactions", 7) >= 2:
            return "Very active"
        if count_within("posts", 14) >= 1:
            return "Active"
        if count_within("comments", 30) + count_within("reactions", 30) >= 5:
            return "Active"
        if any(tab.get("activity_classification_uncertain") for tab in tabs.values()):
            return ""
        return ""

    def report_navigation(method, action, reason=""):
        callback = getattr(cdp, "execution_observer", None)
        if callable(callback):
            callback(method=method, action=action, reason=reason)

    if selector_based:
        try:
            report_navigation("Direct URL", "Opening profile for assessment")
            cdp.navigate(profile_base + "/", wait_load=False, timeout=min(8, max(2, remaining())))
        except (TimeoutError, RuntimeError) as exc:
            profile_activity_open_result = {
                "found": False,
                "clicked": False,
                "via": "profile_page",
                "error": str(exc),
            }
        else:
            ready_state = _wait_for_linkedin_ready(
                cdp,
                expected_selector='main, .scaffold-layout, a[href*="/recent-activity/all/"], .artdeco-card',
                timeout=min(12, max(3, remaining())),
                stable_for=0.0,
                ignored_overlays={".authentication-outlet"},
                ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
            )
            invalid = _activity_invalid_result(cdp)
            if invalid:
                return {**invalid, "source_tab": "profile", "tabs": tabs}
            if ready_state.get("ready") and bounded_pause(0.5, 1.25):
                try:
                    report_navigation("DOM", "Opening profile activity")
                    profile_activity_open_result = _open_profile_activity_from_profile(
                        cdp, timeout=min(25, max(0, remaining() - 4))
                    )
                    if profile_activity_open_result.get("clicked"):
                        bounded_pause(0.75, 1.5)
                        _wait_for_linkedin_ready(
                            cdp,
                            expected_selector='main, .scaffold-layout, button, [role="tab"], .profile-creator-shared-feed-update__container, .feed-shared-update-v2, .artdeco-card',
                            timeout=min(10, max(3, remaining())),
                            stable_for=0.0,
                            ignored_overlays={".authentication-outlet"},
                            ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
                        )
                except (TimeoutError, RuntimeError) as exc:
                    profile_activity_open_result = {
                        "found": False,
                        "clicked": False,
                        "via": "show_all_posts",
                        "error": str(exc),
                    }
            else:
                profile_activity_open_result = {
                    "found": False,
                    "clicked": False,
                    "via": "profile_page",
                    "load_state": ready_state,
                }

    selected_tab_order = tab_order or (
        ACTIVITY_SELECTOR_RANKING_TAB_ORDER if selector_based else ACTIVITY_RANKING_TAB_ORDER
    )
    for tab_key, tab_label in selected_tab_order:
        if remaining() < 4:
            return timeout_result(tab_key, "insufficient_budget_before_tab")
        activity_url = _activity_url_for_tab(profile_base, tab_key)
        open_result: dict[str, Any]
        if selector_based and profile_activity_open_result.get("clicked"):
            try:
                report_navigation("DOM", f"Opening {tab_label}")
                open_result = _open_activity_tab(cdp, tab_label)
            except (TimeoutError, RuntimeError) as exc:
                open_result = {
                    "found": False,
                    "clicked": False,
                    "via": "selector_error",
                    "error": str(exc),
                }
            if open_result.get("found") and _current_activity_url_is_disallowed(cdp):
                open_result = {**open_result, "disallowed_url_after_selector": True}
            if not open_result.get("found") or open_result.get("disallowed_url_after_selector"):
                try:
                    report_navigation(
                        "Direct URL · fallback",
                        f"Opening {tab_label}",
                        open_result.get("error")
                        or "Activity tab control missing or reached an unsupported destination",
                    )
                    cdp.navigate(activity_url, wait_load=False, timeout=min(8, max(2, remaining())))
                except (TimeoutError, RuntimeError) as exc:
                    return timeout_result(
                        tab_key,
                        "navigation_failed_or_timed_out",
                        error_detail=str(exc),
                        open_result=open_result,
                    )
                navigation_state = _wait_for_activity_destination(
                    cdp,
                    profile_base,
                    tab_key,
                    timeout=min(8, max(2, remaining())),
                )
                if navigation_state.get("invalid"):
                    return {
                        **navigation_state["invalid"],
                        "source_tab": tab_key,
                        "tabs": tabs,
                        "navigation_state": navigation_state,
                    }
                if not navigation_state.get("arrived"):
                    return timeout_result(
                        tab_key,
                        "activity_page_not_ready",
                        navigation_state=navigation_state,
                        open_result=open_result,
                    )
                open_result = {
                    **open_result,
                    "fallback_via": "direct_activity_url",
                    "url": activity_url,
                }
            else:
                bounded_pause(0.75, 1.5)
                navigation_state = _wait_for_activity_destination(
                    cdp,
                    profile_base,
                    tab_key,
                    timeout=min(6, max(2, remaining())),
                )
                if navigation_state.get("invalid"):
                    return {
                        **navigation_state["invalid"],
                        "source_tab": tab_key,
                        "tabs": tabs,
                        "navigation_state": navigation_state,
                    }
                if not navigation_state.get("arrived"):
                    try:
                        report_navigation(
                            "Direct URL · fallback",
                            f"Opening {tab_label}",
                            "DOM navigation did not reach the requested activity tab",
                        )
                        cdp.navigate(
                            activity_url, wait_load=False, timeout=min(8, max(2, remaining()))
                        )
                    except (TimeoutError, RuntimeError) as exc:
                        return timeout_result(
                            tab_key,
                            "navigation_failed_or_timed_out",
                            error_detail=str(exc),
                            open_result=open_result,
                            navigation_state=navigation_state,
                        )
                    navigation_state = _wait_for_activity_destination(
                        cdp,
                        profile_base,
                        tab_key,
                        timeout=min(8, max(2, remaining())),
                    )
                    if navigation_state.get("invalid"):
                        return {
                            **navigation_state["invalid"],
                            "source_tab": tab_key,
                            "tabs": tabs,
                            "navigation_state": navigation_state,
                        }
                    if not navigation_state.get("arrived"):
                        return timeout_result(
                            tab_key,
                            "activity_page_not_ready",
                            navigation_state=navigation_state,
                            open_result=open_result,
                        )
                    open_result = {
                        **open_result,
                        "fallback_via": "direct_activity_url",
                        "url": activity_url,
                    }
        else:
            try:
                report_navigation(
                    "Direct URL · fallback" if selector_based else "Direct URL",
                    f"Opening {tab_label}",
                    (
                        profile_activity_open_result.get("error")
                        or "Profile activity could not be opened through DOM"
                    )
                    if selector_based
                    else "",
                )
                cdp.navigate(activity_url, wait_load=False, timeout=min(8, max(2, remaining())))
            except (TimeoutError, RuntimeError) as exc:
                return timeout_result(
                    tab_key,
                    "navigation_failed_or_timed_out",
                    error_detail=str(exc),
                    profile_activity_open_result=profile_activity_open_result,
                )
            navigation_state = _wait_for_activity_destination(
                cdp,
                profile_base,
                tab_key,
                timeout=min(8, max(2, remaining())),
            )
            if navigation_state.get("invalid"):
                return {
                    **navigation_state["invalid"],
                    "source_tab": tab_key,
                    "tabs": tabs,
                    "navigation_state": navigation_state,
                }
            if not navigation_state.get("arrived"):
                return timeout_result(
                    tab_key,
                    "activity_page_not_ready",
                    navigation_state=navigation_state,
                    profile_activity_open_result=profile_activity_open_result,
                )
            open_result = {
                "found": True,
                "clicked": False,
                "via": "direct_activity_url",
                "url": activity_url,
            }

        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector="main, .scaffold-layout, .profile-creator-shared-feed-update__container, .feed-shared-update-v2, .artdeco-card",
            timeout=min(12, max(3, remaining())),
            stable_for=0.0,
            ignored_overlays={".authentication-outlet"},
            ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
        )
        early_feed_state: dict[str, Any] | None = None
        if not ready_state.get("ready"):
            # On slow connections LinkedIn can keep the document/skeleton state
            # "loading" after the useful activity feed has started hydrating.
            # Wait for the feed-specific signal before treating the page as failed.
            early_feed_state = _wait_for_activity_feed_state(
                cdp,
                timeout=min(45, max(15, remaining())),
                poll_interval=1.0,
            )
            if not early_feed_state.get("ready"):
                return timeout_result(
                    tab_key,
                    "activity_page_not_ready",
                    load_state=ready_state,
                    feed_state=early_feed_state,
                )
        if not bounded_pause(0.5, 1.25):
            return timeout_result(tab_key, "timeout_after_ready")

        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {**invalid, "source_tab": tab_key, "tabs": tabs}
        feed_state = (
            early_feed_state
            if early_feed_state and early_feed_state.get("ready")
            else _wait_for_activity_feed_state(cdp, timeout=min(30, max(8, remaining())))
        )
        if feed_state.get("invalid"):
            return {
                **feed_state["invalid"],
                "source_tab": tab_key,
                "tabs": tabs,
                "feed_state": feed_state,
            }
        if not feed_state.get("ready"):
            return timeout_result(tab_key, "activity_feed_not_hydrated", feed_state=feed_state)

        # An explicit LinkedIn empty state is a finished tab, not a loading
        # failure. Continue to Reactions and Comments without spending the
        # remaining profile budget scrolling an empty feed.
        if str(feed_state.get("reason") or "") == "explicit_empty_state":
            tabs[tab_key] = {
                "activities": [],
                "total_visible": 0,
                "profile_name": "",
                "label": tab_label,
                "url": activity_url,
                "navigation_type": "selector_based" if selector_based else "direct_url",
                "open_result": open_result,
                "profile_activity_open_result": profile_activity_open_result,
                "scroll_result": {"stopped": "explicit_empty_state", "elapsed_sec": 0},
                "feed_state": feed_state,
                "activity_window_counts": {
                    "within_7d": 0,
                    "within_30d": 0,
                    "parseable": 0,
                    "unparsed": 0,
                },
                "activity_classification_uncertain": False,
            }
            continue

        if not bounded_pause(0.5, 1.5):
            return timeout_result(tab_key, "timeout_before_scroll")
        scroll_budget = max(1.0, min(random.uniform(2, 4), remaining() - 2))
        if scroll_budget <= 0:
            return timeout_result(tab_key, "insufficient_budget_before_scroll")
        scroll_result = _scroll_activity_with_lazy_patience(
            cdp,
            sim,
            max_seconds=scroll_budget,
            max_distance=1400,
        )
        if not bounded_pause(0.5, 1.5):
            return timeout_result(tab_key, "timeout_before_extract")

        try:
            tab_result = _extract_visible_activity_entries(
                cdp,
                tab_key,
                timeout=min(8, max(3, remaining())),
                minimum_direct_comments=minimum_direct_comments,
            )
        except (TimeoutError, RuntimeError) as exc:
            return timeout_result(
                tab_key, "activity_extract_failed_or_timed_out", error_detail=str(exc)
            )

        window_counts = classify_activity_windows(tab_result.get("activities", []))
        total_visible = int(tab_result.get("total_visible", 0) or 0)
        parseable = int(window_counts.get("parseable", 0) or 0)
        tabs[tab_key] = {
            **tab_result,
            "label": tab_label,
            "url": activity_url,
            "navigation_type": "selector_based" if selector_based else "direct_url",
            "open_result": open_result,
            "profile_activity_open_result": profile_activity_open_result,
            "scroll_result": scroll_result,
            "feed_state": feed_state,
            "activity_window_counts": window_counts,
            "activity_classification_uncertain": total_visible > 0 and parseable == 0,
        }
        level = partial_activity_level()
        if level == "Very active" and not disable_early_stop:
            return {
                "error": False,
                "profile_url": profile_url,
                "navigation_type": "selector_based" if selector_based else "direct_url",
                "tabs": tabs,
                "early_stop": True,
                "early_stop_level": level,
                "elapsed_sec": round(elapsed(), 2),
                "timeout_sec": max_seconds,
            }

    if tabs and not any(int((tab or {}).get("total_visible", 0) or 0) > 0 for tab in tabs.values()):
        explicit_empty_tabs = [
            key
            for key, tab in tabs.items()
            if str(((tab or {}).get("feed_state") or {}).get("reason") or "")
            == "explicit_empty_state"
        ]
        if len(explicit_empty_tabs) < len(tabs):
            return {
                "error": True,
                "danger": "activity_read_timeout",
                "reason": "activity_feed_not_hydrated",
                "deferred_empty_extract": True,
                "profile_url": profile_url,
                "navigation_type": "selector_based" if selector_based else "direct_url",
                "tabs": tabs,
                "early_stop": False,
                "elapsed_sec": round(elapsed(), 2),
                "timeout_sec": max_seconds,
            }

    return {
        "error": False,
        "profile_url": profile_url,
        "navigation_type": "selector_based" if selector_based else "direct_url",
        "tabs": tabs,
        "early_stop": False,
        "elapsed_sec": round(elapsed(), 2),
        "timeout_sec": max_seconds,
    }
