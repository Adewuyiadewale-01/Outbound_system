"""Human-like browser interaction simulator (mouse, keyboard, scrolling).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S8). Pure move.
"""

import json
import random
import time

from outbound.shared.browser.connection import CDPConnection
from outbound.shared.human.delays import human_delay, typing_delay


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
