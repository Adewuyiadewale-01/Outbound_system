#!/usr/bin/env python3
"""
LinkedIn automation helper — CDP-based browser control with human simulation.

Operates through a real Chrome profile via Chrome DevTools Protocol (port 18800).
Every action is designed to be indistinguishable from a human using LinkedIn.

Usage as library:
    from linkedin_helper import LinkedInSession
    session = LinkedInSession()
    session.connect()
    session.warm_up()
    ...
    session.cool_down()
    session.disconnect()

Usage as CLI:
    python3 linkedin_helper.py preflight
    python3 linkedin_helper.py warm-up
    python3 linkedin_helper.py view-profile --url <linkedin_url>
    python3 linkedin_helper.py read-feed --scrolls 5
"""

import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlparse

import websocket  # websocket-client

try:
    from outreach_helper import (
        count_outreach_log_connection_requests,
        count_pipeline_connected_leads,
    )
except Exception:  # pragma: no cover - keep LinkedIn-only helpers usable
    count_outreach_log_connection_requests = None
    count_pipeline_connected_leads = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CDP_HOST = "localhost"
CDP_PORT = 18800

# State file for daily quotas, session history, last scroll sequence, etc.
STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "state")
STATE_FILE = os.path.join(STATE_DIR, "linkedin_state.json")
DIAGNOSTIC_DIR = os.path.join(STATE_DIR, "linkedin_debug")

# Hard limits (non-overridable)
MAX_CONN_REQ_PER_DAY = 30
MAX_CONN_REQ_PER_WEEK = 150
MAX_PROFILE_VIEWS_PER_DAY = 100
MAX_MESSAGES_PER_DAY = 50
MAX_WITHDRAWALS_PER_DAY = 5
MAX_ACTIONS_PER_MINUTE = 4

# Warmup schedule: week_number -> max daily conn_req
WARMUP_SCHEDULE = {1: 20, 2: 25}  # week 3+ defaults to MAX_CONN_REQ_PER_DAY

# Acceptance rate thresholds
ACCEPTANCE_RATE_WARN = 0.20
ACCEPTANCE_RATE_CRITICAL = 0.15
ACTIVITY_TAB_ORDER = [
    ("all", "all"),
    ("comments", "Comments"),
    ("reactions", "Reactions"),
]
ACTIVITY_RANKING_TAB_ORDER = [
    ("posts", "Posts"),
    ("reactions", "Reactions"),
    ("comments", "Comments"),
]
ACTIVITY_SELECTOR_RANKING_TAB_ORDER = [
    ("posts", "Posts"),
    ("reactions", "Reactions"),
    ("comments", "Comments"),
]
ACTIVITY_ALLOWED_TAB_KEYS = {"all", "posts", "comments", "reactions"}
ACTIVITY_DISALLOWED_PATH_RE = re.compile(
    r"/recent-activity/(articles|videos|images|documents)/?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Delay utilities — all timing is randomized, never repeating patterns
# ---------------------------------------------------------------------------


def human_delay(min_s: float, max_s: float, distribution: str = "uniform") -> float:
    """Sleep for a randomized duration. Returns the actual delay used."""
    if distribution == "gaussian":
        mean = (min_s + max_s) / 2
        std = (max_s - min_s) / 6  # 99.7% within range
        delay = max(min_s, min(max_s, random.gauss(mean, std)))
    elif distribution == "log_normal":
        # Skews toward shorter delays with occasional longer ones
        mean = math.log((min_s + max_s) / 2)
        std = 0.5
        delay = max(min_s, min(max_s, random.lognormvariate(mean, std)))
    else:  # uniform
        delay = random.uniform(min_s, max_s)
    time.sleep(delay)
    return delay


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


def typing_delay():
    """Delay between keystrokes for natural typing."""
    # Occasional longer pauses (thinking mid-word)
    if random.random() < 0.08:
        time.sleep(random.uniform(0.3, 0.8))
    else:
        time.sleep(random.uniform(0.05, 0.20))


# ---------------------------------------------------------------------------
# State persistence — tracks quotas, session history, scroll sequences
# ---------------------------------------------------------------------------


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _ensure_diagnostic_dir():
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return cleaned.strip("-") or "unknown"


def load_state() -> dict[str, Any]:
    """Load persistent state from disk."""
    _ensure_state_dir()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict[str, Any]):
    """Save persistent state to disk."""
    _ensure_state_dir()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_today_key() -> str:
    return date.today().isoformat()


def get_week_key() -> str:
    """ISO week key like '2026-W11'."""
    d = date.today()
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def increment_counter(state: dict, category: str, amount: int = 1) -> int:
    """Increment a daily counter. Returns new value."""
    today = get_today_key()
    if "counters" not in state:
        state["counters"] = {}
    if today not in state["counters"]:
        state["counters"][today] = {}
    current = state["counters"][today].get(category, 0)
    state["counters"][today][category] = current + amount
    save_state(state)
    return current + amount


def get_counter(state: dict, category: str) -> int:
    """Get today's count for a category."""
    today = get_today_key()
    return state.get("counters", {}).get(today, {}).get(category, 0)


def get_weekly_counter(state: dict, category: str) -> int:
    """Sum this week's count for a category (Mon-Sun)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    total = 0
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        total += state.get("counters", {}).get(day, {}).get(category, 0)
    return total


# ---------------------------------------------------------------------------
# CDP Connection Manager
# ---------------------------------------------------------------------------


class CDPConnection:
    """Manages a WebSocket connection to Chrome DevTools Protocol."""

    def __init__(self, host: str | None = None, port: int | None = None):
        # Resolve this at construction time rather than import time.  The
        # sequential activity-lane runner uses a separate process for each
        # authorized browser profile and supplies its endpoint through env.
        self.host = host or os.environ.get("LINKEDIN_CDP_HOST", CDP_HOST)
        configured_port = (
            port if port is not None else os.environ.get("LINKEDIN_CDP_PORT", CDP_PORT)
        )
        try:
            self.port = int(configured_port)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid LINKEDIN_CDP_PORT: {configured_port!r}")
        self.ws: websocket.WebSocket | None = None
        self.target_id: str | None = None
        self.ws_url: str | None = None
        self._msg_id = 0
        self._lock = threading.Lock()

    def _http_get(self, path: str) -> Any:
        """Make an HTTP GET to the CDP HTTP endpoint."""
        import urllib.request

        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            raise ConnectionError(f"CDP HTTP request failed ({url}): {e}")

    def _http_request(self, path: str, method: str = "GET") -> Any:
        """Make a bounded request to a Chrome debugging HTTP endpoint."""
        import urllib.request

        url = f"http://{self.host}:{self.port}{path}"
        try:
            request = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(request, timeout=5) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except Exception as e:
            raise ConnectionError(f"CDP HTTP request failed ({method} {url}): {e}")

    def replace_page_target(self, url: str = "about:blank") -> dict[str, Any]:
        """Create a clean tab and discard the currently attached frozen tab."""
        old_target_id = self.target_id
        encoded_url = quote(str(url or "about:blank"), safe="")
        new_target = self._http_request(f"/json/new?{encoded_url}", method="PUT")
        new_target_id = new_target.get("id")
        if not new_target_id:
            raise ConnectionError("Chrome did not return a target id for the replacement tab")

        self.disconnect()
        if old_target_id and old_target_id != new_target_id:
            try:
                self._http_get(f"/json/close/{quote(old_target_id, safe='')}")
            except ConnectionError:
                # The renderer may already have disappeared. The clean target is
                # still usable, so a failed close must not discard the recovery.
                pass
        return {
            "ok": True,
            "old_target_id": old_target_id or "",
            "new_target_id": new_target_id,
            "new_target_url": new_target.get("url", url),
        }

    def create_page_target(self, url: str = "about:blank") -> dict[str, Any]:
        """Create and attach to a new tab without closing any existing tab.

        Workflows that share a CDP browser must not reuse or replace another
        workflow's active page.  The returned target id can also be persisted
        by the caller for diagnostics.
        """
        encoded_url = quote(str(url or "about:blank"), safe="")
        target = self._http_request(f"/json/new?{encoded_url}", method="PUT")
        target_id = target.get("id")
        ws_url = target.get("webSocketDebuggerUrl")
        if not target_id or not ws_url:
            raise ConnectionError("Chrome did not return a usable target for the new tab")

        self.disconnect()
        self.target_id = target_id
        self.ws_url = ws_url
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        return {
            "ok": True,
            "target_id": target_id,
            "url": target.get("url", url),
        }

    def health_check(self) -> dict[str, Any]:
        """Check if Chrome is running and CDP is responsive."""
        try:
            version = self._http_get("/json/version")
            return {
                "status": "ok",
                "browser": version.get("Browser", "unknown"),
                "protocol": version.get("Protocol-Version", "unknown"),
                "user_agent": version.get("User-Agent", "unknown"),
            }
        except ConnectionError as e:
            return {"status": "error", "error": str(e)}

    def connect(self, target_url: str | None = None) -> bool:
        """Connect to a Chrome tab via CDP WebSocket.

        If target_url is given, find the tab with that URL.
        Otherwise, connect to the first available page target.
        """
        # Get list of targets (tabs)
        targets = self._http_get("/json/list")
        page_targets = [t for t in targets if t.get("type") == "page"]

        if not page_targets:
            raise ConnectionError("No page targets found in Chrome. Open a tab first.")

        # Find matching target or use first
        target = None
        if target_url:
            for t in page_targets:
                if target_url in t.get("url", ""):
                    target = t
                    break
        if target is None:
            # Prefer a LinkedIn tab if available
            for t in page_targets:
                if "linkedin.com" in t.get("url", ""):
                    target = t
                    break
        if target is None:
            target = page_targets[0]

        self.target_id = target["id"]
        self.ws_url = target["webSocketDebuggerUrl"]

        # Connect WebSocket
        self.ws = websocket.create_connection(
            self.ws_url,
            timeout=30,
            suppress_origin=True,
        )
        return True

    def disconnect(self):
        """Close the CDP WebSocket connection."""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        """Send a CDP command and wait for its response."""
        if not self.ws:
            raise ConnectionError("Not connected to CDP. Call connect() first.")

        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        self.ws.send(json.dumps(message))

        # Wait for matching response
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.ws.settimeout(min(5, timeout - (time.time() - start)))
                raw = self.ws.recv()
                data = json.loads(raw)
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise RuntimeError(
                            f"CDP error: {data['error'].get('message', data['error'])}"
                        )
                    return data.get("result", {})
                # Otherwise it's an event — ignore for now
            except websocket.WebSocketTimeoutException:
                continue

        raise TimeoutError(f"CDP command {method} timed out after {timeout}s")

    def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 30) -> Any:
        """Evaluate JavaScript in the page context and return the result."""
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }
        result = self.send("Runtime.evaluate", params, timeout=timeout)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            exception = details.get("exception") or {}
            description = exception.get("description") or exception.get("value") or ""
            text = details.get("text", "")
            line = details.get("lineNumber")
            column = details.get("columnNumber")
            location = ""
            if isinstance(line, int) and isinstance(column, int):
                location = f" (line {line + 1}, col {column + 1})"
            raise RuntimeError(f"JS evaluation error: {description or text or details}{location}")
        remote_obj = result.get("result", {})
        return remote_obj.get("value")

    def navigate(self, url: str, wait_load: bool = True, timeout: float = 30) -> dict:
        """Navigate to a URL and optionally wait for load."""
        result = self.send("Page.navigate", {"url": url}, timeout=timeout)
        if wait_load:
            # Enable page events if not already
            try:
                self.send("Page.enable", timeout=5)
            except Exception:
                pass
            # Wait for loadEventFired
            start = time.time()
            while time.time() - start < timeout:
                try:
                    self.ws.settimeout(2)
                    raw = self.ws.recv()
                    data = json.loads(raw)
                    if data.get("method") == "Page.loadEventFired":
                        break
                    if data.get("method") == "Page.frameStoppedLoading":
                        break
                except websocket.WebSocketTimeoutException:
                    continue
            # Extra settle time for dynamic content
            human_delay(1.0, 2.5)
        return result

    def get_current_url(self) -> str:
        """Get the current page URL."""
        return self.evaluate("window.location.href") or ""

    def capture_screenshot_base64(self) -> str:
        """Capture the current page as a base64 PNG screenshot."""
        result = self.send("Page.captureScreenshot", {"format": "png"}, timeout=30)
        return result.get("data", "")


# ---------------------------------------------------------------------------
# Stealth patches — mask automation indicators
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Human simulation primitives
# ---------------------------------------------------------------------------


class HumanSimulator:
    """Simulates human-like browser interactions via CDP."""

    def __init__(self, cdp: CDPConnection):
        self.cdp = cdp
        # Viewport dimensions (will be read from browser)
        self._viewport_width = 1440
        self._viewport_height = 900
        self._update_viewport()

    def _update_viewport(self):
        """Read actual viewport dimensions from the browser."""
        try:
            dims = self.cdp.evaluate(
                "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"
            )
            if dims:
                d = json.loads(dims)
                self._viewport_width = d["w"]
                self._viewport_height = d["h"]
        except Exception:
            pass

    # --- Mouse ---

    def move_mouse(self, x: float, y: float, steps: int = 0):
        """Move mouse to coordinates with optional intermediate steps."""
        if steps > 0:
            # Get current position (approximate — start from center if unknown)
            cx, cy = self._viewport_width / 2, self._viewport_height / 2
            for i in range(1, steps + 1):
                frac = i / steps
                # Add slight curve (bezier-like jitter)
                jx = random.uniform(-3, 3)
                jy = random.uniform(-3, 3)
                ix = cx + (x - cx) * frac + jx
                iy = cy + (y - cy) * frac + jy
                self.cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": int(ix),
                        "y": int(iy),
                    },
                    timeout=8,
                )
                time.sleep(random.uniform(0.01, 0.03))

        self.cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": int(x),
                "y": int(y),
            },
            timeout=8,
        )

    def click(self, x: float, y: float, hover_first: bool = True):
        """Click at coordinates with optional hover-before-click."""
        if hover_first:
            self.move_mouse(x, y, steps=random.randint(3, 8))
            human_delay(0.3, 1.5)

        # Mouse down
        self.cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": int(x),
                "y": int(y),
                "button": "left",
                "clickCount": 1,
            },
            timeout=8,
        )
        # Small hold time
        time.sleep(random.uniform(0.05, 0.15))
        # Mouse up
        self.cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": int(x),
                "y": int(y),
                "button": "left",
                "clickCount": 1,
            },
            timeout=8,
        )

    def click_element(self, selector: str, hover_first: bool = True) -> bool:
        """Click an element by CSS selector. Returns True if successful."""
        # Get element position
        pos = self.cdp.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return null;
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return null;
                return JSON.stringify({{
                    x: rect.left + rect.width * {random.uniform(0.3, 0.7)},
                    y: rect.top + rect.height * {random.uniform(0.3, 0.7)},
                    visible: true
                }});
            }})()
        """)
        if not pos:
            return False
        coords = json.loads(pos)
        if not coords.get("visible"):
            return False
        try:
            self.click(coords["x"], coords["y"], hover_first=hover_first)
            return True
        except TimeoutError:
            clicked = self.cdp.evaluate(
                f"""
                (() => {{
                    const el = document.querySelector({json.dumps(selector)});
                    if (!el) return false;
                    el.click();
                    return true;
                }})()
            """,
                timeout=8,
            )
            return bool(clicked)

    # --- Keyboard ---

    def type_text(self, text: str):
        """Type text character by character with human-like timing."""
        for char in text:
            self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "text": char,
                    "key": char,
                    "code": f"Key{char.upper()}" if char.isalpha() else "",
                },
            )
            self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": char,
                    "code": f"Key{char.upper()}" if char.isalpha() else "",
                },
            )
            typing_delay()

    def press_key(self, key: str, code: str = ""):
        """Press a single key (e.g., Enter, Tab, Escape)."""
        key_code_map = {
            "Enter": ("Enter", "Enter", 13),
            "Tab": ("Tab", "Tab", 9),
            "Escape": ("Escape", "Escape", 27),
            "Backspace": ("Backspace", "Backspace", 8),
            "ArrowDown": ("ArrowDown", "ArrowDown", 40),
            "ArrowUp": ("ArrowUp", "ArrowUp", 38),
        }
        k, c, kc = key_code_map.get(key, (key, code or key, 0))
        self.cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": k,
                "code": c,
                "windowsVirtualKeyCode": kc,
                "nativeVirtualKeyCode": kc,
            },
        )
        time.sleep(random.uniform(0.05, 0.12))
        self.cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": k,
                "code": c,
                "windowsVirtualKeyCode": kc,
                "nativeVirtualKeyCode": kc,
            },
        )

    # --- Scrolling ---

    def scroll(self, delta_y: int = 300, smooth: bool = True):
        """Scroll the page by delta_y pixels. Positive = down."""

        def js_scroll(amount: int):
            self.cdp.evaluate(f"window.scrollBy(0, {int(amount)})", timeout=8)

        if smooth:
            # Break into smaller increments for natural scrolling
            remaining = abs(delta_y)
            direction = 1 if delta_y > 0 else -1
            while remaining > 0:
                chunk = min(remaining, random.randint(80, 180))
                try:
                    self.cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseWheel",
                            "x": self._viewport_width // 2 + random.randint(-50, 50),
                            "y": self._viewport_height // 2 + random.randint(-50, 50),
                            "deltaX": 0,
                            "deltaY": chunk * direction,
                        },
                        timeout=8,
                    )
                except TimeoutError:
                    js_scroll(chunk * direction)
                remaining -= chunk
                time.sleep(random.uniform(0.03, 0.08))
        else:
            try:
                self.cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": self._viewport_width // 2,
                        "y": self._viewport_height // 2,
                        "deltaX": 0,
                        "deltaY": delta_y,
                    },
                    timeout=8,
                )
            except TimeoutError:
                js_scroll(delta_y)

    def scroll_to_bottom(
        self,
        fraction: float = 0.8,
        speed: str = "normal",
        max_seconds: float = 25.0,
        max_distance: int = 7000,
    ):
        """Scroll down a fraction of the page height.

        speed: 'slow' (reading), 'normal', 'fast' (skimming)
        """
        page_height = int(self.cdp.evaluate("document.body.scrollHeight") or 3000)
        target = int(page_height * fraction)
        current = int(self.cdp.evaluate("window.scrollY") or 0)
        distance = target - current

        if distance <= 0:
            return
        # Guardrail: LinkedIn can render very tall/infinite pages; cap runtime
        # and travel distance so a single action cannot hang the whole session.
        distance = min(distance, max_distance)

        speed_params = {
            "slow": {"chunk_min": 150, "chunk_max": 300, "pause_min": 0.8, "pause_max": 2.0},
            "normal": {"chunk_min": 250, "chunk_max": 500, "pause_min": 0.3, "pause_max": 1.0},
            "fast": {"chunk_min": 400, "chunk_max": 700, "pause_min": 0.1, "pause_max": 0.4},
        }
        p = speed_params.get(speed, speed_params["normal"])

        start = time.time()
        scrolled = 0
        while scrolled < distance and (time.time() - start) < max_seconds:
            chunk = random.randint(p["chunk_min"], p["chunk_max"])
            chunk = min(chunk, distance - scrolled)
            try:
                self.scroll(chunk)
            except TimeoutError:
                self.cdp.evaluate(f"window.scrollBy(0, {int(chunk)})", timeout=8)
            scrolled += chunk
            human_delay(p["pause_min"], p["pause_max"])

    def get_scroll_position(self) -> dict[str, int]:
        """Get current scroll position and page dimensions."""
        result = self.cdp.evaluate("""
            JSON.stringify({
                scrollY: Math.round(window.scrollY),
                scrollHeight: document.body.scrollHeight,
                viewportHeight: window.innerHeight
            })
        """)
        return (
            json.loads(result)
            if result
            else {"scrollY": 0, "scrollHeight": 0, "viewportHeight": 900}
        )


