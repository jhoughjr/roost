#!/usr/bin/env bash
# backup-report.sh - push the backup service reading to pulse.
#
# It runs backup-status.py and POSTs the JSON to pulse `/api/backups`, where the
# dashboard draws a card. install-backup-report.sh runs it every hour. Backups
# move once a night, so a faster push says nothing new.
#
# Run it on a machine that reaches both hosts. backup-status.py reads the
# schedule from ROOST_BACKUP_SERVICE_HOST and the repositories from
# ROOST_BACKUP_REPO_HOST, and it reports a host it cannot reach as unknown.
# A reading from a machine that sees only one host is worse than none, because
# the card then shows half the service as unknown for a reason outside itself.
#
# Config:
#   ROOST_PULSE_URL    pulse base URL (default: https://pulse.jimmyhoughjr.net)
#   ROOST_BACKUP_*     the hosts and paths, see backup-status.py
#
# Shared key: ~/.roost_node_key (chmod 600), must match `dokku config pulse NODE_KEY`.
set -euo pipefail
BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$BIN/roost-env.sh"

KEY_FILE="$HOME/.roost_node_key"
[ -f "$KEY_FILE" ] || { echo "backup-report: missing $KEY_FILE (the pulse NODE_KEY)" >&2; exit 1; }
KEY="$(tr -d '\r\n' < "$KEY_FILE")"
PULSE=${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}

# backup-status.py exits non-zero on a red reading, and a red reading is the one
# most worth pushing. So the exit code is ignored here, and only an empty
# reading counts as a failure.
READING="$(python3 "$BIN/backup-status.py" --json || true)"
[ -n "$READING" ] || { echo "backup-report: backup-status.py returned nothing" >&2; exit 1; }

curl -sf -m 20 -X POST "$PULSE/api/backups" \
  -H "content-type: application/json" \
  -H "x-roost-node-key: $KEY" \
  --data "$READING" > /dev/null

echo "backup-report: pushed to $PULSE"
