"""Danger detection — captcha / restriction / login / invalid-profile pages.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S6). Pure move.
"""

import json
from typing import Any

from outbound.shared.browser.connection import CDPConnection


class PageType:
    FEED = "feed"
    PROFILE = "profile"
    ACTIVITY = "activity"
    SEARCH = "search"
    MESSAGING = "messaging"
    NOTIFICATIONS = "notifications"
    MY_NETWORK = "my_network"
    CAPTCHA = "captcha"
    RESTRICTION = "restriction"
    LOGIN = "login"
    UNKNOWN = "unknown"


def detect_page(cdp: CDPConnection, timeout: float = 30) -> dict[str, Any]:
    """Detect the current LinkedIn page type and extract key info."""
    result = cdp.evaluate(
        """
        (() => {
            const url = window.location.href;
            const title = document.title || '';
            const body = document.body ? document.body.innerText.substring(0, 500) : '';

            // Circuit breaker checks first
            // LinkedIn embeds captcha-related elements on normal pages for tracking.
            // Only flag a real CAPTCHA challenge: URL-based or a VISIBLE large iframe.
            const isLoginPage = url.includes('/login') || url.includes('/uas/login') || url.includes('/checkpoint');
            const isCaptchaUrl = url.includes('/captcha') || url.includes('/challenge');

            // A real captcha iframe will be large and visible (>100px), not a 1px tracking pixel
            const captchaIframes = Array.from(document.querySelectorAll('iframe'));
            const hasVisibleCaptchaIframe = captchaIframes.some(f => {
                const rect = f.getBoundingClientRect();
                return (f.src || '').includes('captcha') && rect.width > 100 && rect.height > 100;
            });

            const hasCaptcha = isCaptchaUrl || hasVisibleCaptchaIframe || !!(
                document.querySelector('#captcha') ||
                body.includes('verify you are a human') ||
                body.includes('complete the security check')
            );

            const hasRestriction = !!(
                body.includes('Your account has been restricted') ||
                body.includes('account is temporarily restricted') ||
                body.includes('we\\'ve restricted your account') ||
                document.querySelector('[class*="restriction"]')
            );

            const hasLoginPrompt = !!(
                url.includes('/login') ||
                url.includes('/checkpoint') ||
                document.querySelector('#session_key') ||
                (body.includes('Sign in') && !url.includes('/feed'))
            );

            const hasEmailVerify = !!(
                body.includes('verify your email') ||
                body.includes('confirm your email') ||
                url.includes('/check/email')
            );

            const hasRobotCheck = !!(
                body.includes('Are you a robot') ||
                body.includes("Let's do a quick security check")
            );

            const bodyLower = body.toLowerCase();
            const titleLower = title.toLowerCase();
            const hasNotFound = !!(
                url.includes('/404') ||
                titleLower.includes('page not found') ||
                titleLower.includes('not found') ||
                bodyLower.includes('page not found') ||
                bodyLower.includes("this page doesn't exist") ||
                bodyLower.includes("this page does not exist") ||
                bodyLower.includes('profile not found') ||
                bodyLower.includes("this profile isn't available") ||
                bodyLower.includes('this profile is not available')
            );

            let pageType = 'unknown';
            if (hasCaptcha) pageType = 'captcha';
            else if (hasRestriction) pageType = 'restriction';
            else if (hasLoginPrompt) pageType = 'login';
            else if (hasEmailVerify) pageType = 'email_verify';
            else if (hasRobotCheck) pageType = 'robot_check';
            else if (hasNotFound) pageType = 'not_found';
            else if (url.includes('/feed')) pageType = 'feed';
            else if (url.match(/\\/in\\/[^/]+\\/recent-activity/)) pageType = 'activity';
            else if (url.match(/\\/in\\/[^/]+/)) pageType = 'profile';
            else if (url.includes('/search/')) pageType = 'search';
            else if (url.includes('/messaging')) pageType = 'messaging';
            else if (url.includes('/notifications')) pageType = 'notifications';
            else if (url.includes('/mynetwork')) pageType = 'my_network';

            return JSON.stringify({
                page_type: pageType,
                url: url,
                title: title,
                is_danger: hasCaptcha || hasRestriction || hasLoginPrompt || hasEmailVerify || hasRobotCheck || hasNotFound,
                danger_type: hasCaptcha ? 'captcha' :
                             hasRestriction ? 'restriction' :
                             hasEmailVerify ? 'email_verify' :
                             hasRobotCheck ? 'robot_check' :
                             hasNotFound ? 'invalid_profile_or_404' :
                             hasLoginPrompt ? 'login' : null,
            });
        })()
    """,
        timeout=timeout,
    )
    return json.loads(result) if result else {"page_type": "unknown", "is_danger": False}


def check_circuit_breakers(cdp: CDPConnection) -> str | None:
    """Check for danger signals. Returns danger type string or None if safe."""
    page = detect_page(cdp)
    if page.get("is_danger"):
        return page.get("danger_type", "unknown_danger")
    return None
