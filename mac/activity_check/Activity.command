#!/bin/zsh
set -e

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Running prepared Pre-final activity session only..."
echo

python3 scripts/check_prefinal_activity.py --activity-only

echo
echo "Done. Press Return to close this window."
read -r _
