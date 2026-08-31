#!/bin/bash
# install-colima-ensure.sh - launchd install for colima-ensure.sh on a Mac.
#
# Brings the colima VM back after a reboot and after a crash, so this Mac keeps
# the amd64 capability that its Rosetta handler provides. colima-ensure.sh
# carries the reasoning.
#
# StartInterval is 300 s. The VM is not something which fails often, and the
# check costs one `colima status` call, so a five minute cadence catches a
# reboot quickly and costs nothing between reboots. RunAtLoad covers the boot
# itself, which is the case this exists for.
#
# A LaunchAgent loads at login rather than at boot, so a headless Mac needs
# automatic login for this to survive a reboot. This script says so if it is
# not set, because without it the agent is silent in exactly the case it was
# installed to cover.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/colima-ensure.sh"
LABEL="net.jimmyhoughjr.roost-colima"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="/tmp/roost-colima.log"

[ -x "$BIN" ] || { echo "install: $BIN is missing or not executable" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>StandardOutPath</key><string>$LOG</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed - checking every 300 s (log: $LOG)"

if ! defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser > /dev/null 2>&1; then
  echo "! automatic login is off, so this agent does not load after a reboot"
  echo "  until someone logs in. Turn it on for a headless Mac."
fi
