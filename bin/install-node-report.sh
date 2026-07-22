#!/bin/bash
# install-node-report.sh — install node-report.sh as a 30 s service on this box:
# launchd on macOS, a systemd *user* timer on Linux (opi has no sudo for the
# runner user, and a user unit needs none). Prereq: ~/.roost_node_key exists
# (copy it from the workstation; it must match `dokku config pulse NODE_KEY`).
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/node-report.sh"
LABEL="net.jimmyhoughjr.roost-node-report"

[ -f "$HOME/.roost_node_key" ] || { echo "install: create ~/.roost_node_key first (chmod 600)" >&2; exit 1; }

case "$(uname -s)" in
Darwin)

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
;;

Linux)

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/roost-node-report.service" <<EOF
[Unit]
Description=Report this node's usage to pulse

[Service]
Type=oneshot
ExecStart=$BIN
EOF

# OnUnitActiveSec alone only re-arms *after* a first run, and OnBootSec never
# fires for a user manager started long after boot — so OnActiveSec gives the
# timer its first shot 30 s after it is started/enabled, and Persistent catches
# up a run missed while the box was down.
cat > "$UNIT_DIR/roost-node-report.timer" <<EOF
[Unit]
Description=Report this node's usage to pulse every 30 s

[Timer]
OnActiveSec=30
OnUnitActiveSec=30
AccuracySec=5s
Persistent=true
Unit=roost-node-report.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now roost-node-report.timer
systemctl --user start roost-node-report.service   # report immediately, don't wait 30 s

# A user timer only survives logout — and only runs at all on a headless box —
# when the user has lingering enabled. Warn rather than fail: enabling it may
# need a privilege this account doesn't have.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "! lingering is OFF for $USER — the timer stops at logout and won't run" >&2
  echo "  after a reboot. Fix: sudo loginctl enable-linger $USER" >&2
fi

echo "✓ roost-node-report.timer installed — reporting every 30 s"
echo "  logs: journalctl --user -u roost-node-report.service -n 20"
;;

*)
  echo "install: unsupported platform $(uname -s)" >&2; exit 1 ;;
esac
