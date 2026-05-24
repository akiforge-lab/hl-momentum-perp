#!/usr/bin/env bash
# Template for a local dry-run wrapper. Copy to dryrun.local.sh (gitignored)
# and edit paths/flags to taste.
#
#   cp scripts/dryrun.example.sh dryrun.local.sh
#   chmod +x dryrun.local.sh
#   ./dryrun.local.sh        # continuous loop
#   ./dryrun.local.sh --once # single cycle
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "no .venv — create one with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python main.py "$@"
