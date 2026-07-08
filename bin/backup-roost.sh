#!/usr/bin/env bash
# backup-roost.sh — nightly pull of the pi's persistent data to this Mac.
# Tars each storage mount from inside a container (the only channel we
# have is dokku@), keeps 14 days. Run via launchd; safe to run any time.
set -euo pipefail
DOKKU="dokku@192.168.0.103"
DEST="$HOME/Backups/roost"
STAMP="$(date +%F)"
mkdir -p "$DEST"

backup() {  # backup <label> <app> <path-in-container>
  local out="$DEST/$1-$STAMP.tgz"
  if ssh -o BatchMode=yes "$DOKKU" run "$2" tar -czf - "$3" > "$out" 2>/dev/null && [ -s "$out" ]; then
    echo "✓ $1 → $out ($(du -h "$out" | cut -f1))"
  else
    rm -f "$out"; echo "✗ $1 FAILED"
  fi
}

backup vault-data  vault /data
backup watts-rates watts /usr/share/nginx/html/data

# retention: 14 days
find "$DEST" -name '*.tgz' -mtime +14 -delete
