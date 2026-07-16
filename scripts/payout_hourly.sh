#!/usr/bin/env bash
# Hourly custodial worker payout (invoked by aipg-payout.timer).
# systemd injects /etc/aipg/grid.env via EnvironmentFile= — do NOT re-source it
# here (bash-sourcing mangles passwords containing special chars).
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="$(dirname -- "$SCRIPT_DIR")"
cd "$APP_DIR"
PY="$APP_DIR/.venv/bin/python"
BUDGET="${PAYOUT_HOURLY_BUDGET:-208.33}"
H_START="$(date -u -d 'now -1 hour' +%Y-%m-%dT%H:00:00+00:00)"
H_END="$(date -u +%Y-%m-%dT%H:00:00+00:00)"
PERIOD="hour-$(date -u -d 'now -1 hour' +%Y-%m-%dT%H)"
"$PY" -m grid_api.services.settlement.payouts \
  --since "$H_START" --until "$H_END" --period-id "$PERIOD" \
  --budget "$BUDGET" --send
"$PY" -m grid_api.services.settlement.payouts --retry-failed
