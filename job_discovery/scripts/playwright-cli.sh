#!/bin/sh
set -eu

exec npx --yes --package @playwright/cli@0.1.18 playwright-cli "$@"