# ---------------------------------------------------------------------------
# Element visibility checker (honeypot guard)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Page detection — identify what LinkedIn page we're on
# ---------------------------------------------------------------------------


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


PROFILE_TOPCARD_SELECTOR = (
    '[componentkey*="profile.card"][componentkey*="Topcard"], '
    '[componentkey*="profile.card"][componentkey*="topcard"], '
    '[componentkey*="Topcard"], '
    '[componentkey*="topcard"], '
    ".pv-top-card"
)
PROFILE_READY_SELECTOR = f"main h1, h1, {PROFILE_TOPCARD_SELECTOR}"
PROFILE_ACTION_READY_SELECTOR = (
    f'{PROFILE_READY_SELECTOR}, button, [role="button"], '
    'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"]'
)
PROFILE_MORE_CONNECT_SELECTOR = (
    'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"]'
    '[href^="/preload/custom-invite/"]'
)


# ---------------------------------------------------------------------------
# Page-readiness gate — used by acceptance-check path for resilient navigation
# ---------------------------------------------------------------------------


def _wait_for_page_ready(
    cdp: CDPConnection,
    expected_selector: str | None = None,
    timeout: float = 20,
    poll_interval: float = 1.0,
    stable_for: float = 0.0,
    ignored_overlays: set | None = None,
    ignored_loaders: set | None = None,
) -> dict[str, Any]:
    """Wait until the page is interactive and optionally until an expected selector appears.

    Checks:
    1. document.readyState is 'complete' or 'interactive'
    2. No overlay/modal blocking interaction (cookie consent, interstitials)
    3. Expected selector is present in the DOM (if provided)
    4. LinkedIn skeleton/loading elements have cleared
    5. Page text length is stable for stable_for seconds (if provided)

    Returns dict with 'ready' bool, 'readyState', 'overlay_detected', 'selector_found'.
    """
    start = time.time()
    last_state: dict[str, Any] = {}
    stable_since: float | None = None
    last_text_len: int | None = None

    while time.time() - start < timeout:
        try:
            check = cdp.evaluate(
                """
                (() => {
                    const rs = document.readyState;

                    // Detect common LinkedIn overlays / modals
                    const overlaySelectors = [
                        '[class*="modal--overlay"]',
                        '[class*="cookie-consent"]',
                        '.artdeco-modal-overlay',
                        '[data-test-modal]',
                    ];
                    let overlay = null;
                    for (const sel of overlaySelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            overlay = sel;
                            break;
                        }
                    }

                    const loadingSelectors = [
                        '.artdeco-loader',
                        '.artdeco-spinner',
                        '.skeleton-loader',
                        '[class*="skeleton"]',
                        '[aria-busy="true"]',
                        '[data-test-id*="loading"]',
                    ];
                    let loading = null;
                    for (const sel of loadingSelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            loading = sel;
                            break;
                        }
                    }

                    return JSON.stringify({
                        readyState: rs,
                        overlay: overlay,
                        loading: loading,
                        textLen: (document.body && document.body.innerText || '').length,
                        url: window.location.href,
                    });
                })()
            """,
                timeout=8,
            )
        except Exception as exc:
            last_state = {
                "ready": False,
                "probe_error": str(exc),
                "elapsed": round(time.time() - start, 1),
            }
            time.sleep(poll_interval)
            continue
        info = json.loads(check) if check else {}
        ready_state = info.get("readyState", "")
        overlay = info.get("overlay")
        blocking_overlay = bool(overlay and overlay not in (ignored_overlays or set()))
        loading = info.get("loading")
        blocking_loading = bool(loading and loading not in (ignored_loaders or set()))
        text_len = int(info.get("textLen", 0) or 0)

        # readyState must be at least interactive
        page_ready = ready_state in ("interactive", "complete")

        # Check selector presence if requested
        selector_found = True
        if expected_selector and page_ready:
            try:
                found = cdp.evaluate(
                    f"!!document.querySelector({json.dumps(expected_selector)})",
                    timeout=8,
                )
                selector_found = bool(found)
            except Exception as exc:
                selector_found = False
                last_state = {
                    "ready": False,
                    "selector_probe_error": str(exc),
                    "elapsed": round(time.time() - start, 1),
                }
                time.sleep(poll_interval)
                continue

        base_ready = page_ready and selector_found and not blocking_overlay and not blocking_loading
        if base_ready and stable_for > 0:
            if last_text_len is None or abs(text_len - last_text_len) > 20:
                stable_since = time.time()
                last_text_len = text_len
            elif stable_since is None:
                stable_since = time.time()
            stable_ready = (time.time() - stable_since) >= stable_for
        else:
            stable_since = None
            last_text_len = text_len
            stable_ready = True

        last_state = {
            "ready": base_ready and stable_ready,
            "readyState": ready_state,
            "overlay_detected": overlay,
            "overlay_blocking": blocking_overlay,
            "loading_detected": loading,
            "loading_blocking": blocking_loading,
            "selector_found": selector_found if expected_selector else None,
            "text_len": text_len,
            "stable_for": stable_for if stable_for else None,
            "url": info.get("url", ""),
            "elapsed": round(time.time() - start, 1),
        }

        if last_state["ready"]:
            return last_state

        # If overlay detected, try to dismiss it
        if blocking_overlay and page_ready:
            try:
                cdp.evaluate(
                    """
                    (() => {
                        // Try dismissing cookie consent / generic modals
                        const dismissBtns = document.querySelectorAll(
                            '[class*="cookie"] button[action-type="ACCEPT"], ' +
                            '.artdeco-modal__dismiss, ' +
                            'button[data-test-modal-close-btn], ' +
                            'button[aria-label="Dismiss"], button[aria-label="Close"]'
                        );
                        if (dismissBtns.length > 0) dismissBtns[0].click();
                    })()
                """,
                    timeout=8,
                )
            except Exception:
                pass

        time.sleep(poll_interval)

    # Timed out
    last_state["ready"] = False
    last_state["timeout"] = True
    return last_state


def _wait_for_linkedin_ready(
    cdp: CDPConnection,
    expected_selector: str | None = None,
    timeout: float = 45,
    stable_for: float = 1.5,
    ignored_overlays: set | None = None,
    ignored_loaders: set | None = None,
) -> dict[str, Any]:
    """Wait for LinkedIn's SPA shell and expected content to finish rendering."""
    return _wait_for_page_ready(
        cdp,
        expected_selector=expected_selector,
        timeout=timeout,
        poll_interval=1.0,
        stable_for=stable_for,
        ignored_overlays=ignored_overlays,
        ignored_loaders=ignored_loaders,
    )


def _navigate_with_readiness(
    cdp: CDPConnection,
    url: str,
    expected_selector: str | None = None,
    nav_timeout: float = 30,
    ready_timeout: float = 20,
) -> dict[str, Any]:
    """Navigate to a URL using JS (no mouse events) and wait for readiness.

    Designed for the acceptance-check path where reliability > human-likeness.
    Falls back to window.location if cdp.navigate has issues.
    """
    result: dict[str, Any] = {"url": url, "method": "cdp_navigate"}

    try:
        cdp.navigate(url, wait_load=True, timeout=nav_timeout)
    except (TimeoutError, RuntimeError) as exc:
        # Fallback: JS-based navigation (no mouse/CDP Page dependency)
        result["method"] = "js_location_fallback"
        result["primary_error"] = str(exc)
        try:
            cdp.evaluate(f"window.location.href = '{url}'")
            # Give the page a moment to start loading
            time.sleep(2)
        except Exception as exc2:
            result["ready"] = False
            result["error"] = f"Both navigation methods failed: {exc}; {exc2}"
            return result

    # Wait for readiness
    readiness = _wait_for_page_ready(cdp, expected_selector, timeout=ready_timeout)
    result.update(readiness)
    return result


def _safe_scroll_or_js(
    cdp: CDPConnection,
    sim: "HumanSimulator",
    pixels: int,
) -> dict[str, Any]:
    """Attempt a human-sim scroll; fall back to JS scrollBy on timeout.

    Scoped to acceptance-check path where we prefer reliability over
    pixel-perfect human simulation.
    """
    result: dict[str, Any] = {"method": "human_sim"}
    try:
        sim.scroll(pixels)
    except TimeoutError as exc:
        result["method"] = "js_fallback"
        result["fallback_reason"] = str(exc)
        jitter = random.randint(-30, 30)
        cdp.evaluate(f"window.scrollBy(0, {pixels + jitter})")
    return result


# ---------------------------------------------------------------------------
# Feed reader — content-aware scrolling
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Profile viewer
# ---------------------------------------------------------------------------


def _safe_debug_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip())
    return slug.strip("_")[:80] or "profile"


