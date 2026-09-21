#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
OUTPUT="$REPO_ROOT/output/sent_invitations_export.csv"

CDP_URL="http://localhost:18800/json/version"
if ! curl -s --max-time 5 "$CDP_URL" > /dev/null 2>&1; then
    echo "ERROR: Chrome CDP not reachable on port 18800."
    exit 1
fi

python3 -c "
import sys, json, csv, time, random
sys.path.insert(0, 'helpers')
from linkedin_helper import (
    CDPConnection, inject_stealth, HumanSimulator,
    scrape_sent_invitations,
)

cdp = CDPConnection()
cdp.connect()
inject_stealth(cdp)
sim = HumanSimulator(cdp)

print('Scraping sent invitations page...')
result = scrape_sent_invitations(cdp, sim)
if result.get('error'):
    print(f'ERROR: {result[\"error\"]}')
    cdp.disconnect()
    sys.exit(1)
invitations = result.get('invitations', [])
for snapshot in result.get('scroll_snapshots', []):
    print(
        f'  Pass {snapshot.get(\"pass\")}: '
        f'snapshot={snapshot.get(\"snapshot_count\")}, '
        f'new={snapshot.get(\"new_count\")}, '
        f'unique={snapshot.get(\"unique_count\")}'
    )
print(f'Total invitations found: {len(invitations)}')

# Write CSV
output_path = '$OUTPUT'
with open(output_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['Name', 'LinkedIn URL', 'Headline', 'Sent'])
    for inv in invitations:
        writer.writerow([
            inv.get('name', ''),
            inv.get('url', ''),
            inv.get('headline', ''),
            inv.get('sent_text', ''),
        ])

print(f'Exported {len(invitations)} invitations to: {output_path}')
cdp.disconnect()
print('Done.')
" 2>&1
