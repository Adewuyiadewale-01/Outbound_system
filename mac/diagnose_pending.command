#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
python3 -c "
import sys, json
sys.path.insert(0, 'helpers')
from outreach_helper import get_pending_connections

import os
creds = os.path.expanduser('~/.openclaw/credentials/google-sheets.json')
pending = get_pending_connections(credentials_path=creds)
print(f'Total pending: {len(pending)}')
print()
for i, p in enumerate(pending):
    url = (p.get('contact_linkedin') or '').strip()
    name = p.get('contact_name', '')
    status = p.get('status', '')
    row = p.get('_row_number', '')
    print(f'{i+1}. {name}')
    print(f'   URL: {repr(url)}')
    print(f'   Status: {status}')
    print(f'   Row: {row}')
    print()
"