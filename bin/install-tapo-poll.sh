#!/bin/bash
# install-tapo-poll.sh — install tapo-poll.py as a long-running service on this
# box: launchd on macOS, a systemd *user* service on Linux (opi has no sudo for
# the runner user, and a user unit needs none).
#
# Unlike node-report (a 30 s one-shot timer), this runs ONE persistent process
# in --watch mode. A KLAP session costs a two-round-trip handshake per device;
# re-paying that every 30 s from a fresh process would be most of the work, and
# holding the session open is what makes sub-30 s polling affordable later.
#
# Also creates ~/.roost-tapo-venv and installs python-kasa into it — roost's
# only non-stdlib dependency, deliberately quarantined off the system python so
# the rest of the toolbelt stays manifest-free.
#
# Prereqs: ~/.tapo_pass (TP-Link account password, chmod 600) and ROOST_TAPO_EMAIL
# + ROOST_TAPO_DEVICES in ~/.roostrc. Run `tapo-poll.py --discover` first to list
# device IPs (that works without credentials).
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)/tapo-poll.py"
VENV="$HOME/.roost-tapo-venv"
LABEL="net.jimmyhoughjr.roost-tapo-poll"

[ -f "$HOME/.tapo_pass" ] || { echo "install: create ~/.tapo_pass first (chmod 600)" >&2; exit 1; }
grep -q '^ROOST_TAPO_EMAIL=' "$HOME/.roostrc" 2>/dev/null \
  || { echo "install: set ROOST_TAPO_EMAIL in ~/.roostrc first" >&2; exit 1; }
grep -q '^ROOST_TAPO_DEVICES=' "$HOME/.roostrc" 2>/dev/null \
  || { echo "install: set ROOST_TAPO_DEVICES in ~/.roostrc first" >&2; exit 1; }

if [ ! -x "$VENV/bin/python3" ]; then
  echo "· creating $VENV"
  python3 -m venv "$VENV"
fi
echo "· installing python-kasa into $VENV"
"$VENV/bin/pip" install --quiet --upgrade pip python-kasa

# Fail loudly here rather than in a service that just restart-loops.
"$VENV/bin/python3" "$BIN" --json >/dev/null \
  || { echo "install: tapo-poll could not read the devices — fix that before installing the service" >&2; exit 1; }

case "$(uname -s)" in
Darwin)

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string><string>--watch</string></array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-tapo-poll.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed — polling continuously (errors: /tmp/roost-tapo-poll.log)"
;;

Linux)

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/roost-tapo-poll.service" <<EOF
[Unit]
Description=Poll Tapo smart plugs, cache locally and report to pulse

[Service]
Type=simple
ExecStart=$BIN --watch
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now roost-tapo-poll.service

# A user unit only survives logout — and only runs at all on a headless box —
# when the user has lingering enabled. Warn rather than fail: enabling it may
# need a privilege this account doesn't have.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "! lingering is OFF for $USER — the poller stops at logout and won't run" >&2
  echo "  after a reboot. Fix: sudo loginctl enable-linger $USER" >&2
fi

echo "✓ roost-tapo-poll.service installed"
echo "  logs: journalctl --user -u roost-tapo-poll.service -n 20"
;;

*)
  echo "install: unsupported platform $(uname -s)" >&2; exit 1 ;;
esac
