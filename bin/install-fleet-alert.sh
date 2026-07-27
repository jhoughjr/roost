#!/bin/bash
# install-fleet-alert.sh — launchd install for fleet-alert.py on this Mac.
# Checks the fleet every 15 min and notifies on STATE CHANGES only (app
# down/recovered, disk or memory past 85%). Optional: set ROOST_NTFY_TOPIC
# in ~/.roostrc to also push to your phone via ntfy.sh.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/fleet-alert.py"
LABEL="net.jimmyhoughjr.roost-fleet-alert"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-fleet-alert.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed — checking every 15 min (errors: /tmp/roost-fleet-alert.log)"
