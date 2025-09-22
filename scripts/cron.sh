#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"/..
if [ -f .env ]; then
  # shellcheck disable=SC1091
  source .env
fi
# Default hourly dry-run pull for the current scope
python3 scripts/pull >> logs/sync.log 2>&1
