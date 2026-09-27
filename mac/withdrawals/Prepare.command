#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
python3 scripts/backfill_withdrawal_countdown.py
python3 scripts/prepare_connection_withdrawals.py