def _write_profile_mapper_dump(
    cdp: CDPConnection,
    profile_url: str,
    result: dict[str, Any],
    navigation_started_at: float,
    readiness_fired_at: str,
) -> None:
    """Write diagnostic-only top-card dumps without changing mapper behavior."""
    try:
        dump_taken_at = datetime.now().isoformat(timespec="milliseconds")
        raw = cdp.evaluate(
            """
            (() => {
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const h1 = document.querySelector('h1');
                const topCard =
                    document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                    document.querySelector('[componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="topcard"]') ||
                    h1?.closest('section') ||
                    h1?.closest('.artdeco-card') ||
                    document.querySelector('.pv-top-card') ||
                    document.querySelector('main');
                const topText = norm((topCard && topCard.innerText) || '');
                return JSON.stringify({
                    url: location.href,
                    readyState: document.readyState,
                    profileName: norm(h1?.innerText || '') || topText.split(' · ')[0].split('\\n')[0],
                    topCardOuterHTML: topCard ? topCard.outerHTML : '',
                    bodyTextSample: ((document.body && document.body.innerText) || '').slice(0, 2000),
                });
            })()
        """,
            timeout=10,
        )
        dump = json.loads(raw) if raw else {}
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "debug"))
        os.makedirs(debug_dir, exist_ok=True)
        lead_slug = _safe_debug_slug(
            dump.get("profileName") or profile_url.rstrip("/").split("/")[-1]
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base = os.path.join(debug_dir, f"{lead_slug}_{timestamp}")
        html_path = f"{base}.html"
        json_path = f"{base}.json"
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(str(dump.get("topCardOuterHTML", "")))
        payload = {
            "profile_url": profile_url,
            "current_url": dump.get("url"),
            "profile_name": dump.get("profileName"),
            "readiness_fired_at": readiness_fired_at,
            "top_card_ready_at": result.get("top_card_ready_at"),
            "dump_taken_at": dump_taken_at,
            "ms_since_navigation": round((time.time() - navigation_started_at) * 1000),
            "classification": result.get("state"),
            "direct_buttons_seen": result.get("direct_buttons_seen", []),
            "more_button_seen": result.get("more_button_seen"),
            "more_clicked_at": result.get("more_menu", {}).get("more_clicked_at")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "more_menu_items_at_read": result.get("more_menu", {}).get("menu_items_count")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "more_scope_found": result.get("more_menu", {}).get("scope_found")
            if isinstance(result.get("more_menu"), dict)
            else None,
            "result": result,
            "html_path": html_path,
            "body_text_sample": dump.get("bodyTextSample", ""),
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        result["debug_dump"] = {"html_path": html_path, "json_path": json_path}
    except Exception as exc:
        result["debug_dump_error"] = str(exc)


def inspect_profile_action_state(cdp: CDPConnection, profile_url: str) -> dict[str, Any]:
    """Inspect the profile top-card action controls without heavy browsing."""
    result: dict[str, Any] = {
        "action": "inspect_profile_action_state",
        "profile_url": profile_url,
        "state": "unknown",
    }
    try:
        navigation_started_at = time.time()
        cdp.navigate(profile_url)
        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_READY_SELECTOR,
            timeout=45,
            stable_for=1.0,
            # LinkedIn keeps this SPA wrapper visible on normal profile pages;
            # it is not the same as an auth wall once profile content is present.
            ignored_overlays={".authentication-outlet"},
        )
        readiness_fired_at = datetime.now().isoformat(timespec="milliseconds")
        result["load_state"] = ready_state
        if not ready_state.get("ready"):
            result["state"] = "unknown"
            result["error"] = "profile_load_timeout"
            _write_profile_mapper_dump(
                cdp, profile_url, result, navigation_started_at, readiness_fired_at
            )
            return result

        danger = check_circuit_breakers(cdp)
        if danger:
            result["state"] = "profile_unavailable"
            result["error"] = danger
            _write_profile_mapper_dump(
                cdp, profile_url, result, navigation_started_at, readiness_fired_at
            )
            return result

        raw = cdp.evaluate(
            """
            (() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0';
                };
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const h1 = document.querySelector('h1');
                const topCard =
                    document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                    document.querySelector('[componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="topcard"]') ||
                    h1?.closest('section') ||
                    h1?.closest('.artdeco-card') ||
                    document.querySelector('.pv-top-card') ||
                    document.querySelector('main');
                const controls = Array.from((topCard || document).querySelectorAll('button, [role="button"], a'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName.toLowerCase(),
                            role: el.getAttribute('role') || '',
                            text: norm(el.innerText || el.textContent || ''),
                            ariaLabel: norm(el.getAttribute('aria-label') || ''),
                            dataControlName: norm(el.getAttribute('data-control-name') || ''),
                            href: el.getAttribute('href') || '',
                            componentKey: norm(el.getAttribute('componentkey') || ''),
                            rect: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                            },
                        };
                    });
                const bodyText = norm((document.body && document.body.innerText) || '');
                return JSON.stringify({
                    url: location.href,
                    readyState: document.readyState,
                    profileName: norm(h1?.innerText || '') || norm((topCard && topCard.innerText) || '').split(' · ')[0].split('\\n')[0],
                    topCardText: norm((topCard && topCard.innerText) || ''),
                    bodySample: bodyText.slice(0, 1000),
                    controls,
                });
            })()
        """,
            timeout=10,
        )
        data = json.loads(raw) if raw else {}
        result.update(data)

        controls = data.get("controls", []) if isinstance(data, dict) else []
        result["top_card_ready_at"] = (
            datetime.now().isoformat(timespec="milliseconds") if controls else None
        )
        result["direct_buttons_seen"] = [
            str(item.get("text", "")).strip()
            for item in controls
            if str(item.get("tag", "")).lower() == "button" and str(item.get("text", "")).strip()
        ]
        top_text = str(data.get("topCardText", "")).strip()
        body_sample = str(data.get("bodySample", "")).strip().lower()

        def exact_text(value: str) -> list[dict[str, Any]]:
            return [
                item
                for item in controls
                if str(item.get("text", "")).strip().lower() == value.lower()
            ]

        pending_controls = [
            item
            for item in controls
            if str(item.get("text", "")).strip().lower() == "pending"
            or str(item.get("ariaLabel", "")).strip().lower().startswith("pending")
            or "withdraw invitation" in str(item.get("ariaLabel", "")).lower()
        ]
        direct_connect = [
            item
            for item in controls
            if str(item.get("text", "")).strip().lower() == "connect"
            and (
                re.match(r"^invite .+ to connect$", str(item.get("ariaLabel", "")).strip(), re.I)
                or str(item.get("componentKey", "")).startswith("ConnectButtonstate:invitation:")
                or str(item.get("tag", "")).lower() in {"button", "a"}
            )
        ]
        message_controls = exact_text("message")

        if any(
            phrase in body_sample
            for phrase in (
                "profile not found",
                "this profile is not available",
                "this profile is unavailable",
                "member not found",
            )
        ):
            result["state"] = "profile_unavailable"
        elif pending_controls:
            result["state"] = "already_pending"
            result["matched_control"] = pending_controls[0]
        elif direct_connect:
            result["state"] = "connect_direct"
            result["matched_control"] = direct_connect[0]
        else:
            more_controls = [
                item
                for item in controls
                if str(item.get("text", "")).strip().lower() == "more"
                or str(item.get("ariaLabel", "")).strip().lower() == "more actions"
            ]
            result["more_button_seen"] = bool(more_controls)
            if more_controls:
                menu_connect = _open_more_and_find_connect(cdp)
                result["more_menu"] = menu_connect
                if menu_connect.get("found"):
                    result["state"] = "connect_in_more"
                    result["matched_control"] = menu_connect.get("control")
                elif menu_connect.get("clicked_more") and not menu_connect.get("menu_populated"):
                    result["state"] = "unknown"
                    result["error"] = "more_menu_unreadable"
            if result["state"] == "unknown":
                top_lower = f" {top_text.lower()} "
                is_first = " 1st " in top_lower or "1st degree connection" in top_lower
                has_connect_like = bool(direct_connect or exact_text("connect"))
                has_pending_like = bool(pending_controls)
                if is_first and message_controls and not has_connect_like and not has_pending_like:
                    result["state"] = "already_connected"
                elif controls and result.get("error") != "more_menu_unreadable":
                    result["state"] = "no_connect_button"
        _write_profile_mapper_dump(
            cdp, profile_url, result, navigation_started_at, readiness_fired_at
        )
        return result
    except Exception as exc:
        result["state"] = "unknown"
        result["error"] = str(exc)
        return result


def _open_more_and_find_connect(cdp: CDPConnection) -> dict[str, Any]:
    """Open the profile More menu once and look for an exact Connect item."""
    more_clicked_at: str | None = None
    clicked = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const h1 = document.querySelector('h1');
            const topCard =
                document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                document.querySelector('[componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="topcard"]') ||
                h1?.closest('section') ||
                h1?.closest('.artdeco-card') ||
                document.querySelector('.pv-top-card');
            if (!topCard) return false;
            const nodes = Array.from(topCard.querySelectorAll('button, [role="button"]'));
            const more = nodes.find((node) =>
                visible(node) &&
                (norm(node.innerText || node.textContent) === 'more' ||
                 norm(node.getAttribute('aria-label')) === 'more actions')
            );
            if (!more) return false;
            more.click();
            return true;
        })()
    """,
        timeout=8,
    )
    if not clicked:
        return {"found": False, "clicked_more": False}

    more_clicked_at = datetime.now().isoformat(timespec="milliseconds")
    deadline = time.time() + 5.0
    last_data: dict[str, Any] = {
        "found": False,
        "clicked_more": True,
        "more_clicked_at": more_clicked_at,
        "menu_items_count": 0,
        "menu_populated": False,
    }
    while time.time() < deadline:
        raw = cdp.evaluate(
            """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const sduiConnect = Array.from(document.querySelectorAll(
                'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]'
            )).find(visible);
            const scope = document.querySelector('.artdeco-dropdown__content--is-open') ||
                (sduiConnect ? sduiConnect.closest('[role="menu"], [data-test-menu], ul, div') : null);
            const menuItems = scope ?
                Array.from(scope.querySelectorAll('.artdeco-dropdown__item, [role="menuitem"]')).filter(visible) :
                Array.from(document.querySelectorAll('[role="menuitem"]')).filter(visible);
            const menuItemsCount = menuItems.length;
            const connect = sduiConnect || menuItems.find((node) =>
                /\\bto connect\\b/i.test(norm(node.getAttribute('aria-label') || '')) ||
                norm(node.innerText || node.textContent).toLowerCase() === 'connect' ||
                (node.getAttribute('componentkey') || '').startsWith('ConnectButtonstate:invitation:')
            );
            if (connect) {
                return JSON.stringify({
                    found: true,
                    menuItemsCount,
                    menuOpen: true,
                    scopeFound: !!scope,
                    control: {
                        tag: connect.tagName.toLowerCase(),
                        role: connect.getAttribute('role') || '',
                        text: norm(connect.innerText || connect.textContent || ''),
                        ariaLabel: norm(connect.getAttribute('aria-label') || ''),
                        href: connect.getAttribute('href') || '',
                        componentKey: norm(connect.getAttribute('componentkey') || ''),
                    },
                });
            }
            return JSON.stringify({
                found: false,
                menuItemsCount,
                menuOpen: !!scope || menuItemsCount > 0,
                scopeFound: !!scope,
            });
        })()
        """,
            timeout=8,
        )
        data = json.loads(raw) if raw else {"found": False, "menuItemsCount": 0}
        menu_items_count = int(data.get("menuItemsCount", 0) or 0)
        last_data = {
            "found": bool(data.get("found")),
            "clicked_more": True,
            "more_clicked_at": more_clicked_at,
            "menu_open": bool(data.get("menuOpen")),
            "scope_found": data.get("scopeFound"),
            "menu_items_count": menu_items_count,
            "menu_populated": menu_items_count > 0,
        }
        if data.get("found"):
            last_data["control"] = data.get("control")
            return last_data
        if menu_items_count > 0:
            return last_data
        time.sleep(0.1)
    last_data["error"] = "more_menu_items_timeout"
    return last_data


def _open_more_and_click_connect(cdp: CDPConnection) -> dict[str, Any]:
    """Open More and click the Connect item inside it.

    Mirrors _open_more_and_find_connect (JS click on More, polling for the open
    dropdown, find Connect by aria-label) but also clicks the matched item.
    Used by the send path so connect_in_more click has the same reliability as
    the mapper's classification.
    """

    def click_open_menu_connect() -> dict[str, Any]:
        raw = cdp.evaluate(
            """
        (() => {
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const sduiConnect = Array.from(document.querySelectorAll(
                'a[role="menuitem"][componentkey^="ConnectButtonstate:invitation:"][href^="/preload/custom-invite/"]'
            )).find(visible);
            const scope = document.querySelector('.artdeco-dropdown__content--is-open') ||
                (sduiConnect ? sduiConnect.closest('[role="menu"], [data-test-menu], ul, div') : null);
            const menuItems = scope ?
                Array.from(scope.querySelectorAll('.artdeco-dropdown__item, [role="menuitem"]')).filter(visible) :
                Array.from(document.querySelectorAll('[role="menuitem"]')).filter(visible);
            const menuItemsCount = menuItems.length;
            const connect = sduiConnect || menuItems.find((node) =>
                /\\bto connect\\b/i.test(norm(node.getAttribute('aria-label') || '')) ||
                norm(node.innerText || node.textContent).toLowerCase() === 'connect' ||
                (node.getAttribute('componentkey') || '').startsWith('ConnectButtonstate:invitation:')
            );
            if (connect) {
                connect.click();
                return JSON.stringify({
                    connectClicked: true,
                    menuItemsCount,
                    menuOpen: true,
                    scopeFound: !!scope,
                    ariaLabel: norm(connect.getAttribute('aria-label') || ''),
                    href: connect.getAttribute('href') || '',
                    componentKey: norm(connect.getAttribute('componentkey') || ''),
                });
            }
            return JSON.stringify({
                connectClicked: false,
                menuItemsCount,
                menuOpen: !!scope || menuItemsCount > 0,
                scopeFound: !!scope,
            });
        })()
        """,
            timeout=8,
        )
        data = json.loads(raw) if raw else {"connectClicked": False, "menuItemsCount": 0}
        return {
            "connect_clicked": bool(data.get("connectClicked")),
            "menu_open": bool(data.get("menuOpen")),
            "scope_found": data.get("scopeFound"),
            "menu_items_count": int(data.get("menuItemsCount", 0) or 0),
            "aria_label": data.get("ariaLabel"),
            "href": data.get("href"),
            "component_key": data.get("componentKey"),
        }

    # The mapper may have already opened More. Do not click More again and
    # accidentally close the menu; consume the open dropdown first.
    existing = click_open_menu_connect()
    if existing.get("connect_clicked"):
        existing["clicked_more"] = False
        existing["used_existing_menu"] = True
        return existing

    more_clicked_at: str | None = None
    clicked = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const h1 = document.querySelector('h1');
            const topCard =
                document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                document.querySelector('[componentkey*="Topcard"]') ||
                document.querySelector('[componentkey*="topcard"]') ||
                h1?.closest('section') ||
                h1?.closest('.artdeco-card') ||
                document.querySelector('.pv-top-card');
            if (!topCard) return false;
            const nodes = Array.from(topCard.querySelectorAll('button, [role="button"]'))
                .filter((node) => !node.closest('.artdeco-dropdown__content'));
            const more = nodes.find((node) =>
                visible(node) &&
                (norm(node.innerText || node.textContent) === 'more' ||
                 norm(node.getAttribute('aria-label')) === 'more actions')
            );
            if (!more) return false;
            more.click();
            return true;
        })()
    """,
        timeout=8,
    )
    if not clicked:
        return {"clicked_more": False, "connect_clicked": False}

    more_clicked_at = datetime.now().isoformat(timespec="milliseconds")
    deadline = time.time() + 5.0
    last_data: dict[str, Any] = {
        "clicked_more": True,
        "connect_clicked": False,
        "more_clicked_at": more_clicked_at,
        "menu_items_count": 0,
    }
    while time.time() < deadline:
        data = click_open_menu_connect()
        menu_items_count = int(data.get("menu_items_count", 0) or 0)
        last_data = {
            "clicked_more": True,
            "connect_clicked": bool(data.get("connect_clicked")),
            "more_clicked_at": more_clicked_at,
            "menu_open": bool(data.get("menu_open")),
            "menu_items_count": menu_items_count,
            "aria_label": data.get("aria_label"),
            "href": data.get("href"),
            "component_key": data.get("component_key"),
        }
        if data.get("connect_clicked"):
            return last_data
        if menu_items_count > 0:
            last_data["error"] = "connect_not_in_menu"
            return last_data
        time.sleep(0.1)
    last_data["error"] = "more_menu_items_timeout"
    return last_data


def browse_profile_briefly(
    cdp: CDPConnection, sim: HumanSimulator, seconds: float = 4.0
) -> dict[str, Any]:
    """Bounded profile browsing used after a profile's action state is known."""
    started = time.time()
    try:
        sim.scroll_to_bottom(
            fraction=random.uniform(0.18, 0.32),
            speed="normal",
            max_seconds=max(1.0, min(seconds, 6.0)),
            max_distance=1800,
        )
        human_delay(1.0, 2.5)
        return {"ok": True, "elapsed": round(time.time() - started, 1)}
    except Exception as exc:
        return {"ok": False, "elapsed": round(time.time() - started, 1), "error": str(exc)}


def view_profile(cdp: CDPConnection, sim: HumanSimulator, profile_url: str) -> dict[str, Any]:
    """Navigate to a profile, scroll naturally, and extract key data.

    Returns extracted profile info.
    """
    inspected = inspect_profile_action_state(cdp, profile_url)
    if inspected.get("error") and inspected.get("state") == "unknown":
        return {"error": True, "danger": inspected.get("error"), "inspection": inspected}

    browse = browse_profile_briefly(cdp, sim, seconds=5.0)
    top_text = str(inspected.get("topCardText", ""))
    return {
        "name": str(inspected.get("profileName", "")).strip(),
        "headline": "",
        "location": "",
        "about": "",
        "connection_degree": "1st"
        if re.search(r"\b1st\b|1st degree connection", top_text, re.I)
        else "",
        "has_connect_button": inspected.get("state") in {"connect_direct", "connect_in_more"},
        "is_pending": inspected.get("state") == "already_pending",
        "is_connected": inspected.get("state") == "already_connected",
        "action_state": inspected.get("state"),
        "inspection": inspected,
        "browse": browse,
    }


# ---------------------------------------------------------------------------
# Activity tab reader
# ---------------------------------------------------------------------------


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


def _current_activity_url_is_disallowed(cdp: CDPConnection) -> bool:
    """Return True when LinkedIn has navigated to an activity tab we never use."""
    try:
        current_url = str(cdp.evaluate("window.location.href") or "")
    except Exception:
        return False
    return bool(ACTIVITY_DISALLOWED_PATH_RE.search(current_url))


def _activity_url_for_tab(profile_base: str, tab_key: str) -> str:
    endpoint = "all" if tab_key == "posts" else tab_key
    return f"{profile_base}/recent-activity/{endpoint}/"


def canonicalize_linkedin_profile_url(value: Any) -> str:
    """Return the canonical public URL for a recognizable LinkedIn /in/ profile."""
    raw = re.sub(r"\s+", "", str(value or "").strip())
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        raw = "https://" + raw

    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return ""

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() != "in":
        return ""

    slug = unquote(path_parts[1]).strip()
    if not slug or "/" in slug or "\\" in slug or any(char.isspace() for char in slug):
        return ""
    # LinkedIn public identifiers are URL path segments. Encode any harmless
    # non-ASCII character while keeping the characters used by vanity slugs.
    encoded_slug = quote(slug, safe="-._~")
    return f"https://www.linkedin.com/in/{encoded_slug}"


