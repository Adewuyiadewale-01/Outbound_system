#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
printf "Withdrawal prepare test limit: "
read LIMIT
if ! [[ "$LIMIT" =~ '^[0-9]+$' ]]; then
  echo "Limit must be a number."
  exit 1
fi
python3 scripts/backfill_withdrawal_countdown.py
python3 scripts/prepare_connection_withdrawals.py --limit "$LIMIT"
