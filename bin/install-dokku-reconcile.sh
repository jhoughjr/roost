#!/usr/bin/env bash
# install-dokku-reconcile.sh — run the reconcile on a schedule, as this account.
#
# A user timer, not a system one: dokku is drivable over ssh as this account, so
# nothing here needs root. Linger keeps user units running when nobody is logged
# in, which is the whole point after a power cut.
set -euo pipefail

DEST="${DEST:-$HOME/opt/dokku-reconcile}"
UNITS="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "$0")" && pwd)"

install -d "$DEST" "$UNITS"
install -m 755 "$SRC/dokku-reconcile.sh" "$DEST/dokku-reconcile.sh"

cat > "$UNITS/dokku-reconcile.service" <<'UNIT'
[Unit]
Description=Start dokku apps that are down and rebuild the nginx vhosts
Documentation=https://github.com/jhoughjr/roost/blob/main/bin/dokku-reconcile.sh

[Service]
Type=oneshot
ExecStart=%h/opt/dokku-reconcile/dokku-reconcile.sh
# Best effort. The script exits non-zero for the one case a person must act on —
# an app whose image is gone — and that must not mark the unit failed and stop
# the timer, because the next run is what rebuilds the proxy.
SuccessExitStatus=1
UNIT

cat > "$UNITS/dokku-reconcile.timer" <<'UNIT'
[Unit]
Description=Reconcile dokku apps and the proxy every ten minutes

[Timer]
# Absolute schedule, and deliberately not OnBootSec+OnUnitActiveSec: those
# anchor off boot and last activation, so a timer enabled long after boot that
# has never fired computes no next elapse and silently never runs. A watchdog
# that never runs is the failure it exists to catch. Learned here already, by
# the phoenix runner watchdog.
OnCalendar=*:0/10
# Replay a window missed while the box was down, which is exactly the case this
# exists for.
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now dokku-reconcile.timer
echo "  installed; next run:"
systemctl --user list-timers dokku-reconcile.timer --no-pager | sed -n '2p'
