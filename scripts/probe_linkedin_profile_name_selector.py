#!/usr/bin/env python3
"""Probe LinkedIn profile name selectors through the existing CDP session.

Usage:
    python3 scripts/probe_linkedin_profile_name_selector.py --url https://www.linkedin.com/in/example/

The script is read-only. It navigates to the profile, waits for the profile
header to settle, then reports candidate selectors and extracted names as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from linkedin_helper import LinkedInSession, check_circuit_breakers  # noqa: E402

PROFILE_NAME_PROBE_JS = """
(() => {
  const norm = value => (value || "").replace(/\\s+/g, " ").trim();
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const selectors = [
    "main h1.text-heading-xlarge",
    "main h1.inline.t-24.v-align-middle.break-words",
    "main section h1",
    "section.artdeco-card h1",
    ".pv-text-details__left-panel h1",
    ".ph5 h1",
    "h1"
  ];
  const candidates = [];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      const text = norm(el.innerText || el.textContent);
      if (!text || !visible(el)) continue;
      const r = el.getBoundingClientRect();
      candidates.push({
        selector,
        text,
        x: Math.round(r.x),
        y: Math.round(r.y),
        width: Math.round(r.width),
        height: Math.round(r.height)
      });
    }
  }
  const profileHeader = [...document.querySelectorAll("main section, main .ph5, main .pv-text-details__left-panel")]
    .filter(visible)
    .map(el => norm(el.innerText || el.textContent).slice(0, 500))
    .find(text => text) || "";
  const best = candidates.find(c => c.y >= 0 && c.y < 420) || candidates[0] || null;
  return {
    ok: !!best,
    url: location.href,
    title: document.title,
    best,
    candidates,
    profile_header_sample: profileHeader
  };
})()
"""


def probe_profile_name(profile_url: str, settle_seconds: float) -> dict[str, Any]:
    session = LinkedInSession()
    connected = False
    try:
        connect_result = session.connect(skip_rate_check=True)
        if not connect_result.get("ok"):
            return {
                "ok": False,
                "reason": "linkedin_session_connect_failed",
                "connect_result": connect_result,
            }
        connected = True
        session.cdp.navigate(profile_url, wait_load=False, timeout=10)
        time.sleep(max(1.0, settle_seconds))
        danger = check_circuit_breakers(session.cdp)
        if danger:
            return {"ok": False, "reason": "circuit_breaker", "danger": danger}
        result = session.cdp.evaluate(PROFILE_NAME_PROBE_JS, timeout=20)
        return (
            result
            if isinstance(result, dict)
            else {"ok": False, "reason": "unexpected_probe_result", "raw": result}
        )
    finally:
        if connected:
            session.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe LinkedIn profile display-name selectors.")
    parser.add_argument("--url", required=True, help="LinkedIn profile URL to inspect.")
    parser.add_argument(
        "--settle-seconds", type=float, default=4.0, help="Seconds to wait after navigation."
    )
    args = parser.parse_args()
    print(
        json.dumps(
            probe_profile_name(args.url, args.settle_seconds),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
