#!/bin/zsh
set -euo pipefail

cd "$(cd "$(dirname "$0")/../.." && pwd)"
echo "Monitor mode has been retired."
echo "Follow-ups now run in one sequence-driven pass from Run.command."

echo
echo "Done. Press Return to close this window."
read -r _
