#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
printf "Dry-run target limit: "
read LIMIT
if ! [[ "$LIMIT" =~ '^[0-9]+$' ]]; then
  echo "Limit must be a number."
  exit 1
fi
python3 scripts/withdraw_connections.py --dry-run --limit "$LIMIT"
