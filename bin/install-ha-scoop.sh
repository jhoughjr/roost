#!/bin/bash
# install-ha-scoop.sh — run ha-scoop.py once an hour: launchd on macOS, a
# systemd *user* timer on Linux (same no-sudo constraint as the poller).
#
# Unlike the poller this is emphatically NOT a long-running process. Home
# Assistant computes one statistics row per entity per hour, so anything more
# frequent than hourly re-reads numbers that cannot have changed. It fires at
# :10 past, because HA finalises the hour a few minutes after it ends and a
# scoop at :00 would consistently miss the hour it just completed.
#
# Overlap with the previous run is deliberate: ROOST_HA_SCOOP_DAYS defaults to
# 2, so every run re-sends ~48 hours. pulse upserts on (device, hour), making
# that idempotent, and it means a missed run — laptop asleep, HA restarting —
# heals itself on the next tick instead of leaving a permanent hole.
#
# Prereqs: ~/.ha_token (chmod 600) and ROOST_HA_ENTITIES in ~/.roostrc. Run
# `ha-scoop.py --entities` to list what HA actually exposes, and note that the
# labels must match ROOST_TAPO_DEVICES or the two sources describe the same
# plug under two names.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$DIR/roost-env.sh"

BIN="$DIR/ha-scoop.py"
LABEL="net.jimmyhoughjr.roost-ha-scoop"

[ -f "$HOME/.ha_token" ] || { echo "install: create ~/.ha_token first (chmod 600)" >&2; exit 1; }
grep -q '^ROOST_HA_ENTITIES=' "$HOME/.roostrc" 2>/dev/null \
  || { echo "install: set ROOST_HA_ENTITIES in ~/.roostrc first (label=entity_id)" >&2
       echo "  list them with: $BIN --entities" >&2; exit 1; }

# Fail here rather than in a timer whose failures nobody reads.
"$BIN" --json --days 1 >/dev/null \
  || { echo "install: ha-scoop could not read HA — fix that before scheduling it" >&2; exit 1; }

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
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>10</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/roost-ha-scoop.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ $LABEL installed — scooping hourly (errors: /tmp/roost-ha-scoop.log)"
;;

Linux)

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/roost-ha-scoop.service" <<EOF
[Unit]
Description=Copy Home Assistant hourly power statistics into pulse

[Service]
Type=oneshot
ExecStart=$BIN
EOF

cat > "$UNIT_DIR/roost-ha-scoop.timer" <<EOF
[Unit]
Description=Hourly Home Assistant statistics scoop

[Timer]
OnCalendar=*:10
# The box reboots and HA restarts; a scoop skipped while down should run on
# the way back up rather than wait for the next hour.
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now roost-ha-scoop.timer

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "! lingering is OFF for $USER — the timer stops at logout and won't run" >&2
  echo "  after a reboot. Fix: sudo loginctl enable-linger $USER" >&2
fi

echo "✓ roost-ha-scoop.timer installed — next: $(systemctl --user list-timers roost-ha-scoop.timer --no-pager 2>/dev/null | sed -n 2p)"
;;

*)
echo "install: unsupported platform $(uname -s)" >&2
exit 1
;;
esac
