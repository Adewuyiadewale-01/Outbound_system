#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"

echo "Limited live run. This can send messages after safety checks pass."
printf "How many due leads should it process? "
read -r LIMIT

if [[ -z "${LIMIT}" ]]; then
  echo "No limit entered. Stopping."
  echo "Press Return to close this window."
  read -r _
  exit 1
fi

python3 scripts/linkedin_followup_runner.py --mode run --limit "${LIMIT}"

echo
echo "Done. Press Return to close this window."
read -r _