def _activity_profile_slug(profile_url: str) -> str:
    match = re.search(r"/in/([^/?#]+)/?", str(profile_url or ""), re.IGNORECASE)
    return (match.group(1) if match else "").lower()


def _activity_destination_matches(current_url: str, profile_base: str, tab_key: str) -> bool:
    slug = _activity_profile_slug(profile_base)
    if not slug:
        return False
    endpoint = "all" if tab_key == "posts" else tab_key
    path = re.sub(r"^https?://[^/]+", "", str(current_url or "").lower())
    path = re.split(r"[?#]", path, maxsplit=1)[0].rstrip("/")
    expected = f"/in/{slug}/recent-activity/{endpoint}"
    return path == expected


def _page_still_loading(cdp: CDPConnection) -> bool:
    """True when the page reports active loading or in-flight resource fetches."""
    try:
        raw = cdp.evaluate(
            "JSON.stringify({rs: document.readyState, "
            "pending: performance.getEntriesByType('resource').filter(r => !r.responseEnd).length})",
            timeout=5,
        )
        state = json.loads(raw) if raw else {}
        return state.get("rs") == "loading" or int(state.get("pending", 0) or 0) > 2
    except Exception:
        return False


def _wait_for_activity_destination(
    cdp: CDPConnection,
    profile_base: str,
    tab_key: str,
    timeout: float = 16.0,
    poll_interval: float = 0.35,
) -> dict[str, Any]:
    """Wait until async navigation reaches the requested activity tab or a hard invalid page.

    LinkedIn's SPA router passes through a transitional /preload/ URL while
    fetching the activity feed. That phase is navigation-in-progress, not a
    failed arrival — it neither matches the destination nor counts against
    the timeout budget. Only non-transit time consumes the deadline.
    """
    started = time.time()
    last_url = ""
    transit_seconds = 0.0

    def effective_elapsed() -> float:
        return time.time() - started - transit_seconds

    while True:
        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {
                "arrived": False,
                "invalid": invalid,
                "url": invalid.get("url", ""),
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        try:
            last_url = str(cdp.evaluate("window.location.href", timeout=5) or "")
        except Exception as exc:
            return {
                "arrived": False,
                "reason": "url_probe_failed",
                "error": str(exc),
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        if _activity_destination_matches(last_url, profile_base, tab_key):
            return {
                "arrived": True,
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        if "/preload/" in last_url:
            # Navigation in progress: the click is being honored, the SPA
            # router just hasn't swapped the URL yet. Wait without burning
            # the arrival budget.
            time.sleep(poll_interval)
            transit_seconds += poll_interval
            continue
        if _page_still_loading(cdp):
            # Circumstantial grace: the page reports active loading or pending
            # network fetches. A fixed budget is unfair to slow networks —
            # wait without counting, same as transit.
            time.sleep(poll_interval)
            transit_seconds += poll_interval
            continue
        if effective_elapsed() >= max(0.5, timeout):
            return {
                "arrived": False,
                "reason": "activity_destination_not_reached",
                "url": last_url,
                "elapsed_sec": round(time.time() - started, 2),
                "transit_sec": round(transit_seconds, 2),
            }
        remaining = max(0.05, timeout - effective_elapsed())
        time.sleep(min(max(0.05, poll_interval), remaining))


def _activity_invalid_result(cdp: CDPConnection) -> dict[str, Any] | None:
    """Detect profile-level invalid pages before a blank can be misclassified."""
    page = detect_page(cdp)
    if page.get("danger_type") == "invalid_profile_or_404":
        danger = "invalid_profile_or_404"
    else:
        danger = check_circuit_breakers(cdp)
    if not danger:
        return None
    return {
        "error": True,
        "danger": danger,
        "reason": "invalid_profile_or_404" if danger == "invalid_profile_or_404" else danger,
        "page_type": page.get("page_type", "unknown"),
        "url": page.get("url", ""),
        "title": page.get("title", ""),
    }


def _activity_scroll_snapshot(cdp: CDPConnection) -> dict[str, Any]:
    raw = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            };
            const hasTime = (el) => /\\b\\d+\\s*(s|m|h|d|w|mo|yr|y)\\b|today|yesterday|ago/i.test(el.innerText || el.textContent || '');
            const strictCards = Array.from(document.querySelectorAll('.feed-shared-update-v2[data-urn*="activity"]'))
                .filter((el) => visible(el) && hasTime(el));
            const fallbackCards = strictCards.length ? [] : Array.from(document.querySelectorAll('.feed-shared-update-v2, [data-urn*="activity"]'))
                .filter((el) => visible(el) && hasTime(el));
            const cards = strictCards.length ? strictCards : fallbackCards;
            const loaders = Array.from(document.querySelectorAll(
                '.artdeco-loader, .artdeco-spinner, [aria-busy="true"], [class*="skeleton"], [class*="loading"]'
            )).filter(visible);
            const bodyText = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const emptyNodes = Array.from(document.querySelectorAll(
                '.scaffold-finite-scroll__empty, .artdeco-empty-state, [class*="empty-state"]'
            )).filter(visible);
            const emptySignals = emptyNodes
                .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 3);
            const explicitEmptyText = /nothing to see for now|no posts|no activity|hasn.t posted|nothing to show|no results/i;
            // "Couldn't load" is deliberately not an empty signal: it means the
            // page failed, not that the member has no activity.
            const emptyState = cards.length === 0 && (
                emptySignals.some((text) => explicitEmptyText.test(text)) ||
                explicitEmptyText.test(bodyText)
            );
            return JSON.stringify({
                scrollY: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
                scrollHeight: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
                viewportHeight: window.innerHeight || document.documentElement.clientHeight || 0,
                cardCount: cards.length,
                strictCardCount: strictCards.length,
                fallbackCardCount: fallbackCards.length,
                loading: loaders.length > 0,
                emptyState,
                emptySignals,
                url: window.location.href
            });
        })()
    """,
        timeout=8,
    )
    return (
        json.loads(raw)
        if raw
        else {
            "scrollY": 0,
            "scrollHeight": 0,
            "viewportHeight": 0,
            "cardCount": 0,
            "loading": False,
            "emptyState": False,
            "emptySignals": [],
            "url": "",
        }
    )


def _wait_for_activity_feed_state(
    cdp: CDPConnection,
    timeout: float = 12.0,
    poll_interval: float = 0.7,
) -> dict[str, Any]:
    """Wait for LinkedIn's client-rendered activity feed, not just the shell."""
    started = time.time()
    last: dict[str, Any] = {}
    while time.time() - started < max(0.5, timeout):
        invalid = _activity_invalid_result(cdp)
        if invalid:
            return {
                "ready": False,
                "invalid": invalid,
                "elapsed_sec": round(time.time() - started, 2),
            }
        try:
            last = _activity_scroll_snapshot(cdp)
        except (TimeoutError, RuntimeError) as exc:
            last = {"error": str(exc)}
        if int(last.get("cardCount", 0) or 0) > 0:
            return {
                "ready": True,
                "reason": "activity_cards_visible",
                "snapshot": last,
                "elapsed_sec": round(time.time() - started, 2),
            }
        if last.get("emptyState") and not last.get("loading"):
            return {
                "ready": True,
                "reason": "explicit_empty_state",
                "snapshot": last,
                "elapsed_sec": round(time.time() - started, 2),
            }
        time.sleep(min(max(0.1, poll_interval), max(0.1, timeout - (time.time() - started))))
    return {
        "ready": False,
        "reason": "activity_feed_not_hydrated",
        "snapshot": last,
        "elapsed_sec": round(time.time() - started, 2),
    }


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
    max_seconds: float = 30.0,
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
            return timeout_result(tab_key, "timeout_before_scroll")
        scroll_budget = max(1.0, min(random.uniform(3, 6), remaining() - 2))
        if scroll_budget <= 0:
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


# ---------------------------------------------------------------------------
# Notification toggle
# ---------------------------------------------------------------------------


def toggle_profile_notifications(
    cdp: CDPConnection, sim: HumanSimulator, enable: bool = True
) -> bool:
    """Toggle notification bell on current profile page. Returns True if successful."""
    # The notification bell is usually accessible via the "More" menu or directly on the profile
    success = cdp.evaluate("""
        (() => {
            // Look for the bell/notification button on profile
            const bellBtn = document.querySelector(
                'button[aria-label*="notification"], ' +
                'button[aria-label*="Notify me"], ' +
                'button[class*="notification"]'
            );
            if (bellBtn) {
                return JSON.stringify({found: true, selector: 'bell'});
            }

            // Try the "More" dropdown first
            const moreBtn = document.querySelector(
                'button[aria-label="More actions"], ' +
                'button[aria-label*="More"]'
            );
            if (moreBtn) {
                return JSON.stringify({found: true, selector: 'more_menu'});
            }

            return JSON.stringify({found: false});
        })()
    """)

    if not success:
        return False

    info = json.loads(success)
    if not info.get("found"):
        return False

    if info["selector"] == "bell":
        return sim.click_element(
            'button[aria-label*="notification"], button[aria-label*="Notify me"]'
        )
    elif info["selector"] == "more_menu":
        # Click More, then find notification option
        sim.click_element('button[aria-label="More actions"], button[aria-label*="More"]')
        human_delay(0.5, 1.5)
        # Look for notification option in dropdown
        return sim.click_element(
            '[class*="dropdown"] button[aria-label*="notification"], '
            '[class*="dropdown"] [data-control-name*="notification"]'
        )

    return False


# ---------------------------------------------------------------------------
# Notification checker
# ---------------------------------------------------------------------------


def check_notifications(cdp: CDPConnection, sim: HumanSimulator) -> dict[str, Any]:
    """Navigate to notifications and extract recent items."""
    cdp.navigate("https://www.linkedin.com/notifications/")
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        return {"error": True, "danger": danger}

    # Scroll a bit
    sim.scroll(random.randint(200, 500))
    human_delay(2, 5)

    notif_data = cdp.evaluate("""
        (() => {
            const items = document.querySelectorAll(
                '.nt-card, [class*="notification-card"], .notification-list-item'
            );
            const notifications = [];
            items.forEach((item, i) => {
                if (i >= 10) return;
                notifications.push({
                    text: item.innerText.substring(0, 200).trim(),
                    index: i,
                });
            });
            return JSON.stringify({
                count: items.length,
                notifications: notifications,
            });
        })()
    """)

    return json.loads(notif_data) if notif_data else {"count": 0, "notifications": []}


def _write_send_diagnostics(
    cdp: CDPConnection,
    profile_url: str,
    stage: str,
    exc: Exception,
    result: dict[str, Any],
) -> str:
    """Persist a diagnostic bundle for unexpected send-connection failures."""
    _ensure_diagnostic_dir()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _safe_slug(profile_url.rstrip("/").split("/")[-1] or "profile")
    base = os.path.join(DIAGNOSTIC_DIR, f"{ts}-{slug}-{_safe_slug(stage)}")

    payload: dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "profile_url": profile_url,
        "error": str(exc),
        "result_so_far": result,
    }

    try:
        payload["current_url"] = cdp.get_current_url()
    except Exception as current_exc:
        payload["current_url_error"] = str(current_exc)

    try:
        payload["page_title"] = cdp.evaluate("document.title") or ""
    except Exception as title_exc:
        payload["page_title_error"] = str(title_exc)

    try:
        payload["page_state"] = cdp.evaluate("""
            (() => JSON.stringify({
                readyState: document.readyState,
                url: window.location.href,
                title: document.title,
                buttons: Array.from(document.querySelectorAll('button')).slice(0, 25).map((btn) => {
                    const rect = btn.getBoundingClientRect();
                    return {
                        text: (btn.innerText || '').trim().slice(0, 80),
                        ariaLabel: btn.getAttribute('aria-label') || '',
                        disabled: !!btn.disabled,
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        visible: !!(rect.width && rect.height)
                    };
                }),
            }))()
        """)
    except Exception as page_exc:
        payload["page_state_error"] = str(page_exc)

    json_path = f"{base}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    try:
        screenshot_b64 = cdp.capture_screenshot_base64()
        if screenshot_b64:
            import base64

            png_path = f"{base}.png"
            with open(png_path, "wb") as fh:
                fh.write(base64.b64decode(screenshot_b64))
            payload["screenshot_path"] = png_path
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
    except Exception:
        pass

    return json_path


# ---------------------------------------------------------------------------
# Session envelope — warm-up and cool-down
# ---------------------------------------------------------------------------


def session_warm_up(cdp: CDPConnection, sim: HumanSimulator, state: dict) -> dict[str, Any]:
    """Execute the session warm-up routine.

    1. Navigate to feed
    2. Content-aware scroll with randomized stop sequence
    3. Maybe check notifications
    4. Return summary of warm-up
    """
    # Navigate to feed
    cdp.navigate("https://www.linkedin.com/feed/")
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        return {"error": True, "danger": danger, "phase": "warm_up"}

    # Generate scroll-stop sequence (unique per session)
    num_stops = random.randint(3, 6)
    sequence = generate_scroll_stop_sequence(num_stops)

    # Record the sequence to state to ensure we don't repeat it
    last_sequence = state.get("last_scroll_sequence", [])
    # Regenerate if somehow identical to yesterday's (unlikely but safe)
    attempts = 0
    while sequence == last_sequence and attempts < 5:
        sequence = generate_scroll_stop_sequence(num_stops)
        attempts += 1

    state["last_scroll_sequence"] = sequence
    save_state(state)

    # Execute feed scroll
    observed = execute_feed_scroll(sim, sequence)

    # Maybe check notifications (30-50% chance)
    checked_notifications = False
    if random.random() < random.uniform(0.3, 0.5):
        check_notifications(cdp, sim)
        checked_notifications = True
        human_delay(2, 5)
        # Navigate back to feed
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(1, 3)

    return {
        "phase": "warm_up",
        "scroll_stops": len(sequence),
        "posts_observed": len(observed),
        "post_types": [p["type"] for p in observed],
        "checked_notifications": checked_notifications,
    }


def session_cool_down(cdp: CDPConnection, sim: HumanSimulator) -> dict[str, Any]:
    """Execute the session cool-down routine."""
    # Return to feed or messaging (randomized)
    if random.random() < 0.7:
        cdp.navigate("https://www.linkedin.com/feed/")
    else:
        cdp.navigate("https://www.linkedin.com/messaging/")
    human_delay(1, 3)

    # Brief scroll or check notification
    if random.random() < 0.5:
        sim.scroll(random.randint(200, 500))
        human_delay(2, 5)
    else:
        # Quick glance at messaging or feed
        human_delay(3, 8)

    # Randomized idle period (never fixed)
    idle_time = human_delay(45, 180)

    return {
        "phase": "cool_down",
        "idle_seconds": round(idle_time, 1),
    }


# ---------------------------------------------------------------------------
# Safety rails — pre-flight checks, quota enforcement, circuit breakers
# ---------------------------------------------------------------------------


