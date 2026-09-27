#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
python3 scripts/linkedin_followup_runner.py --mode run

echo
echo "Done. Press Return to close this window."
read -r _
