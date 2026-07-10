#!/bin/bash
# install-node-report.sh — launchd install for node-report.sh on this Mac.
# Reports usage to pulse every 30 s. Prereq: ~/.roost_node_key exists
# (copy it from the workstation; it must match `dokku config pulse NODE_KEY`).
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/node-report.sh"
LABEL="net.jimmyhoughjr.roost-node-report"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$HOME/.roost_node_key" ] || { echo "install: create ~/.roost_node_key first (chmod 600)" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>StartInterval</key><integer>30</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-node-report.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed — reporting every 30 s (errors: /tmp/roost-node-report.log)"