def preflight_check(state: dict | None = None) -> dict[str, Any]:
    """Run pre-flight checks before any automation session.

    Returns dict with 'ok' boolean and details.
    """
    if state is None:
        state = load_state()

    results = {
        "ok": True,
        "checks": {},
    }

    # 1. Check Chrome CDP is responsive
    cdp = CDPConnection()
    health = cdp.health_check()
    results["checks"]["chrome_cdp"] = health
    if health["status"] != "ok":
        results["ok"] = False
        results["block_reason"] = f"Chrome CDP not responding on port {cdp.port}"
        return results

    # 2. Connect and check LinkedIn session
    try:
        cdp.connect()
        # Preflight must be quick and bounded.  A Chrome page-load event is
        # not reliable enough to gate the entire activity workflow: when it
        # never arrives, the old watcher could sit here for hours before it
        # ever opened a target profile.
        inject_stealth(cdp)
        page = detect_page(cdp, timeout=10)

        # CDP being reachable is not proof that LinkedIn is available. A newly
        # launched automation profile opens on Chrome's New Tab page, which
        # used to pass preflight and leave the first real workflow navigation
        # as the point of failure. Open LinkedIn deliberately and verify the
        # resulting page before allowing any workflow to proceed.
        # A profile reader can legitimately leave the active tab on LinkedIn's
        # 404 page after quarantining an invalid profile.  That is target-level
        # state, not evidence that the signed-in account or CDP lane is unsafe.
        # Re-establish a neutral LinkedIn page before applying the account-level
        # danger checks.  Login, checkpoint, captcha and restriction pages must
        # still block immediately and are intentionally not navigated away from.
        if page.get("page_type") in {"unknown", "not_found"}:
            cdp.navigate("https://www.linkedin.com/feed/", wait_load=False, timeout=8)
            time.sleep(2)
            page = detect_page(cdp, timeout=10)
        results["checks"]["linkedin_session"] = page

        if page.get("is_danger"):
            results["ok"] = False
            results["block_reason"] = f"Danger detected: {page.get('danger_type')}"
            cdp.disconnect()
            return results

        if page["page_type"] == "login":
            results["ok"] = False
            results["block_reason"] = "LinkedIn is logged out — manual login required"
            cdp.disconnect()
            return results

        if page.get("page_type") == "unknown":
            results["ok"] = False
            results["block_reason"] = (
                "LinkedIn session could not be verified after opening LinkedIn"
            )
            cdp.disconnect()
            return results

        cdp.disconnect()
    except Exception as e:
        results["ok"] = False
        results["block_reason"] = f"CDP connection failed: {e}"
        return results

    # 3. Check daily quotas
    conn_req_today = get_counter(state, "conn_req_sent")
    conn_req_week = get_weekly_counter(state, "conn_req_sent")
    profile_views_today = get_counter(state, "profile_views")

    results["checks"]["quotas"] = {
        "conn_req_today": conn_req_today,
        "conn_req_limit": MAX_CONN_REQ_PER_DAY,
        "conn_req_week": conn_req_week,
        "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
        "profile_views_today": profile_views_today,
        "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
    }

    if conn_req_today >= MAX_CONN_REQ_PER_DAY:
        results["checks"]["quotas"]["conn_req_exhausted"] = True
    if conn_req_week >= MAX_CONN_REQ_PER_WEEK:
        results["checks"]["quotas"]["conn_req_week_exhausted"] = True
        results["ok"] = False
        results["block_reason"] = "Weekly connection request limit reached"
    if profile_views_today >= MAX_PROFILE_VIEWS_PER_DAY:
        results["checks"]["quotas"]["profile_views_exhausted"] = True

    # 4. Check acceptance rate from sheet sources of truth.
    sheet_sent = {"ok": False, "source": "op_bruteforce_outreach_log"}
    if count_outreach_log_connection_requests is None:
        sheet_sent["error"] = "Outreach Log sent helper unavailable"
    else:
        try:
            sheet_sent = count_outreach_log_connection_requests()
        except Exception as e:
            sheet_sent["error"] = str(e)

    if not sheet_sent.get("ok"):
        results["ok"] = False
        results["block_reason"] = (
            "Unable to verify sent connection requests from Op Bruteforce Outreach Log: "
            f"{sheet_sent.get('error', 'unknown error')}"
        )
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": "op_bruteforce_pipeline",
            "sent": None,
            "accepted": None,
            "error": sheet_sent.get("error", "unknown error"),
        }
        return results

    sent_count = int(sheet_sent.get("sent", 0) or 0)
    if sent_count >= 10:  # Only check with sufficient sample
        pipeline_acceptance = {"ok": False, "source": "op_bruteforce_pipeline"}
        if count_pipeline_connected_leads is None:
            pipeline_acceptance["error"] = "Pipeline acceptance helper unavailable"
        else:
            try:
                pipeline_acceptance = count_pipeline_connected_leads()
            except Exception as e:
                pipeline_acceptance["error"] = str(e)

        if not pipeline_acceptance.get("ok"):
            results["ok"] = False
            results["block_reason"] = (
                "Unable to verify acceptance rate from Op Bruteforce Pipeline: "
                f"{pipeline_acceptance.get('error', 'unknown error')}"
            )
            results["checks"]["acceptance_rate"] = {
                "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
                "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
                "sent": sent_count,
                "accepted": None,
                "error": pipeline_acceptance.get("error", "unknown error"),
            }
            return results

        pipeline_connected = int(pipeline_acceptance.get("connected", 0) or 0)
        accepted_count = min(pipeline_connected, sent_count)
        acceptance_rate = accepted_count / sent_count
        results["checks"]["acceptance_rate"] = {
            "sent_source": sheet_sent.get("source", "op_bruteforce_outreach_log"),
            "accepted_source": pipeline_acceptance.get("source", "op_bruteforce_pipeline"),
            "rate": round(acceptance_rate, 3),
            "sent": sent_count,
            "accepted": accepted_count,
            "outreach_log_sent": sent_count,
            "outreach_log_sent_rows": sheet_sent.get("sent_rows"),
            "pipeline_connected": pipeline_connected,
            "pipeline_connected_rows": pipeline_acceptance.get("connected_rows"),
        }
        if acceptance_rate < ACCEPTANCE_RATE_CRITICAL:
            results["ok"] = False
            results["block_reason"] = (
                f"Acceptance rate critically low ({acceptance_rate:.1%}). "
                "Connection requests paused until manual override."
            )
        elif acceptance_rate < ACCEPTANCE_RATE_WARN:
            results["checks"]["acceptance_rate"]["warning"] = True

    return results


# ---------------------------------------------------------------------------
# Phase 3: Connection request engine + engagement scanner
# ---------------------------------------------------------------------------


def send_connection_request(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
    note: str | None = None,
    enable_notifications: bool = False,
    profile_data: dict[str, Any] | None = None,
    activity_data: dict[str, Any] | None = None,
    verify_connection_modal_only: bool = False,
) -> dict[str, Any]:
    """Send a connection request only; activity timing is owned by the runner."""
    result = {
        "action": "send_connection_request",
        "profile_url": profile_url,
        "success": False,
    }
    stage = "start"

    try:
        # --- Quota gate ---
        conn_today = get_counter(state, "conn_req_sent")
        conn_week = get_weekly_counter(state, "conn_req_sent")
        views_today = get_counter(state, "profile_views")

        if conn_today >= MAX_CONN_REQ_PER_DAY:
            result["error"] = "daily_conn_req_limit"
            return result
        if conn_week >= MAX_CONN_REQ_PER_WEEK:
            result["error"] = "weekly_conn_req_limit"
            return result
        if views_today >= MAX_PROFILE_VIEWS_PER_DAY:
            result["error"] = "daily_profile_view_limit"
            return result

        # --- Step 1: Inspect profile action state ---
        stage = "inspect_profile"
        if profile_data is None:
            profile_data = view_profile(cdp, sim, profile_url)
            increment_counter(state, "profile_views")

        if profile_data.get("error"):
            result["error"] = profile_data.get("danger", "profile_view_failed")
            return result

        result["profile"] = profile_data
        action_state = str(profile_data.get("action_state") or "").strip()

        # Already connected or pending?
        if profile_data.get("is_connected") or action_state == "already_connected":
            result["error"] = "already_connected"
            return result
        if profile_data.get("is_pending") or action_state == "already_pending":
            result["error"] = "already_pending"
            return result
        if action_state in {"profile_unavailable", "unknown"}:
            result["error"] = action_state
            return result
        if not profile_data.get("has_connect_button") and action_state not in {
            "connect_direct",
            "connect_in_more",
        }:
            result["error"] = "no_connect_button"
            result["detail"] = "Profile may require InMail or Follow only"
            return result

        stage = "return_to_profile"
        cdp.navigate(profile_url)
        _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_ACTION_READY_SELECTOR,
            timeout=30,
            stable_for=0.8,
            ignored_overlays={".authentication-outlet"},
        )
        human_delay(1, 2)

        # --- Step 2: Circuit breaker re-check ---
        stage = "check_circuit_breakers"
        danger = check_circuit_breakers(cdp)
        if danger:
            result["error"] = danger
            return result

        # --- Step 3: Click Connect ---
        stage = "click_connect"
        ready_state = _wait_for_linkedin_ready(
            cdp,
            expected_selector=PROFILE_ACTION_READY_SELECTOR,
            timeout=45,
            stable_for=1.0,
            ignored_overlays={".authentication-outlet"},
        )
        result["connect_ready_state"] = ready_state
        if not ready_state.get("ready"):
            result["error"] = "connect_page_not_ready"
            return result
        human_delay(1, 3, distribution="gaussian")

        connect_clicked = _click_connect_button(cdp, sim, action_state=action_state)

        if not connect_clicked:
            result["error"] = "connect_button_not_clickable"
            return result

        human_delay(1, 2)

        # --- Step 4: Handle the connection modal ---
        stage = "handle_note_modal"
        if note:
            add_note_clicked = _click_add_note_button(cdp, sim)

            if add_note_clicked:
                human_delay(0.5, 1.5)
                _type_connection_note(cdp, sim, note[:300])
                human_delay(0.5, 1.5)
            else:
                result["note_skipped"] = True
                result["note_skip_reason"] = "add_note_button_not_found"

        # --- Step 5: Click Send ---
        stage = "click_send"
        modal_ready = _wait_for_connect_modal(cdp, timeout=30)
        modal_state = _inspect_connect_modal(cdp)
        result["send_modal_ready_state"] = {"ready": modal_ready, **modal_state}
        if not modal_ready:
            _dismiss_connect_modal(cdp, sim)
            result["error"] = "send_modal_not_ready"
            return result
        if modal_state.get("emailRequired"):
            _dismiss_connect_modal(cdp, sim)
            result["error"] = "email_required_to_connect"
            result["detail"] = (
                "LinkedIn requires the member's email before sending this invitation."
            )
            return result

        if verify_connection_modal_only:
            # Follow the production send route through its final checkpoint;
            # replace only the Send click with a confirmed dismissal.
            result["modal_opened"] = True
            result["send_without_note_found"] = isinstance(modal_state.get("sendWithoutNote"), dict)
            result["send_without_note_disabled"] = bool(
                isinstance(modal_state.get("sendWithoutNote"), dict)
                and modal_state["sendWithoutNote"].get("disabled")
            )
            result["email_required"] = False
            result["closed"] = _dismiss_connect_modal(cdp, sim)
            result["success"] = bool(result["closed"])
            if not result["success"]:
                result["error"] = "connection_modal_not_dismissed"
            return result

        human_delay(0.5, 1.5, distribution="gaussian")
        send_clicked = _click_send_button(cdp, sim)

        if not send_clicked:
            _dismiss_connect_modal(cdp, sim)
            send_without_note = modal_state.get("sendWithoutNote")
            if isinstance(send_without_note, dict) and send_without_note.get("disabled"):
                result["error"] = "send_without_note_disabled"
            else:
                result["error"] = "send_button_not_clickable"
            return result

        human_delay(1, 3)

        # --- Step 6: Verify success ---
        stage = "verify_success"
        _wait_for_linkedin_ready(
            cdp,
            expected_selector="body",
            timeout=20,
            stable_for=0.8,
            ignored_overlays={".authentication-outlet"},
        )
        verification: dict[str, Any] = {"pending": False, "state": None, "attempts": []}
        for _attempt in range(3):
            live_state = inspect_profile_action_state(cdp, profile_url)
            state_name = str(live_state.get("state") or "").strip()
            verification["attempts"].append(
                {
                    "state": state_name,
                    "profileName": live_state.get("profileName"),
                    "url": live_state.get("url"),
                }
            )
            if state_name == "already_pending":
                verification["pending"] = True
                verification["state"] = state_name
                break
            human_delay(2, 4)

        result["verification"] = verification
        result["success"] = bool(verification.get("pending"))
        result["verified_pending"] = bool(verification.get("pending"))
        result["note_sent"] = bool(note) and not result.get("note_skipped")
        if not result["success"]:
            result["error"] = "send_unverified"

        if result["success"]:
            increment_counter(state, "conn_req_sent")

        return result
    except Exception as exc:
        diagnostics_path = _write_send_diagnostics(cdp, profile_url, stage, exc, result)
        raise RuntimeError(f"{exc} | diagnostics={diagnostics_path}")


def send_connection_only(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
    profile_state: dict[str, Any] | None = None,
    note: str | None = None,
    verify_connection_modal_only: bool = False,
) -> dict[str, Any]:
    """Explicit send-only interface used by the outreach state machine."""
    profile_data = profile_state
    if profile_data and "action_state" not in profile_data:
        profile_data = {
            "name": str(profile_data.get("profileName", "")).strip(),
            "has_connect_button": profile_data.get("state")
            in {"connect_direct", "connect_in_more"},
            "is_pending": profile_data.get("state") == "already_pending",
            "is_connected": profile_data.get("state") == "already_connected",
            "action_state": profile_data.get("state"),
            "inspection": profile_data,
        }
    return send_connection_request(
        cdp,
        sim,
        state,
        profile_url,
        note=note,
        enable_notifications=False,
        profile_data=profile_data,
        activity_data={},
        verify_connection_modal_only=verify_connection_modal_only,
    )


def verify_no_note_send_ui(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict[str, Any],
    profile_url: str,
    profile_state: dict[str, Any],
) -> dict[str, Any]:
    """Use the production send path through the modal, then dismiss it safely."""
    action_state = str(profile_state.get("state") or "").strip()
    profile_data = {
        "name": str(profile_state.get("profileName", "")).strip(),
        "has_connect_button": action_state in {"connect_direct", "connect_in_more"},
        "is_pending": action_state == "already_pending",
        "is_connected": action_state == "already_connected",
        "action_state": action_state,
        "inspection": profile_state,
    }
    send_path = send_connection_request(
        cdp,
        sim,
        state,
        profile_url,
        note=None,
        enable_notifications=False,
        profile_data=profile_data,
        activity_data={},
        verify_connection_modal_only=True,
    )
    return {
        "ok": bool(send_path.get("success")),
        "profile_url": profile_url,
        "action_state": action_state,
        "modal_opened": bool(send_path.get("modal_opened")),
        "send_without_note_found": bool(send_path.get("send_without_note_found")),
        "send_without_note_disabled": send_path.get("send_without_note_disabled"),
        "email_required": bool(send_path.get("email_required")),
        "closed": bool(send_path.get("closed")),
        "modal_state": send_path.get("send_modal_ready_state"),
        **({"error": send_path["error"]} if send_path.get("error") else {}),
    }


def _inspect_connect_modal(cdp: CDPConnection) -> dict[str, Any]:
    """Inspect LinkedIn's invite modal, including open shadow-root render paths."""
    raw = cdp.evaluate(
        """
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const modalRoots = deepNodes.filter((el) => {
                const id = el.getAttribute('data-test-modal-id') || '';
                const role = el.getAttribute('role') || '';
                const text = norm(el.innerText || el.textContent || '');
                return id === 'send-invite-modal' ||
                    (role === 'dialog' && /add a note to your invitation\\?/i.test(text));
            }).filter(visible);
            const scope = modalRoots[modalRoots.length - 1] || null;
            const modalText = norm((scope && (scope.innerText || scope.textContent)) || '');
            const buttons = (scope ?
                Array.from(scope.querySelectorAll('button, [role="button"]')) :
                deepNodes.filter((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    return tag === 'button' || el.getAttribute('role') === 'button';
                })
            ).filter(visible);
            const serializeButton = (btn) => btn ? {
                tag: (btn.tagName || '').toLowerCase(),
                text: norm(btn.innerText || btn.textContent || ''),
                ariaLabel: norm(btn.getAttribute('aria-label') || ''),
                disabled: !!btn.disabled || btn.getAttribute('aria-disabled') === 'true',
            } : null;
            const addNote = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label.toLowerCase() === 'add a note';
            }) || null;
            const sendWithoutNote = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label.toLowerCase() === 'send without a note';
            }) || null;
            const likelySend = buttons.find((btn) => {
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent).toLowerCase();
                return label === 'send' || label === 'send now' || label.startsWith('send ');
            }) || null;
            const emailInputs = (scope ?
                Array.from(scope.querySelectorAll('input, textarea')) :
                deepNodes.filter((el) => ['input', 'textarea'].includes((el.tagName || '').toLowerCase()))
            ).filter(visible).filter((el) => {
                const type = norm(el.getAttribute('type') || '').toLowerCase();
                const label = norm([
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('placeholder') || '',
                    el.getAttribute('name') || '',
                    el.getAttribute('id') || '',
                ].join(' ')).toLowerCase();
                return type === 'email' || label.includes('email');
            });
            const emailRequired = /enter (their|this member'?s|the member'?s)?\\s*email to connect/i.test(modalText) ||
                /verify this member knows you/i.test(modalText) ||
                emailInputs.length > 0;
            return JSON.stringify({
                modalRootFound: !!scope,
                dialogFound: !!modalRoots.find((el) => el.getAttribute('role') === 'dialog'),
                modalTextSample: modalText.slice(0, 500),
                emailRequired,
                emailInputCount: emailInputs.length,
                addNote: serializeButton(addNote),
                sendWithoutNote: serializeButton(sendWithoutNote),
                likelySend: serializeButton(likelySend),
                buttonCount: buttons.length,
            });
        })()
    """,
        timeout=5,
    )
    return json.loads(raw) if raw else {"modalRootFound": False, "dialogFound": False}


