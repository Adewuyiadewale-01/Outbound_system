#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Dry run will inspect LinkedIn threads but will not send messages or update Pipeline."
printf "Limit how many due leads? Leave blank for all: "
read -r LIMIT

if [[ -n "${LIMIT}" ]]; then
  python3 scripts/linkedin_followup_runner.py --mode run --dry-run --limit "${LIMIT}"
else
  python3 scripts/linkedin_followup_runner.py --mode run --dry-run
fi

echo
echo "Done. Press Return to close this window."
read -r _
