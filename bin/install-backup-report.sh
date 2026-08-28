#!/bin/bash
# install-backup-report.sh - install backup-report.sh as an hourly service on this
# box: launchd on macOS, a systemd *user* timer on Linux (a user unit needs no
# sudo). Prereq: ~/.roost_node_key exists and matches `dokku config pulse NODE_KEY`.
#
# Install it on a machine that reaches both the service host and the repository
# host. A machine that sees only one of them pushes a half-unknown reading.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/backup-report.sh"
LABEL="net.jimmyhoughjr.roost-backup-report"

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
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-backup-report.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed - reporting every hour (errors: /tmp/roost-backup-report.log)"
;;

Linux)

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/roost-backup-report.service" <<EOF
[Unit]
Description=Report the backup service state to pulse

[Service]
Type=oneshot
ExecStart=$BIN
EOF

# OnActiveSec gives the timer its first run a minute after it is enabled, and
# Persistent catches up a run missed while the box was down.
cat > "$UNIT_DIR/roost-backup-report.timer" <<EOF
[Unit]
Description=Report the backup service state to pulse every hour

[Timer]
OnActiveSec=60
OnUnitActiveSec=1h
AccuracySec=1m
Persistent=true
Unit=roost-backup-report.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now roost-backup-report.timer
systemctl --user start roost-backup-report.service   # report now, do not wait an hour

# A user timer only survives logout, and only runs on a headless box, when the
# user has lingering enabled. Warn rather than fail: enabling it may need a
# privilege this account does not have.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "! lingering is OFF for $USER - the timer stops at logout and will not run" >&2
  echo "  after a reboot. Fix: sudo loginctl enable-linger $USER" >&2
fi

echo "✓ roost-backup-report.timer installed - reporting every hour"
echo "  logs: journalctl --user -u roost-backup-report.service -n 20"
;;

*)
  echo "install: unsupported platform $(uname -s)" >&2; exit 1 ;;
esac
