#!/bin/bash
# install-ci-live-report.sh — launchd install for ci-live-report.sh on the mini.
# Pushes each watched repo's live CI runs to the ci-live app every 20 s. Prereq:
# ~/.roost_ci_key exists (must match `dokku config ci-live CI_KEY`) and
# ROOST_CI_LIVE_REPOS/ENDPOINT are set in ~/.roostrc.
#
# StartInterval is a fixed 20 s so the poller pushes at least as often as the
# fastest per-project interval (the smallest sane project cadence is ~30 s); the
# board follows each project's advertised intervalMs regardless.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/ci-live-report.sh"
LABEL="net.jimmyhoughjr.roost-ci-live"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$HOME/.roost_ci_key" ] || { echo "install: create ~/.roost_ci_key first (chmod 600)" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>StartInterval</key><integer>20</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-ci-live.log</string>
  <key>StandardOutPath</key><string>/tmp/roost-ci-live.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed — pushing every 20 s (log: /tmp/roost-ci-live.log)"
