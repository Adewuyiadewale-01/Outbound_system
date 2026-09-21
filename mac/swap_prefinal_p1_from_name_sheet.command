#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Running Pre-final P1 swap check..."
echo

python3 scripts/swap_prefinal_p1_from_name_sheet.py

echo
echo "Done. Press Return to close this window."
read -r _