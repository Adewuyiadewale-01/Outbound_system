"""Stealth patches — mask automation indicators before automation runs.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S4). Pure move.
"""

from outbound.shared.browser.connection import CDPConnection

STEALTH_SCRIPTS = [
    # Remove navigator.webdriver flag
    """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });
    """,
    # Mask CDP detection via Runtime.enable side effects
    """
    // Prevent detection of CDP via window.cdc_adoQpoasnfa76pfcZLmcfl_Array etc.
    const origKeys = Object.keys;
    Object.keys = function(obj) {
        const keys = origKeys.call(this, obj);
        if (obj === window) {
            return keys.filter(k => !k.match(/^cdc_/));
        }
        return keys;
    };
    """,
    # Realistic plugins array
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            plugins.length = 3;
            return plugins;
        },
        configurable: true
    });
    """,
    # Realistic languages
    """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true
    });
    """,
    # Chrome runtime check (for extension detection bypass)
    """
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
    """,
    # Permissions query override (notification permission)
    """
    const origQuery = window.Notification && Notification.permission;
    if (navigator.permissions) {
        const origPermQuery = navigator.permissions.query;
        navigator.permissions.query = function(parameters) {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return origPermQuery.call(this, parameters);
        };
    }
    """,
]


def inject_stealth(cdp: CDPConnection):
    """Inject all stealth patches into the page.

    Uses Page.addScriptToEvaluateOnNewDocument so patches persist across navigations.
    """
    for script in STEALTH_SCRIPTS:
        try:
            cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": script}, timeout=3)
        except Exception:
            pass  # Non-critical — some may fail on older Chrome versions

    # Also evaluate immediately for the current page
    for script in STEALTH_SCRIPTS:
        try:
            # These patches are optional. Never allow a stuck renderer to hold
            # preflight for six default 30-second CDP timeouts.
            cdp.evaluate(script, timeout=3)
        except Exception:
            pass
