"""Element visibility helpers (honeypot guard) for CDP pages.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S5). Pure move.
"""

import json

from outbound.shared.browser.connection import CDPConnection


def is_element_visible(cdp: CDPConnection, selector: str) -> bool:
    """Check if an element is genuinely visible (not a honeypot)."""
    result = cdp.evaluate(f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return (
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0' &&
                rect.width > 0 &&
                rect.height > 0 &&
                rect.top < window.innerHeight &&
                rect.bottom > 0
            );
        }})()
    """)
    return bool(result)


def get_visible_elements(cdp: CDPConnection, selector: str) -> list[dict]:
    """Get all visible elements matching selector with their positions."""
    result = cdp.evaluate(f"""
        (() => {{
            const els = document.querySelectorAll({json.dumps(selector)});
            const visible = [];
            els.forEach((el, i) => {{
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    rect.width > 0 &&
                    rect.height > 0 &&
                    rect.top < window.innerHeight + 200 &&
                    rect.bottom > -200
                ) {{
                    visible.push({{
                        index: i,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                        width: rect.width,
                        height: rect.height,
                        text: el.innerText ? el.innerText.substring(0, 200) : '',
                    }});
                }}
            }});
            return JSON.stringify(visible);
        }})()
    """)
    return json.loads(result) if result else []
