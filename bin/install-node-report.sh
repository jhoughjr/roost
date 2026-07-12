#!/bin/bash
# install-node-report.sh — install the node-report agent on this machine.
# macOS: launchd LaunchAgent. Linux: systemd user timer. Every 30 s either way.
# Prereq: ~/.roost_node_key exists (copy it from the workstation; it must
# match `dokku config pulse NODE_KEY`).
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/node-report.sh"

[ -f "$HOME/.roost_node_key" ] || { echo "install: create ~/.roost_node_key first (chmod 600)" >&2; exit 1; }

if [ "$(uname -s)" = Darwin ]; then
  LABEL="net.jimmyhoughjr.roost-node-report"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

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
else
  command -v systemctl > /dev/null || { echo "install: Linux needs systemd (no systemctl found)" >&2; exit 1; }
  UNIT_DIR="$HOME/.config/systemd/user"

  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/roost-node-report.service" <<EOF
[Unit]
Description=roost node report → pulse

[Service]
Type=oneshot
ExecStart=$BIN
EOF
  cat > "$UNIT_DIR/roost-node-report.timer" <<EOF
[Unit]
Description=roost node report every 30 s

[Timer]
OnBootSec=30
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now roost-node-report.timer
  # Without linger the user manager (and this timer) dies at logout.
  if command -v loginctl > /dev/null && [ "$(loginctl show-user "$USER" --property Linger --value 2>/dev/null)" != "yes" ]; then
    echo "⚠ run once: sudo loginctl enable-linger $USER   (else reporting stops when you log out)"
  fi
  echo "✓ roost-node-report.timer installed — reporting every 30 s (logs: journalctl --user -u roost-node-report)"
fi
