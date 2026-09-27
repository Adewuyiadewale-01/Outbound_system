#!/bin/zsh
set -e

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Running limited Pre-final activity checker + Final bridge..."
echo
printf "Limit P1/P2 activity targets to process: "
read -r LIMIT

if ! [[ "$LIMIT" =~ '^[0-9]+$' ]] || [[ "$LIMIT" -lt 1 ]]; then
  echo "Limit must be a positive number."
  echo
  echo "Press Return to close this window."
  read -r _
  exit 1
fi

echo
python3 scripts/check_prefinal_activity.py --limit "$LIMIT"

echo
echo "Done. Press Return to close this window."
read -r _
