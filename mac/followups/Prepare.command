#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
python3 scripts/linkedin_followup_runner.py --prepare-only

echo
echo "Done. Press Return to close this window."
read -r _
