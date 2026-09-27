#!/bin/zsh
set -e

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Running Pre-final activity checker + Final bridge..."
echo

python3 scripts/check_prefinal_activity.py

echo
echo "Done. Press Return to close this window."
read -r _
