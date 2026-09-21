#!/bin/bash
# Acceptance-check launcher — runs on the real Mac with full ~/.openclaw access.
# Double-click in Finder or run from Terminal.
#
# This script must run on the Mac (not in a sandbox) because it needs:
#   1. ~/.openclaw/credentials/google-sheets.json  (Sheets auth)
#   2. Chrome CDP on localhost:18800               (browser control)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="$REPO_ROOT/state/acceptance_monitoring/last_run.log"
DONE="$REPO_ROOT/state/acceptance_monitoring/last_run.done"
TODAY=$(date +"%m/%d/%Y")

mkdir -p "$(dirname "$LOG")"
rm -f "$DONE"

# --- Pre-flight: verify credentials and CDP are reachable ---
CREDS="$HOME/.openclaw/credentials/google-sheets.json"
CDP_URL="http://localhost:18800/json/version"

preflight_ok=true

if [ ! -f "$CREDS" ]; then
    echo "ERROR: Credentials not found at $CREDS" | tee "$LOG"
    preflight_ok=false
fi

if ! curl -s --max-time 5 "$CDP_URL" > /dev/null 2>&1; then
    echo "WARNING: Chrome CDP not reachable on port 18800." | tee -a "$LOG"
    echo "  Run: bash scripts/launch-chrome.sh" | tee -a "$LOG"
    echo "  Then re-run this script." | tee -a "$LOG"
    preflight_ok=false
fi

if [ "$preflight_ok" = false ]; then
    echo "=== PREFLIGHT FAILED ===" | tee -a "$LOG"
    touch "$DONE"
    exit 1
fi

# --- Run acceptance check ---
{
  echo "=== Acceptance-check run: $(date) ==="
  echo "=== Date: $TODAY ==="
  cd "$REPO_ROOT"
  # TODO(workflow-2): retarget when outreach/OBF is carved into outbound/outreach/ (scripts/ entry point)
python3 helpers/linkedin_outreach_session.py acceptance-check --date "$TODAY" 2>&1
  EXIT_CODE=$?
  echo "=== EXIT CODE: $EXIT_CODE ==="
  echo "=== Run complete ==="
} | tee "$LOG"

touch "$DONE"