def _dismiss_connect_modal(cdp: CDPConnection, sim: HumanSimulator | None = None) -> bool:
    """Dismiss the invite modal and confirm that it is no longer visible."""

    def modal_is_open() -> bool:
        try:
            state = _inspect_connect_modal(cdp)
            return bool(state.get("modalRootFound"))
        except Exception:
            # Failure to inspect must never be interpreted as confirmed closed.
            return True

    def wait_until_closed(timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not modal_is_open():
                return True
            time.sleep(0.15)
        return not modal_is_open()

    if not modal_is_open():
        return True

    try:
        clicked = cdp.evaluate(
            """
            (() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const deepNodes = [];
                const walk = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    for (const el of root.querySelectorAll('*')) {
                        deepNodes.push(el);
                        if (el.shadowRoot) walk(el.shadowRoot);
                    }
                };
                walk(document);
                const modalRoots = deepNodes.filter((el) => {
                    const id = el.getAttribute('data-test-modal-id') || '';
                    const role = el.getAttribute('role') || '';
                    const text = norm(el.innerText || el.textContent || '');
                    return visible(el) && (id === 'send-invite-modal' ||
                        (role === 'dialog' && (text.includes('invitation') ||
                            text.includes('send without a note'))));
                });
                const scope = modalRoots[modalRoots.length - 1] || null;
                if (!scope) return false;
                const close = Array.from(scope.querySelectorAll('button, [role="button"]')).find((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag !== 'button' && el.getAttribute('role') !== 'button') return false;
                    if (!visible(el)) return false;
                    const label = norm([
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.innerText || el.textContent || '',
                    ].join(' '));
                    return /(^|\\s)(dismiss|close)(\\s|$)/.test(label);
                });
                if (!close) return false;
                close.click();
                return true;
            })()
        """,
            timeout=5,
        )
        if bool(clicked) and wait_until_closed():
            return True
    except Exception:
        pass
    if sim:
        try:
            clicked = sim.click_element(
                '[data-test-modal-id="send-invite-modal"] button[aria-label="Dismiss"], '
                '[data-test-modal-id="send-invite-modal"] button[aria-label="Close"], '
                '[role="dialog"] button[aria-label="Dismiss"], '
                '[role="dialog"] button[aria-label="Close"]'
            )
            if clicked and wait_until_closed():
                return True
        except Exception:
            pass

    # Escape is a safe modal-dismiss fallback and cannot activate the Send
    # control.  Confirm closure after dispatching it rather than trusting the
    # key event itself.
    try:
        cdp.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        cdp.send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        return wait_until_closed()
    except Exception:
        return False


def _wait_for_connect_modal(cdp: CDPConnection, timeout: float = 3.0) -> bool:
    """Poll for the connect modal (a dialog containing a Send button) to appear after clicking Connect."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            modal = _inspect_connect_modal(cdp)
            if modal.get("sendWithoutNote") or modal.get("likelySend"):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _click_connect_button(
    cdp: CDPConnection,
    sim: HumanSimulator,
    action_state: str = "",
    modal_timeout: float = 30.0,
) -> bool:
    """Click Connect from the profile top-card only.

    Never search the whole document: LinkedIn can render People You May Know
    cards below the profile, and those cards also contain Connect buttons.
    """

    def attempt_click() -> bool:
        if action_state == "connect_in_more":
            more_result = _open_more_and_click_connect(cdp)
            return bool(more_result.get("connect_clicked"))

        # Direct top-card path only. Verified empirically to fire LinkedIn's
        # React handler; CDP mouse events were silently no-op'ing here.
        direct_clicked = cdp.evaluate("""
            (() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden';
                };
                const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const h1 = document.querySelector('h1');
                const topCard =
                    document.querySelector('[componentkey*="profile.card"][componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="profile.card"][componentkey*="topcard"]') ||
                    document.querySelector('[componentkey*="Topcard"]') ||
                    document.querySelector('[componentkey*="topcard"]') ||
                    h1?.closest('section') ||
                    h1?.closest('.artdeco-card') ||
                    document.querySelector('.pv-top-card');
                if (!topCard) return false;
                const nodes = Array.from(topCard.querySelectorAll('button, [role="button"], a'))
                    .filter((node) => !node.closest('.artdeco-dropdown__content'));
                for (const node of nodes) {
                    if (!visible(node)) continue;
                    const text = norm(node.innerText || node.textContent);
                    const aria = norm(node.getAttribute('aria-label'));
                    const isConnectText = text === 'connect';
                    const isConnectAria = /^invite\\b.*\\bto connect\\b/i.test(aria);
                    const componentKey = node.getAttribute('componentkey') || '';
                    const href = node.getAttribute('href') || '';
                    const isConnectComponent =
                        componentKey.startsWith('ConnectButtonstate:invitation:') &&
                        (componentKey.endsWith('_connect') || href.startsWith('/preload/custom-invite/'));
                    if (!isConnectText && !isConnectAria && !isConnectComponent) continue;
                    node.click();
                    return true;
                }
                return false;
            })()
        """)
        return bool(direct_clicked)

    if not attempt_click():
        return False
    # LinkedIn's invitation dialog is sometimes rendered asynchronously after
    # the profile action has accepted the click.  Returning false after the
    # old three-second probe left a real, late-opening modal on screen and
    # made both production sends and no-send checks misreport the action.
    if _wait_for_connect_modal(cdp, timeout=modal_timeout):
        return True
    # Click fired but no modal appeared. Retry once.
    if not attempt_click():
        return False
    return _wait_for_connect_modal(cdp, timeout=modal_timeout)


def _click_add_note_button(cdp: CDPConnection, sim: HumanSimulator) -> bool:
    """Click the 'Add a note' button in the connection request modal."""
    clicked_shadow = cdp.evaluate("""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const btn = deepNodes.find((node) => {
                const tag = (node.tagName || '').toLowerCase();
                if (tag !== 'button' && node.getAttribute('role') !== 'button') return false;
                if (!visible(node)) return false;
                const label = norm(node.getAttribute('aria-label') || node.innerText || node.textContent);
                return label === 'add a note';
            });
            if (!btn) return false;
            btn.click();
            return true;
        })()
    """)
    if bool(clicked_shadow):
        return True

    if sim.click_element(
        'button[aria-label*="Add a note"], '
        'button[aria-label*="add a note"], '
        'button[data-control-name*="add_note"], '
        'button[data-control-name*="invite"], '
        "button.artdeco-button--secondary"
    ):
        return True

    # Fallback: find a visible button by text and click it directly.
    clicked = cdp.evaluate("""
        (() => {
            const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const node of nodes) {
                const text = (node.innerText || node.textContent || '').trim().toLowerCase();
                if (!text.includes('add a note')) continue;
                const rect = node.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                node.click();
                return true;
            }
            return false;
        })()
    """)
    return bool(clicked)


def _type_connection_note(cdp: CDPConnection, sim: HumanSimulator, note: str):
    """Type a connection note into the modal textarea."""
    # Find and focus the textarea
    focused = cdp.evaluate("""
        (() => {
            const ta = document.querySelector(
                'textarea[name="message"], textarea#custom-message, ' +
                'textarea[placeholder*="Add a note"], textarea.connect-button-send-invite__custom-message'
            );
            if (ta) { ta.focus(); ta.value = ''; return true; }
            return false;
        })()
    """)
    if focused:
        human_delay(0.3, 0.8)
        sim.type_text(note)


def _click_send_button(cdp: CDPConnection, sim: HumanSimulator) -> bool:
    """Click the Send / Send now button in the connection modal."""
    # Only click inside the visible invite modal. A global primary-button selector
    # can hit unrelated page controls and create a false "sent" path.
    clicked = cdp.evaluate("""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const deepNodes = [];
            const walk = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    deepNodes.push(el);
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
            };
            walk(document);
            const modalRoots = deepNodes.filter((el) => {
                const id = el.getAttribute('data-test-modal-id') || '';
                const role = el.getAttribute('role') || '';
                const text = norm(el.innerText || el.textContent || '');
                return id === 'send-invite-modal' ||
                    (role === 'dialog' && text.includes('add a note to your invitation'));
            }).filter(visible);
            const scope = modalRoots[modalRoots.length - 1] || null;
            const buttons = (scope ?
                Array.from(scope.querySelectorAll('button, [role="button"]')) :
                deepNodes.filter((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    return tag === 'button' || el.getAttribute('role') === 'button';
                })
            ).filter(visible);
            const preferred = buttons.find((btn) => {
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return false;
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                return label === 'send without a note';
            });
            if (preferred) {
                preferred.click();
                return true;
            }
            for (const btn of buttons) {
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                const label = norm(btn.getAttribute('aria-label') || btn.innerText || btn.textContent);
                if (!(label === 'send' || label === 'send now' || label.startsWith('send '))) continue;
                const rect = btn.getBoundingClientRect();
                if (!rect.width || !rect.height) continue;
                btn.click();
                return true;
            }
            return false;
        })()
    """)
    return bool(clicked)


# ---------------------------------------------------------------------------
# Engagement actions (Approaches A-E)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sent-invitations scraper & subtractive acceptance detection
# ---------------------------------------------------------------------------


def _normalize_linkedin_profile_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    normalized = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    return re.sub(r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", normalized)


def _normalize_sent_invitation_name(name: str) -> str:
    lowered = str(name or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


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


def verify_acceptance_via_profile(
    cdp: CDPConnection,
    sim: HumanSimulator,
    profile_url: str,
) -> dict[str, Any]:
    """Visit a profile to verify whether a connection was accepted, declined, or expired.

    Uses inspect_profile_action_state which is already part of view_profile.
    Returns:
    - status: 'accepted', 'declined_or_expired', 'still_pending', 'unknown'
    - action_state: raw state from inspect_profile_action_state
    """
    result: dict[str, Any] = {
        "action": "verify_acceptance",
        "profile_url": profile_url,
        "action_state": "",
        "profile_name": "",
    }

    nav = _navigate_with_readiness(
        cdp,
        profile_url,
        nav_timeout=30,
        ready_timeout=15,
    )

    if not nav.get("ready"):
        result["status"] = "unknown"
        result["error"] = (
            f"page_not_ready|readyState={nav.get('readyState', '?')}"
            f"|overlay={nav.get('overlay_detected', '?')}"
            f"|loading={nav.get('loading_detected', '?')}"
            f"|timeout={nav.get('timeout', '?')}"
            f"|url={nav.get('url', '?')}"
        )
        return result

    human_delay(2, 3)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["status"] = "unknown"
        result["error"] = f"circuit_breaker:{danger}"
        return result

    inspected = inspect_profile_action_state(cdp, profile_url)
    action_state = inspected.get("state", "unknown")
    result["action_state"] = action_state
    result["profile_name"] = inspected.get("profileName", "")

    if action_state == "already_connected":
        result["status"] = "accepted"
    elif action_state == "already_pending":
        result["status"] = "still_pending"
    elif action_state in ("connect_direct", "connect_in_more"):
        result["status"] = "declined_or_expired"
    elif action_state == "no_connect_button":
        # Profile visible but no connect/pending button — likely accepted (1st degree)
        # or profile has restricted connection options
        result["status"] = "accepted"
        result["note"] = "inferred_from_no_connect_button"
    elif action_state == "profile_unavailable":
        result["status"] = "declined_or_expired"
        result["note"] = "profile_unavailable"
    else:
        result["status"] = "unknown"

    # Brief natural scroll to look human
    _safe_scroll_or_js(cdp, sim, random.randint(100, 250))
    human_delay(1, 2)

    return result


def check_acceptances_subtractive(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    pending_prospects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect new acceptances by comparing pending prospects against sent invitations.

    Subtractive strategy:
    1. Scrape sent invitations page for all currently pending invites
    2. For each prospect in pending_prospects, check if they're still on the sent page
    3. If missing from sent page, visit their profile to verify acceptance vs decline
    4. Return categorized results

    Args:
        pending_prospects: list of prospect dicts from the sheet, each must have
            'contact_linkedin' (URL) and 'contact_name' fields.

    Returns dict with acceptances, declines, still_pending, and verification details.
    """
    result: dict[str, Any] = {
        "action": "check_acceptances_subtractive",
        "acceptances": [],
        "declines": [],
        "still_pending": [],
        "errors": [],
        "sent_invitations_count": 0,
    }

    if not pending_prospects:
        result["status"] = "no_pending_prospects"
        return result

    # Step 1: Scrape sent invitations page
    sent = scrape_sent_invitations(cdp, sim)
    if sent.get("error"):
        result["error"] = sent["error"]
        return result

    sent_urls = set()
    sent_names: dict[str, list[dict[str, Any]]] = {}
    for inv in sent.get("invitations", []):
        url = _normalize_linkedin_profile_url(inv.get("url", ""))
        if url:
            sent_urls.add(url)
        name_key = _normalize_sent_invitation_name(inv.get("name", ""))
        if name_key:
            sent_names.setdefault(name_key, []).append(inv)

    result["sent_invitations_count"] = sent.get("count", len(sent.get("invitations", [])))
    result["sent_invitations_url_count"] = len(sent_urls)
    result["sent_invitations"] = sent.get("invitations", [])
    result["sent_invitations_scroll_snapshots"] = sent.get("scroll_snapshots", [])

    # Step 2: Compare each pending prospect against sent page
    missing_from_sent: list[dict[str, Any]] = []
    for prospect in pending_prospects:
        prospect_url = _normalize_linkedin_profile_url(prospect.get("contact_linkedin") or "")
        prospect_name_key = _normalize_sent_invitation_name(prospect.get("contact_name", ""))
        if not prospect_url:
            result["errors"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "error": "no_linkedin_url_in_sheet",
                }
            )
            continue

        if prospect_url in sent_urls:
            result["still_pending"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "url": prospect_url,
                    "status": "still_on_sent_page",
                }
            )
        elif prospect_name_key and len(sent_names.get(prospect_name_key, [])) == 1:
            result["still_pending"].append(
                {
                    "contact_name": prospect.get("contact_name", ""),
                    "url": prospect_url,
                    "status": "still_on_sent_page_name_match",
                    "sent_page_name": sent_names[prospect_name_key][0].get("name", ""),
                }
            )
        else:
            missing_from_sent.append(prospect)

    # Step 3: Verify missing prospects via profile visit
    result["missing_from_sent_count"] = len(missing_from_sent)
    for prospect in missing_from_sent:
        prospect_url = (prospect.get("contact_linkedin") or "").strip()
        contact_name = prospect.get("contact_name", "")
        human_delay(2, 4)  # Pace between profile visits

        verification = None
        last_error = None
        for attempt in range(2):  # Retry once on unknown/error
            try:
                verification = verify_acceptance_via_profile(cdp, sim, prospect_url)
            except Exception as exc:
                last_error = str(exc)
                verification = None
                if attempt == 0:
                    human_delay(3, 5)
                continue

            status = verification.get("status", "unknown")
            if status != "unknown":
                break  # Got a definitive answer
            # First attempt returned unknown — retry after a pause
            if attempt == 0:
                human_delay(3, 5)

        if verification is None:
            result["errors"].append(
                {
                    "contact_name": contact_name,
                    "url": prospect_url,
                    "error": last_error or "verification_failed_after_retries",
                }
            )
            continue

        status = verification.get("status", "unknown")
        entry = {
            "name": verification.get("profile_name") or contact_name,
            "url": prospect_url.rstrip("/").lower().split("?")[0],
            "source": "sent_page_subtractive",
            "verification": status,
            "action_state": verification.get("action_state", ""),
        }

        if status == "accepted":
            result["acceptances"].append(entry)
        elif status == "declined_or_expired":
            result["declines"].append(entry)
        elif status == "still_pending":
            # Was missing from sent page but profile says pending — possible pagination miss
            result["still_pending"].append(
                {
                    **entry,
                    "note": "missing_from_sent_but_profile_shows_pending",
                }
            )
        else:
            verify_error = verification.get("error", "")
            result["errors"].append(
                {
                    **entry,
                    "error": f"unknown_status_after_retry:{status}|detail={verify_error}",
                }
            )

    return result


