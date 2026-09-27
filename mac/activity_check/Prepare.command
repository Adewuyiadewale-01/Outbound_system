#!/bin/zsh
set -e

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Preparing Pre-final activity local session..."
echo

python3 scripts/check_prefinal_activity.py --prepare-only

echo
echo "Done. Press Return to close this window."
read -r _
