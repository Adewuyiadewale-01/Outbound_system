#!/usr/bin/env python3
"""Read-only-ish probe for profile fallback Pending -> withdrawal popup.

This opens a target profile, clicks the visible Pending control, captures the
visible dialog/menu controls, then dismisses the popup. It does not click the
Withdraw confirmation button.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from linkedin_helper import (  # noqa: E402
    LinkedInSession,
    _navigate_with_readiness,
    check_circuit_breakers,
    human_delay,
)

DEFAULT_PROFILE = "https://www.linkedin.com/in/huub-van-delft-msc-msre-rt-79768a7b/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe LinkedIn profile fallback withdrawal popup without confirming."
    )
    parser.add_argument("--profile-url", default=DEFAULT_PROFILE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = LinkedInSession()
    connected = session.connect(skip_rate_check=True)
    if not connected.get("ok"):
        print(
            json.dumps(
                {"ok": False, "reason": "linkedin_connect_failed", "detail": connected}, indent=2
            )
        )
        return 1

    try:
        nav_result = _navigate_with_readiness(
            session.cdp,
            args.profile_url,
            expected_selector="body",
            nav_timeout=20,
            ready_timeout=20,
        )
        human_delay(1, 2)
        danger = check_circuit_breakers(session.cdp)
        if danger:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "circuit_breaker",
                        "detail": danger,
                        "nav_result": nav_result,
                    },
                    indent=2,
                )
            )
            return 1

        result = session.cdp.evaluate(
            """
            (async () => {
              const sleep = ms => new Promise(r => setTimeout(r, ms));
              const norm = s => (s || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && style.display !== "none" && style.visibility !== "hidden";
              };
              const describe = el => {
                const r = el.getBoundingClientRect();
                return {
                  tag: el.tagName,
                  role: norm(el.getAttribute("role")),
                  type: norm(el.getAttribute("type")),
                  text: norm(el.innerText || el.textContent),
                  aria: norm(el.getAttribute("aria-label")),
                  dataControlName: norm(el.getAttribute("data-control-name")),
                  componentKey: norm(el.getAttribute("componentkey")),
                  href: el.href || "",
                  rect: {
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    w: Math.round(r.width),
                    h: Math.round(r.height)
                  }
                };
              };

              const beforeControls = [...document.querySelectorAll('a,button,[role="button"]')].filter(visible);
              const pendingCandidates = beforeControls.filter(el => {
                const text = norm(el.innerText || el.textContent);
                const aria = norm(el.getAttribute("aria-label"));
                const key = norm(el.getAttribute("componentkey") || el.getAttribute("data-control-name"));
                return /^Pending$/i.test(text) || /Pending, click to withdraw/i.test(aria) || /_pending/i.test(key);
              });
              const pending = pendingCandidates
                .map(el => {
                  const r = el.getBoundingClientRect();
                  const y = Math.round(r.y);
                  return {el, score: (y > 120 ? 100 : 0) + (y > 0 ? 20 : 0) - Math.abs(y - 600)};
                })
                .sort((a, b) => b.score - a.score)[0]?.el || pendingCandidates[0];

              if (!pending) {
                return {
                  ok: false,
                  reason: "pending_control_missing",
                  url: location.href,
                  title: document.title,
                  beforeControls: beforeControls.map(describe).slice(0, 80)
                };
              }

              pending.scrollIntoView({block: "center"});
              await sleep(500);
              pending.click();
              await sleep(1600);

              const afterControls = [...document.querySelectorAll('a,button,[role="button"]')].filter(visible);
              const dialogs = [...document.querySelectorAll('[role="dialog"], [data-test-modal], .artdeco-modal')].filter(visible);
              const popups = [...document.querySelectorAll('[role="menu"], [role="listbox"], .artdeco-dropdown__content, .artdeco-modal')].filter(visible);
              const withdrawCandidates = afterControls.filter(el => {
                const text = norm(el.innerText || el.textContent);
                const aria = norm(el.getAttribute("aria-label"));
                return /^Withdraw$/i.test(text) || /withdraw invitation/i.test(aria);
              });
              const cancelCandidates = afterControls.filter(el => {
                const text = norm(el.innerText || el.textContent);
                const aria = norm(el.getAttribute("aria-label"));
                return /^Cancel$/i.test(text) || /^Dismiss$/i.test(aria);
              });

              const dismissed = (() => {
                const cancel = cancelCandidates.find(el => /^Cancel$/i.test(norm(el.innerText || el.textContent)));
                const dismiss = cancelCandidates.find(el => /^Dismiss$/i.test(norm(el.getAttribute("aria-label"))));
                const close = cancel || dismiss;
                if (close) {
                  close.click();
                  return describe(close);
                }
                document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
                return {fallback: "escape"};
              })();
              await sleep(500);

              return {
                ok: true,
                url: location.href,
                title: document.title,
                pendingClicked: describe(pending),
                dialogCount: dialogs.length,
                popupCount: popups.length,
                dialogs: dialogs.map(el => ({
                  role: norm(el.getAttribute("role")),
                  className: el.className ? String(el.className).slice(0, 160) : "",
                  text: norm(el.innerText || el.textContent).slice(0, 500),
                  rect: describe(el).rect
                })),
                withdrawCandidates: withdrawCandidates.map(describe),
                cancelCandidates: cancelCandidates.map(describe),
                afterControls: afterControls.map(describe).slice(0, 120),
                dismissed
              };
            })()
            """,
            await_promise=True,
            timeout=20,
        )
        print(
            json.dumps(
                {"ok": True, "nav_result": nav_result, "probe": result},
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        session.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
