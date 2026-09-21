#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

CDP_URL="http://localhost:18800/json/version"
if ! curl -s --max-time 5 "$CDP_URL" > /dev/null 2>&1; then
    echo "ERROR: Chrome CDP not reachable on port 18800."
    exit 1
fi

python3 -c "
import sys, json, os, time
sys.path.insert(0, 'helpers')

creds = os.path.expanduser('~/.openclaw/credentials/google-sheets.json')

# Step 1: Load pending prospects from sheet
from outreach_helper import get_pending_connections
from linkedin_helper import _normalize_linkedin_profile_url
pending = get_pending_connections(credentials_path=creds)
print(f'=== PENDING PROSPECTS ({len(pending)}) ===')
prospect_urls = {}
for p in pending:
    url = _normalize_linkedin_profile_url(p.get('contact_linkedin') or '')
    name = p.get('contact_name', '')
    prospect_urls[url] = name
    print(f'  {name}: {url}')

# Step 2: Scrape sent invitations page
print()
print('=== SCRAPING SENT INVITATIONS PAGE ===')
from linkedin_helper import (
    CDPConnection, inject_stealth, scrape_sent_invitations,
    _navigate_with_readiness, _normalize_linkedin_profile_url, HumanSimulator,
)

cdp = CDPConnection()
cdp.connect()
inject_stealth(cdp)
sim = HumanSimulator(cdp)

sent_result = scrape_sent_invitations(cdp, sim)
if sent_result.get('error'):
    print(f'  ERROR: {sent_result[\"error\"]}')
    cdp.disconnect()
    raise SystemExit(1)
invitations = sent_result.get('invitations', [])
for snapshot in sent_result.get('scroll_snapshots', []):
    print(
        f'  Pass {snapshot.get(\"pass\")}: '
        f'snapshot={snapshot.get(\"snapshot_count\")}, '
        f'new={snapshot.get(\"new_count\")}, '
        f'unique={snapshot.get(\"unique_count\")}'
    )
print(f'  Total sent invitations found: {len(invitations)}')

sent_urls = set()
print()
print('=== SENT INVITATIONS ===')
for inv in invitations:
    url = _normalize_linkedin_profile_url(inv.get('url', ''))
    name = inv.get('name', '')
    sent_text = inv.get('sent_text', '')
    sent_urls.add(url)
    print(f'  {name}: {url} ({sent_text})')

# Step 3: Compare
print()
print('=== MATCHING RESULTS ===')
missing = []
for url, name in prospect_urls.items():
    if url in sent_urls:
        print(f'  STILL PENDING: {name} — found on sent page')
    else:
        print(f'  MISSING FROM SENT: {name} — needs verification')
        missing.append((name, url))

print()
print(f'Summary: {len(prospect_urls) - len(missing)} still pending, {len(missing)} missing from sent page')

if missing:
    print()
    print('=== VERIFYING MISSING PROSPECTS ===')
    from linkedin_helper import inspect_profile_action_state
    for name, url in missing:
        # Reconstruct full URL for navigation
        full_url = url if url.startswith('http') else f'https://www.linkedin.com{url}'
        print(f'  Checking {name} at {full_url}...')
        try:
            nav = _navigate_with_readiness(cdp, full_url, nav_timeout=30, ready_timeout=15)
            time.sleep(3)
            inspected = inspect_profile_action_state(cdp, full_url)
            state = inspected.get('state', 'unknown')
            print(f'    -> state: {state}')
            if state == 'already_connected':
                print(f'    -> ACCEPTED!')
            elif state == 'already_pending':
                print(f'    -> still pending (pagination miss?)')
            elif state in ('connect_direct', 'connect_in_more'):
                print(f'    -> DECLINED or EXPIRED')
            else:
                print(f'    -> unknown state')
        except Exception as e:
            print(f'    -> ERROR: {e}')
        time.sleep(2)

cdp.disconnect()
print()
print('=== DONE ===')
" 2>&1 | tee "$REPO_ROOT/state/acceptance_monitoring/matching_diagnostic.log"