def check_acceptances(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
) -> dict[str, Any]:
    """Scan My Network for new connection acceptances.

    Used in the 10:00 AM acceptance check session.
    Returns list of newly accepted connections.

    Hardened path: uses JS navigation with readiness gates and automatic
    retry/fallback so this works reliably in unattended/background-window
    scenarios. Human-sim mouse events are attempted but not required.
    """
    result: dict[str, Any] = {"action": "check_acceptances", "acceptances": []}

    # --- Navigate with readiness gate ---
    CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
    # Selector covers current (LazyColumn) and legacy LinkedIn connection layouts
    EXPECTED_SELECTOR = (
        '[data-component-type="LazyColumn"], .mn-connection-card, [class*="connection-card"]'
    )

    nav = _navigate_with_readiness(
        cdp,
        CONNECTIONS_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        # One retry: sometimes LinkedIn redirects through an interstitial
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            CONNECTIONS_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Page not ready after navigation: "
            f"readyState={nav.get('readyState')}, "
            f"overlay={nav.get('overlay_detected')}, "
            f"selector_found={nav.get('selector_found')}"
        )
        return result

    human_delay(2, 4)

    # --- Circuit breaker check ---
    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # --- Scroll to trigger lazy-loading (reliability > human-likeness) ---
    scroll_info = _safe_scroll_or_js(cdp, sim, random.randint(200, 500))
    result["scroll"] = scroll_info
    human_delay(2, 4)

    # --- Extract recent connections (with retry) ---
    conn_data = _extract_connections_dom(cdp)
    if not conn_data:
        # Retry once after a short wait — page may still be hydrating
        human_delay(2, 3)
        conn_data = _extract_connections_dom(cdp)

    connections = json.loads(conn_data) if conn_data else []

    # Filter for recent connections (today / yesterday)
    today = date.today()
    recent = []
    for conn in connections:
        time_text = conn.get("time_text", "")
        is_recent = any(
            t in time_text
            for t in [
                "just now",
                "today",
                "hour",
                "minute",
                "1 day",
                "yesterday",
                "1d",
                "2d",
                "1h",
                "2h",
                "3h",
            ]
        )

        # Also match "connected on <date>" format (e.g. "connected on april 27, 2026")
        if not is_recent and "connected on" in time_text:
            # Try to parse the date from the text
            try:
                date_part = time_text.split("connected on")[-1].strip().rstrip(".")
                # Handle formats like "april 27, 2026"
                conn_date = datetime.strptime(date_part, "%B %d, %Y").date()
                days_ago = (today - conn_date).days
                if days_ago <= 2:  # Today or yesterday (with buffer)
                    is_recent = True
                    conn["_parsed_days_ago"] = days_ago
            except (ValueError, TypeError):
                pass  # Unparseable date — fall through to index check

        if is_recent or conn["index"] < 5:  # Top 5 are newest
            recent.append(conn)

    result["acceptances"] = recent
    result["total_connections_visible"] = len(connections)

    return result


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


# ---------------------------------------------------------------------------
# Withdrawal manager — withdraw stale pending connection requests
# ---------------------------------------------------------------------------


