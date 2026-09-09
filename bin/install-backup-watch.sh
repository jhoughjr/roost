#!/usr/bin/env bash
# install-backup-watch.sh - run backup-watch on a schedule, as this account.
#
# A user timer, like the reconcile's. Nothing here needs root: it reads one stamp file
# in this account's own state directory.
#
# It fires at 08:00, not at 03:30. The backup starts at 03:30 and the failure being
# watched for is the box dying part-way through, so the check has to run well after
# the window closes rather than beside it.
set -euo pipefail

DEST="${DEST:-$HOME/opt/backup-watch}"
UNITS="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "$0")" && pwd)"

install -d "$DEST" "$UNITS"
install -m 755 "$SRC/backup-watch.sh" "$DEST/backup-watch.sh"

cat > "$UNITS/backup-watch.service" <<EOF
[Unit]
Description=Say so when the nightly backup did not finish

[Service]
Type=oneshot
ExecStart=$DEST/backup-watch.sh
StandardOutput=append:$HOME/.local/state/opi-backup/watch.log
StandardError=append:$HOME/.local/state/opi-backup/watch.log
EOF

cat > "$UNITS/backup-watch.timer" <<'EOF'
[Unit]
Description=Check the nightly backup finished

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

install -d "$HOME/.local/state/opi-backup"
systemctl --user daemon-reload
systemctl --user enable --now backup-watch.timer
echo "  installed; next run:"
systemctl --user list-timers backup-watch.timer --no-pager | sed -n '2p'
