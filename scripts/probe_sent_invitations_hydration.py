#!/usr/bin/env python3
"""Read-only diagnostic for LinkedIn Sent Invitations list hydration.

This deliberately does not inspect a prepared withdrawal queue, open a profile,
open a withdrawal dialog, write a sheet, or write the withdrawal journal.
It reproduces the runner's direct navigation to Sent Invitations, waits for the
page to settle, performs one workspace scroll event in the invitation pane, and
reports whether LinkedIn appended more invitation rows.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

from linkedin_helper import LinkedInSession, check_circuit_breakers  # noqa: E402
from withdraw_connections import SENT_INVITATIONS_URL, open_sent_invitations  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Sent Invitations hydration diagnostic.")
    parser.add_argument("--warmup-seconds", type=float, default=30.0)
    parser.add_argument("--settle-seconds", type=float, default=8.0)
    return parser.parse_args()


def snapshot(session: LinkedInSession, label: str) -> dict:
    result = session.cdp.evaluate(
        """
        (() => {
          const scroller = document.querySelector('main#workspace');
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          const links = [...document.querySelectorAll('a[aria-label^="Withdraw invitation sent to"]')]
            .filter(visible);
          return {
            label: %s,
            at: new Date().toISOString(),
            url: location.href,
            onSentInvitations: /\\/mynetwork\\/invitation-manager\\/sent\\/?/i.test(location.pathname),
            workspaceFound: !!scroller,
            withdrawRows: links.length,
            profileLinks: [...document.querySelectorAll('main#workspace a[href*="/in/"]')].filter(visible).length,
            scrollTop: scroller ? Math.round(scroller.scrollTop) : null,
            clientHeight: scroller ? Math.round(scroller.clientHeight) : null,
            scrollHeight: scroller ? Math.round(scroller.scrollHeight) : null,
            wheelPoint: scroller ? {
              x: Math.round(scroller.getBoundingClientRect().left + scroller.getBoundingClientRect().width * 0.5),
              y: Math.round(scroller.getBoundingClientRect().top + Math.min(scroller.getBoundingClientRect().height * 0.6, Math.max(140, scroller.getBoundingClientRect().height - 120)))
            } : null,
          };
        })()
        """
        % json.dumps(label),
        timeout=12,
    )
    return result if isinstance(result, dict) else {"label": label, "raw": result}


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

    observations: list[dict] = []
    try:
        opened = open_sent_invitations(session)
        if not opened.get("ok"):
            print(
                json.dumps(
                    {"ok": False, "reason": "sent_invitations_not_ready", "open": opened}, indent=2
                )
            )
            return 1
        danger = check_circuit_breakers(session.cdp)
        if danger:
            print(
                json.dumps({"ok": False, "reason": "circuit_breaker", "detail": danger}, indent=2)
            )
            return 1

        observations.append(snapshot(session, "opened"))
        time.sleep(max(0, args.warmup_seconds))
        before = snapshot(session, "after_warmup")
        observations.append(before)
        point = before.get("wheelPoint") or {}
        height = int(before.get("clientHeight") or 0)
        if not point or height <= 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "workspace_scroller_missing",
                        "observations": observations,
                    },
                    indent=2,
                )
            )
            return 1

        scroll_result = session.cdp.evaluate(
            """
            (() => {
              const scroller = document.querySelector('main#workspace');
              if (!scroller) return {ok: false, reason: 'workspace_scroller_missing'};
              const beforeTop = Math.round(scroller.scrollTop);
              const beforeHeight = Math.round(scroller.scrollHeight);
              const delta = Math.max(360, Math.round(scroller.clientHeight * 0.85));
              scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + delta);
              scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
              window.dispatchEvent(new Event('scroll'));
              return {
                ok: true,
                method: 'js_scroll_event',
                beforeTop,
                afterTop: Math.round(scroller.scrollTop),
                beforeHeight,
                afterHeight: Math.round(scroller.scrollHeight),
                delta,
              };
            })()
            """,
            timeout=8,
        )
        observations.append({"label": "scroll_action", "scroll_result": scroll_result})
        observations.append(snapshot(session, "immediately_after_scroll_event"))

        checks = max(1, int(max(0, args.settle_seconds)))
        for second in range(1, checks + 1):
            time.sleep(1)
            observations.append(snapshot(session, f"settle_{second}s"))

        row_counts = [item.get("withdrawRows", 0) for item in observations]
        initial_rows = int(before.get("withdrawRows") or 0)
        final_rows = int(observations[-1].get("withdrawRows") or 0)
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "read_only_no_withdrawals",
                    "navigation": SENT_INVITATIONS_URL,
                    "warmup_seconds": args.warmup_seconds,
                    "settle_seconds": args.settle_seconds,
                    "initial_rows": initial_rows,
                    "final_rows": final_rows,
                    "rows_added": final_rows - initial_rows,
                    "hydrated_after_scroll_event": final_rows > initial_rows,
                    "max_rows_seen": max(int(count or 0) for count in row_counts),
                    "observations": observations,
                },
                indent=2,
            )
        )
        return 0
    finally:
        session.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