def withdraw_connection(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    profile_url: str,
) -> dict[str, Any]:
    """Withdraw a pending connection request from a prospect's profile.

    Sequence:
    1. Navigate to profile
    2. Verify the request is still pending
    3. Click "Pending" → "Withdraw"
    4. Confirm withdrawal

    Returns result dict with success status.
    """
    result = {
        "action": "withdraw_connection",
        "profile_url": profile_url,
        "success": False,
    }

    # Check daily withdrawal quota
    withdrawals_today = get_counter(state, "withdrawals")
    if withdrawals_today >= MAX_WITHDRAWALS_PER_DAY:
        result["error"] = "daily_withdrawal_limit"
        return result

    # Navigate to profile
    cdp.navigate(profile_url)
    human_delay(2, 4)

    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # Brief natural scroll (don't go deep — just enough to look human)
    sim.scroll(random.randint(100, 300))
    human_delay(2, 5, distribution="gaussian")

    # Check if Pending button exists
    pending_info = cdp.evaluate("""
        (() => {
            const pendingBtn = document.querySelector(
                'button[aria-label*="Pending"], ' +
                'button[class*="pending"]'
            );
            if (pendingBtn) {
                return JSON.stringify({
                    found: true,
                    label: pendingBtn.getAttribute('aria-label') || '',
                });
            }

            // Check if already connected (no withdrawal needed)
            const msgBtn = document.querySelector('button[aria-label*="Message"]');
            const connectBtn = document.querySelector('button[aria-label*="Connect"]');
            if (msgBtn && !connectBtn) {
                return JSON.stringify({found: false, reason: 'already_connected'});
            }
            if (connectBtn) {
                return JSON.stringify({found: false, reason: 'not_pending'});
            }

            return JSON.stringify({found: false, reason: 'button_not_found'});
        })()
    """)

    info = json.loads(pending_info) if pending_info else {}

    if not info.get("found"):
        result["error"] = info.get("reason", "pending_button_not_found")
        return result

    # Click Pending button
    human_delay(1, 2, distribution="gaussian")
    clicked = sim.click_element('button[aria-label*="Pending"], button[class*="pending"]')

    if not clicked:
        result["error"] = "pending_click_failed"
        return result

    human_delay(0.5, 1.5)

    # Click "Withdraw" in the dropdown/modal
    withdraw_clicked = sim.click_element(
        'button[aria-label*="Withdraw"], '
        'button:has(span:contains("Withdraw")), '
        '[class*="dropdown"] li button, '
        'div[class*="artdeco-dropdown"] button'
    )

    if not withdraw_clicked:
        # Try alternative — sometimes it's a confirmation dialog
        human_delay(0.5, 1)
        withdraw_clicked = cdp.evaluate("""
            (() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText.trim().toLowerCase().includes('withdraw')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)

    if not withdraw_clicked:
        # Dismiss any open dropdown
        sim.click_element('button[aria-label="Dismiss"], button[aria-label="Close"]')
        result["error"] = "withdraw_button_not_found"
        return result

    human_delay(1, 2)

    # Verify withdrawal — button should no longer say "Pending"
    verify = cdp.evaluate("""
        (() => {
            const pending = document.querySelector(
                'button[aria-label*="Pending"]'
            );
            const connect = document.querySelector(
                'button[aria-label*="Connect"]'
            );
            return JSON.stringify({
                still_pending: !!pending,
                connect_available: !!connect,
            });
        })()
    """)

    verification = json.loads(verify) if verify else {}

    if verification.get("still_pending"):
        result["error"] = "withdrawal_may_have_failed"
        return result

    result["success"] = True
    result["connect_restored"] = verification.get("connect_available", False)
    increment_counter(state, "withdrawals")

    return result


def check_acceptance_notifications(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
) -> dict[str, Any]:
    """Check the LinkedIn notifications page for connection acceptance events.

    More natural than checking My Network directly — humans check notifications.
    Looks for "accepted your invitation" / "accepted your connection request" entries.

    Returns list of accepted connections with profile URLs and names.

    Hardened path: uses readiness-gated navigation and safe scroll fallbacks
    for reliable unattended operation.
    """
    result: dict[str, Any] = {"action": "check_acceptance_notifications", "acceptances": []}

    # --- Navigate with readiness gate ---
    NOTIF_URL = "https://www.linkedin.com/notifications/"
    EXPECTED_SELECTOR = (
        '.nt-card, [class*="notification-card"], .notification-list-item, '
        '[class*="ntfctn"], li[class*="notification"]'
    )

    nav = _navigate_with_readiness(
        cdp,
        NOTIF_URL,
        expected_selector=EXPECTED_SELECTOR,
        nav_timeout=30,
        ready_timeout=20,
    )
    result["navigation"] = nav

    if not nav.get("ready"):
        human_delay(2, 4)
        nav = _navigate_with_readiness(
            cdp,
            NOTIF_URL,
            expected_selector=EXPECTED_SELECTOR,
            nav_timeout=30,
            ready_timeout=15,
        )
        result["navigation_retry"] = nav

    if not nav.get("ready"):
        result["error"] = (
            f"Notifications page not ready: "
            f"readyState={nav.get('readyState')}, "
            f"overlay={nav.get('overlay_detected')}, "
            f"selector_found={nav.get('selector_found')}"
        )
        return result

    human_delay(2, 4)

    # --- Circuit breaker check ---
    danger = check_circuit_breakers(cdp)
    if danger:
        result["error"] = danger
        return result

    # --- Scroll to load notifications (safe fallback) ---
    _safe_scroll_or_js(cdp, sim, random.randint(200, 500))
    human_delay(2, 4)

    # Sometimes scroll more (humans browse notifications)
    if random.random() < 0.4:
        _safe_scroll_or_js(cdp, sim, random.randint(200, 400))
        human_delay(1, 3)

    # --- Extract acceptance notifications (with retry) ---
    notif_data = _extract_acceptance_notifs_dom(cdp)
    if not notif_data:
        human_delay(2, 3)
        notif_data = _extract_acceptance_notifs_dom(cdp)

    acceptances = json.loads(notif_data) if notif_data else []
    result["acceptances"] = acceptances
    result["count"] = len(acceptances)

    # Surface the freshest notification matches separately for the runner,
    # but leave counting to the higher-level acceptance workflow.
    if acceptances:
        new_accepts = [
            a
            for a in acceptances
            if any(
                t in a.get("time_text", "")
                for t in [
                    "just now",
                    "today",
                    "hour",
                    "minute",
                    "1d",
                    "1 day",
                    "yesterday",
                    "2h",
                    "3h",
                    "4h",
                    "5h",
                ]
            )
            or a.get("index", 99) < 5
        ]
        if new_accepts:
            result["new_acceptances"] = new_accepts

    return result


def _extract_acceptance_notifs_dom(cdp: CDPConnection) -> str | None:
    """Extract acceptance notification data from the DOM via Runtime.evaluate.

    Factored out of check_acceptance_notifications to enable retry logic.
    """
    return cdp.evaluate("""
        (() => {
            const acceptances = [];
            const items = document.querySelectorAll(
                '.nt-card, [class*="notification-card"], .notification-list-item, ' +
                '[class*="ntfctn"], li[class*="notification"]'
            );
            items.forEach((item, i) => {
                if (i >= 30) return;
                const text = item.innerText.toLowerCase();

                // Check for acceptance language
                const isAcceptance = (
                    text.includes('accepted your invitation') ||
                    text.includes('accepted your connection') ||
                    text.includes('accepted your request') ||
                    text.includes('is now a connection') ||
                    text.includes('you are now connected')
                );

                if (isAcceptance) {
                    const linkEl = item.querySelector('a[href*="/in/"]');
                    const url = linkEl ? linkEl.href.split('?')[0] : '';

                    const nameEl = item.querySelector(
                        'strong, [class*="actor-name"], a[href*="/in/"]'
                    );
                    const name = nameEl ? nameEl.innerText.trim().split('\\n')[0] : '';

                    const timeEl = item.querySelector(
                        'time, [class*="time"], [class*="timestamp"]'
                    );
                    const timeText = timeEl ? timeEl.innerText.trim() : '';

                    if (url || name) {
                        acceptances.push({
                            url: url,
                            name: name,
                            time_text: timeText.toLowerCase(),
                            index: i,
                            notification_text: item.innerText.substring(0, 150).trim(),
                        });
                    }
                }
            });
            return JSON.stringify(acceptances);
        })()
    """)


# ---------------------------------------------------------------------------
# Session orchestrator — batch connection requests with interleaved engagement
# ---------------------------------------------------------------------------


def run_session(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    prospects: list[dict[str, Any]],
    burst_size: tuple[int, int] = (3, 6),
    engagement_approaches: list[str] | None = None,
) -> dict[str, Any]:
    """Run a full connection request session with interleaved engagement.

    This is the main orchestrator that combines:
    1. Warm-up (feed scroll)
    2. Bursts of connection requests (burst_size range per burst)
    3. Between-burst interleave activities (engagement approaches)
    4. Cool-down

    The agent provides:
    - prospects: list of prospect dicts from outreach_helper.load_prospect_queue()
    - engagement_approaches: list of approach letters to use (e.g., ["A", "E"])
      A = built-in (Activity tab on every profile, already mandatory)
      B = follow_engagement_trail (visit likers/commenters)
      C = scan_reaction_list (mine reactions)
      D = scan_prospect_box (PYMK suggestions)
      E = like_post (feed engagement filler)

    Returns detailed session report.
    """
    if engagement_approaches is None:
        engagement_approaches = ["E"]  # Default to feed likes as filler

    session_report = {
        "action": "run_session",
        "started_at": datetime.now().isoformat(),
        "conn_requests": [],
        "engagement_actions": [],
        "errors": [],
        "warm_up": None,
        "cool_down": None,
    }

    # --- Warm-up ---
    warm_up_result = session_warm_up(cdp, sim, state)
    session_report["warm_up"] = warm_up_result
    if warm_up_result.get("error"):
        session_report["aborted"] = True
        session_report["abort_reason"] = warm_up_result.get("danger", "warm_up_failed")
        return session_report

    # --- Process prospects in bursts ---
    prospect_idx = 0
    burst_count = 0

    while prospect_idx < len(prospects):
        # Determine burst size (randomized within range)
        this_burst = random.randint(burst_size[0], burst_size[1])
        burst_prospects = prospects[prospect_idx : prospect_idx + this_burst]
        prospect_idx += this_burst
        burst_count += 1

        # Execute burst
        for prospect in burst_prospects:
            # Check quotas before each request
            if get_counter(state, "conn_req_sent") >= MAX_CONN_REQ_PER_DAY:
                session_report["stopped_reason"] = "daily_limit_reached"
                break
            if get_weekly_counter(state, "conn_req_sent") >= MAX_CONN_REQ_PER_WEEK:
                session_report["stopped_reason"] = "weekly_limit_reached"
                break

            # Inter-request delay (randomized, never identical)
            if session_report["conn_requests"]:
                human_delay(30, 90, distribution="log_normal")

            # Send the connection request
            cr_result = send_connection_request(
                cdp,
                sim,
                state,
                profile_url=prospect.get("contact_linkedin", ""),
                note=prospect.get("_note"),
                enable_notifications=prospect.get("_enable_notifications", False),
            )
            cr_result["prospect_id"] = prospect.get("id", "")
            cr_result["company"] = prospect.get("company", "")
            cr_result["contact_name"] = prospect.get("contact_name", "")
            session_report["conn_requests"].append(cr_result)

            if cr_result.get("error") in ("daily_conn_req_limit", "weekly_conn_req_limit"):
                session_report["stopped_reason"] = cr_result["error"]
                break

            # Circuit breaker from connection request
            if cr_result.get("error") in (
                "captcha",
                "restriction",
                "email_verify",
                "robot_check",
                "login",
            ):
                session_report["aborted"] = True
                session_report["abort_reason"] = cr_result["error"]
                return session_report

        # Check if we should stop
        if session_report.get("stopped_reason") or session_report.get("aborted"):
            break

        # --- Between-burst interleave ---
        if prospect_idx < len(prospects):
            interleave_result = _execute_interleave(cdp, sim, state, engagement_approaches)
            session_report["engagement_actions"].append(interleave_result)

    # --- Cool-down ---
    human_delay(5, 15)
    cool_down_result = session_cool_down(cdp, sim)
    session_report["cool_down"] = cool_down_result

    # --- Summary ---
    successful = sum(1 for cr in session_report["conn_requests"] if cr.get("success"))
    session_report["summary"] = {
        "total_attempted": len(session_report["conn_requests"]),
        "successful": successful,
        "failed": len(session_report["conn_requests"]) - successful,
        "bursts": burst_count,
        "engagement_actions": len(session_report["engagement_actions"]),
        "ended_at": datetime.now().isoformat(),
    }

    return session_report


def _execute_interleave(
    cdp: CDPConnection,
    sim: HumanSimulator,
    state: dict,
    approaches: list[str],
) -> dict[str, Any]:
    """Execute a random interleave activity between connection request bursts.

    Picks a random approach from the provided list and executes it.
    """
    approach = random.choice(approaches)
    result = {"approach": approach}

    human_delay(5, 20, distribution="gaussian")

    if approach == "E":
        # Feed engagement — scroll feed and like a post
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)

        # Scroll a few times
        for _ in range(random.randint(2, 5)):
            sim.scroll(random.randint(300, 600))
            human_delay(1, 4)

        # Like a visible post
        like_result = like_post(cdp, sim, state)
        result["like"] = like_result

    elif approach == "B":
        # Engagement trail — needs a post URL (agent provides via prospect activity)
        # Since we don't have a URL here, do a feed scroll instead as fallback
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)
        scroll_seq = generate_scroll_stop_sequence(random.randint(2, 4))
        observed = execute_feed_scroll(sim, scroll_seq)
        result["feed_scroll"] = {"posts_observed": len(observed)}

    elif approach == "D":
        # Prospect box scan
        scan_result = scan_prospect_box(cdp, sim)
        result["suggestions"] = scan_result

    else:
        # Default: brief feed scroll
        cdp.navigate("https://www.linkedin.com/feed/")
        human_delay(2, 4)
        sim.scroll(random.randint(300, 800))
        human_delay(3, 8)

    return result


# ---------------------------------------------------------------------------
# LinkedInSession — high-level session manager
# ---------------------------------------------------------------------------


class LinkedInSession:
    """High-level LinkedIn automation session manager.

    Usage:
        session = LinkedInSession()
        session.connect()
        session.warm_up()
        # ... do work ...
        session.cool_down()
        session.disconnect()
    """

    def __init__(self):
        self.cdp = CDPConnection()
        self.sim: HumanSimulator | None = None
        self.state = load_state()
        self.connected = False
        self._action_times: list[float] = []  # timestamps of recent actions

    def connect(self, skip_rate_check: bool = False) -> dict[str, Any]:
        """Connect to Chrome CDP, inject stealth, run preflight.

        Args:
            skip_rate_check: If True, skip the acceptance rate gate in preflight.
                Use for read-only operations like acceptance checks that should
                run even when the acceptance rate is critically low.
        """
        # Preflight
        preflight = preflight_check(self.state)
        if not preflight["ok"]:
            # Read-only operations never send a connection request, so a rate
            # gate (including a temporary failure while reading its Sheets
            # sources) must not make the browser lane look unhealthy.  Keep
            # every other preflight blocker -- CDP, login, danger and quota --
            # fully enforced.
            rate_gate_failed = (
                "acceptance" in str(preflight.get("block_reason", "")).lower()
                or "outreach log" in str(preflight.get("block_reason", "")).lower()
            )
            if skip_rate_check and rate_gate_failed:
                preflight["ok"] = True
                preflight["acceptance_rate_skipped"] = True
            else:
                return preflight

        # Connect
        self.cdp.connect()
        # preflight_check already attached the stealth scripts to this same
        # page and to every future document. Re-applying them here creates a
        # second CDP evaluation phase before the first profile can open.
        self.sim = HumanSimulator(self.cdp)
        self.connected = True
        return {
            "ok": True,
            "status": "connected",
            **{k: v for k, v in preflight.items() if k.startswith("acceptance_rate")},
        }

    def disconnect(self):
        """Disconnect from Chrome CDP."""
        self.cdp.disconnect()
        self.connected = False
        self.sim = None

    def _enforce_rate_limit(self):
        """Ensure we don't exceed MAX_ACTIONS_PER_MINUTE."""
        now = time.time()
        # Clean old entries
        self._action_times = [t for t in self._action_times if now - t < 60]
        if len(self._action_times) >= MAX_ACTIONS_PER_MINUTE:
            wait_until = self._action_times[0] + 60
            if wait_until > now:
                time.sleep(wait_until - now + random.uniform(0.5, 2.0))
        self._action_times.append(time.time())

    def _check_danger(self) -> str | None:
        """Check for circuit breaker conditions."""
        return check_circuit_breakers(self.cdp)

    def warm_up(self) -> dict[str, Any]:
        """Execute session warm-up."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return session_warm_up(self.cdp, self.sim, self.state)

    def cool_down(self) -> dict[str, Any]:
        """Execute session cool-down."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return session_cool_down(self.cdp, self.sim)

    def view_profile(self, profile_url: str) -> dict[str, Any]:
        """View a profile with human-like behavior."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"error": True, "danger": danger}

        result = view_profile(self.cdp, self.sim, profile_url)
        increment_counter(self.state, "profile_views")
        return result

    def inspect_profile_action_state(self, profile_url: str) -> dict[str, Any]:
        """Inspect profile action state without heavy browsing."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        # A 404 belongs to the profile that was previously open.  It is not a
        # browser-wide condition and must not prevent us navigating to the
        # next profile in the frozen queue.
        if danger and danger != "invalid_profile_or_404":
            return {"state": "profile_unavailable", "error": danger, "profile_url": profile_url}
        result = inspect_profile_action_state(self.cdp, profile_url)
        increment_counter(self.state, "profile_views")
        return result

    def verify_no_note_send_ui(
        self, profile_url: str, profile_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify the no-note invite modal controls without sending."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"ok": False, "error": danger, "profile_url": profile_url}
        return verify_no_note_send_ui(self.cdp, self.sim, self.state, profile_url, profile_state)

    def read_activity(self, profile_url: str, max_seconds: float = 30.0) -> dict[str, Any]:
        """Read a profile's activity tab."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger:
            return {"error": True, "danger": danger}

        return read_activity_tab(self.cdp, self.sim, profile_url, max_seconds=max_seconds)

    def read_activity_detail(
        self,
        profile_url: str,
        max_seconds: float = 45.0,
        navigation_type: str = "direct_url",
        tab_order: list[tuple[str, str]] | None = None,
        minimum_direct_comments: int = 2,
        disable_early_stop: bool = False,
    ) -> dict[str, Any]:
        """Read profile activity tabs separately for ranking."""
        self._enforce_rate_limit()
        danger = self._check_danger()
        if danger and danger != "invalid_profile_or_404":
            return {"error": True, "danger": danger}

        return read_activity_tabs_detail(
            self.cdp,
            self.sim,
            profile_url,
            max_seconds=max_seconds,
            navigation_type=navigation_type,
            tab_order=tab_order,
            minimum_direct_comments=minimum_direct_comments,
            disable_early_stop=disable_early_stop,
        )

    def set_notifications(self, enable: bool = True) -> bool:
        """Toggle notifications for the current profile."""
        return toggle_profile_notifications(self.cdp, self.sim, enable)

    def read_feed(self, num_stops: int = 5) -> list[dict]:
        """Scroll the feed with content-aware behavior."""
        self._enforce_rate_limit()
        sequence = generate_scroll_stop_sequence(num_stops)
        return execute_feed_scroll(self.sim, sequence)

    def get_quotas(self) -> dict[str, Any]:
        """Get current quota status."""
        return {
            "conn_req_today": get_counter(self.state, "conn_req_sent"),
            "conn_req_limit": MAX_CONN_REQ_PER_DAY,
            "conn_req_week": get_weekly_counter(self.state, "conn_req_sent"),
            "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
            "profile_views_today": get_counter(self.state, "profile_views"),
            "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
        }

    # --- Phase 3 methods ---

    def send_connection(
        self,
        profile_url: str,
        note: str | None = None,
        enable_notifications: bool = False,
        profile_data: dict[str, Any] | None = None,
        activity_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a connection request to a profile."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return send_connection_request(
            self.cdp,
            self.sim,
            self.state,
            profile_url,
            note,
            enable_notifications,
            profile_data,
            activity_data,
        )

    def send_connection_only(
        self,
        profile_url: str,
        profile_state: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a connection request without reading activity internally."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return send_connection_only(
            self.cdp, self.sim, self.state, profile_url, profile_state, note
        )

    def like(self, post_url: str | None = None) -> dict[str, Any]:
        """Like a post (Approach E)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return like_post(self.cdp, self.sim, self.state, post_url)

    def engagement_trail(
        self,
        post_url: str,
        max_profiles: int = 3,
    ) -> dict[str, Any]:
        """Follow engagement trail on a post (Approach B)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return follow_engagement_trail(
            self.cdp,
            self.sim,
            self.state,
            post_url,
            max_profiles,
        )

    def reaction_scan(self, post_url: str) -> dict[str, Any]:
        """Scan reaction list on a post (Approach C)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return scan_reaction_list(self.cdp, self.sim, post_url)

    def check_accepts(self) -> dict[str, Any]:
        """Check for new connection acceptances (legacy: connections page scan)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptances(self.cdp, self.sim, self.state)

    def check_accepts_subtractive(self, pending_prospects: list[dict[str, Any]]) -> dict[str, Any]:
        """Check for acceptances by comparing pending prospects against sent invitations.

        Subtractive strategy: anyone pending in sheet but missing from sent page
        has had a status change. Verifies via profile visit.
        """
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptances_subtractive(
            self.cdp,
            self.sim,
            self.state,
            pending_prospects,
        )

    def prospect_box(self) -> dict[str, Any]:
        """Scan 'People you may know' suggestions (Approach D)."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return scan_prospect_box(self.cdp, self.sim)

    def batch_session(
        self,
        prospects: list[dict[str, Any]],
        burst_size: tuple[int, int] = (3, 6),
        engagement_approaches: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a full batch session with interleaved engagement."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return run_session(
            self.cdp,
            self.sim,
            self.state,
            prospects,
            burst_size,
            engagement_approaches,
        )

    def withdraw(self, profile_url: str) -> dict[str, Any]:
        """Withdraw a pending connection request."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return withdraw_connection(self.cdp, self.sim, self.state, profile_url)

    def check_acceptance_notifs(self) -> dict[str, Any]:
        """Check notifications page for connection acceptances."""
        self._enforce_rate_limit()
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return check_acceptance_notifications(self.cdp, self.sim, self.state)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LinkedIn automation helper")
    sub = parser.add_subparsers(dest="command")

    # preflight
    sub.add_parser("preflight", help="Run pre-flight checks")

    # health
    sub.add_parser("health", help="Check Chrome CDP health")

    # warm-up
    sub.add_parser("warm-up", help="Execute session warm-up")

    # cool-down
    sub.add_parser("cool-down", help="Execute session cool-down")

    # view-profile
    vp = sub.add_parser("view-profile", help="View a LinkedIn profile")
    vp.add_argument("--url", required=True, help="LinkedIn profile URL")

    # read-activity
    ra = sub.add_parser("read-activity", help="Read profile activity tab")
    ra.add_argument("--url", required=True, help="LinkedIn profile URL")

    # read-feed
    rf = sub.add_parser("read-feed", help="Scroll and read the feed")
    rf.add_argument("--stops", type=int, default=5, help="Number of scroll stops")

    # detect-page
    sub.add_parser("detect-page", help="Detect current LinkedIn page type")

    # quotas
    sub.add_parser("quotas", help="Show current quota status")

    # --- Phase 3 CLI commands ---

    # send-connection
    sc = sub.add_parser("send-connection", help="Send a connection request")
    sc.add_argument("--url", required=True, help="LinkedIn profile URL")
    sc.add_argument("--note", default=None, help="Personalized note (max 300 chars)")
    sc.add_argument("--notify", action="store_true", help="Enable notifications if active")

    vm = sub.add_parser(
        "verify-connection-modal", help="Open and dismiss the connection modal without sending"
    )
    vm.add_argument("--url", required=True, help="LinkedIn profile URL")

    # like-post
    lp = sub.add_parser("like-post", help="Like a post")
    lp.add_argument("--url", default=None, help="Post URL (or like from current viewport)")

    # engagement-trail
    et = sub.add_parser("engagement-trail", help="Follow engagement trail on a post")
    et.add_argument("--url", required=True, help="Post URL")
    et.add_argument("--max-profiles", type=int, default=3, help="Max profiles to visit")

    # reaction-scan
    rs = sub.add_parser("reaction-scan", help="Scan reaction list on a post")
    rs.add_argument("--url", required=True, help="Post URL")

    # check-acceptances
    sub.add_parser("check-acceptances", help="Check for new connection acceptances")

    # prospect-box
    sub.add_parser("prospect-box", help="Scan People You May Know suggestions")

    # withdraw
    wd = sub.add_parser("withdraw", help="Withdraw a pending connection request")
    wd.add_argument("--url", required=True, help="LinkedIn profile URL")

    # check-acceptance-notifs
    sub.add_parser("check-acceptance-notifs", help="Check notifications for connection acceptances")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "health":
        cdp = CDPConnection()
        result = cdp.health_check()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "ok" else 1)

    if args.command == "preflight":
        result = preflight_check()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 1)

    if args.command == "quotas":
        state = load_state()
        quotas = {
            "conn_req_today": get_counter(state, "conn_req_sent"),
            "conn_req_limit": MAX_CONN_REQ_PER_DAY,
            "conn_req_week": get_weekly_counter(state, "conn_req_sent"),
            "conn_req_week_limit": MAX_CONN_REQ_PER_WEEK,
            "profile_views_today": get_counter(state, "profile_views"),
            "profile_views_limit": MAX_PROFILE_VIEWS_PER_DAY,
        }
        print(json.dumps(quotas, indent=2))
        sys.exit(0)

    # Commands that need a full session
    session = LinkedInSession()
    connect_result = session.connect()
    if not connect_result.get("ok"):
        print(json.dumps(connect_result, indent=2))
        sys.exit(1)

    try:
        if args.command == "warm-up":
            result = session.warm_up()
        elif args.command == "cool-down":
            result = session.cool_down()
        elif args.command == "view-profile":
            result = session.view_profile(args.url)
        elif args.command == "read-activity":
            result = session.read_activity(args.url)
        elif args.command == "read-feed":
            sequence = generate_scroll_stop_sequence(args.stops)
            result = execute_feed_scroll(session.sim, sequence)
        elif args.command == "detect-page":
            result = detect_page(session.cdp)
        elif args.command == "send-connection":
            result = session.send_connection(
                args.url,
                note=args.note,
                enable_notifications=args.notify,
            )
        elif args.command == "verify-connection-modal":
            profile_state = session.inspect_profile_action_state(args.url)
            result = session.verify_no_note_send_ui(args.url, profile_state)
            result["profile_state"] = profile_state
        elif args.command == "like-post":
            result = session.like(args.url)
        elif args.command == "engagement-trail":
            result = session.engagement_trail(args.url, args.max_profiles)
        elif args.command == "reaction-scan":
            result = session.reaction_scan(args.url)
        elif args.command == "check-acceptances":
            result = session.check_accepts()
        elif args.command == "prospect-box":
            result = session.prospect_box()
        elif args.command == "withdraw":
            result = session.withdraw(args.url)
        elif args.command == "check-acceptance-notifs":
            result = session.check_acceptance_notifs()
        else:
            result = {"error": f"Unknown command: {args.command}"}

        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        session.disconnect()


if __name__ == "__main__":
    main()
