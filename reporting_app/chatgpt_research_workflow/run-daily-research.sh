#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_DIR="$(cd "$WORKFLOW_DIR/../../.." && pwd)"
if [[ -f "$REPOSITORY_DIR/.env" ]]; then
  set -a
  source "$REPOSITORY_DIR/.env"
  set +a
fi
cd "$WORKFLOW_DIR"

SHEET_URL="${SHEET_URL:-}"
SHEET_TAB="${SHEET_TAB:-Messaging}"
CDP_URL="${CDP_URL:-http://127.0.0.1:9222}"
CHATGPT_PROFILE_DIR="${CHATGPT_PROFILE_DIR:-$WORKFLOW_DIR/.chatgpt-manual-profile}"
DOC_WEBHOOK_URL="${DOC_WEBHOOK_URL:-}"
DOC_WEBHOOK_SECRET="${DOC_WEBHOOK_SECRET:-}"
EXTRA_ARGS=()

if [[ -z "$SHEET_URL" ]]; then
  echo "SHEET_URL is required. Set it in the environment; see the repository .env.example." >&2
  exit 2
fi

if [[ "${DRY_RUN:-}" != "1" && "${DRY_RUN:-}" != "true" ]] && { [[ -z "$DOC_WEBHOOK_URL" ]] || [[ -z "$DOC_WEBHOOK_SECRET" ]]; }; then
  echo "DOC_WEBHOOK_URL and DOC_WEBHOOK_SECRET are required outside dry-run mode." >&2
  exit 2
fi

if [[ -n "${LIMIT:-}" ]]; then
  EXTRA_ARGS+=(--limit "$LIMIT")
fi

if [[ "${DRY_RUN:-}" == "1" || "${DRY_RUN:-}" == "true" ]]; then
  EXTRA_ARGS+=(--dry-run)
fi

if [[ -n "${GOOGLE_DOC_ID:-}" ]]; then
  EXTRA_ARGS+=(--google-doc-id "$GOOGLE_DOC_ID")
fi

if [[ -n "${GOOGLE_DOC_URL:-}" ]]; then
  EXTRA_ARGS+=(--google-doc-url "$GOOGLE_DOC_URL")
fi

if [[ "${DRY_RUN:-}" != "1" && "${DRY_RUN:-}" != "true" ]] && ! curl -fsS "$CDP_URL/json/version" >/dev/null 2>&1; then
  open -na "Google Chrome" --args \
    --remote-debugging-port=9222 \
    --user-data-dir="$CHATGPT_PROFILE_DIR" \
    "https://chatgpt.com/" >/dev/null 2>&1

  for _ in {1..30}; do
    if curl -fsS "$CDP_URL/json/version" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

CMD=(
  npm run research --
  --sheet-url "$SHEET_URL"
  --sheet "$SHEET_TAB"
  --cdp-url "$CDP_URL"
  --doc-webhook-url "$DOC_WEBHOOK_URL"
  --doc-webhook-secret "$DOC_WEBHOOK_SECRET"
)

if ((${#EXTRA_ARGS[@]})); then
  CMD+=("${EXTRA_ARGS[@]}")
fi

"${CMD[@]}"
