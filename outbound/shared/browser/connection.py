"""Chrome DevTools Protocol transport (CDP WebSocket connection manager).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S3). Pure move.
"""

import json
import os
import threading
import time
from typing import Any
from urllib.parse import quote

import websocket  # websocket-client

from outbound.shared.human.delays import human_delay

CDP_HOST = "localhost"


CDP_PORT = 18800


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
