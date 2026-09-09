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
[ -f "$SRC/mesh-alert.sh" ] && install -m 755 "$SRC/mesh-alert.sh" "$DEST/mesh-alert.sh"
# The script reads NODE_KEY through the secret reader, so the reader travels with it. Without
# this the installed copy sources a file that is not there and reports nothing, every ten minutes.
install -d "$DEST/lib"
install -m 644 "$SRC/../lib/roost-secret.sh" "$DEST/lib/roost-secret.sh"

cat > "$UNITS/dokku-reconcile.service" <<'UNIT'
[Unit]
Description=Start dokku apps that are down and rebuild the nginx vhosts
Documentation=https://github.com/jhoughjr/roost/blob/main/bin/dokku-reconcile.sh
# OnFailure is a [Unit] directive. In [Service] systemd ignores it with one line
# in the journal and the alert never fires.
OnFailure=dokku-reconcile-alert.service

[Service]
Type=oneshot
ExecStart=%h/opt/dokku-reconcile/dokku-reconcile.sh
# Deliberately NOT SuccessExitStatus=1 any more. The non-zero exit is the one
# case a person must act on, and OnFailure is how that reaches them — treating
# it as success would keep the timer healthy and tell nobody.
UNIT

# Told over LoRa, because everything else reports over the network that breaks.
cat > "$UNITS/dokku-reconcile-alert.service" <<'UNIT'
[Unit]
Description=Say over the mesh that the reconcile found something it cannot fix

[Service]
Type=oneshot
EnvironmentFile=-%h/.config/mesh-alert.env
# The last line the reconcile wrote is the whole message. A person needs to know
# to go and look, not to be told what to think.
ExecStart=/bin/sh -c 'exec %h/opt/dokku-reconcile/mesh-alert.sh "opi: $(journalctl --user -u dokku-reconcile.service -n 20 --no-pager -o cat 2>/dev/null | grep -E "redeploy needed|no image" | tail -1 | cut -c1-120)"'
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
