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
# Shared key: NODE_KEY, read through lib/roost-secret.sh, and it must match `dokku config pulse NODE_KEY`.
# `roost secrets` says where this host reads it from.
set -euo pipefail
BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$BIN/roost-env.sh"
# shellcheck source=/dev/null
. "$BIN/../lib/roost-secret.sh"

KEY="$(roost_secret NODE_KEY)" || { echo "backup-report: no NODE_KEY - run 'roost secrets' to see what this host reads" >&2; exit 1; }
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
