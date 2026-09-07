#!/usr/bin/env bash
# install-runner-watchdog.sh — put the runner watchdog on a schedule, as this account.
#
# It belongs on the opi and nowhere else. The reasoning is in the script's own header:
# every other CI alarm is a step inside the job, so when the runner is the thing that is
# down, nothing fires at all. This watches the fleet from outside it.
#
# A user timer, because the watchdog needs no root. Linger keeps user units running when
# nobody is logged in, which is the case this exists for.
set -euo pipefail

DEST="${DEST:-$HOME/opt/phoenix-watchdog}"
UNITS="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "$0")" && pwd)"

install -d "$DEST" "$UNITS"
install -m 755 "$SRC/runner-watchdog.sh" "$DEST/runner-watchdog.sh"

cat > "$UNITS/phoenix-runner-watchdog.service" <<'UNIT'
[Unit]
Description=Phoenix CI self-hosted runner watchdog (off-box fleet alerting)

[Service]
Type=oneshot
# A read-only GitHub token lives here, 0600. Everything else, the ntfy topic and the
# thresholds, comes from ~/.roostrc through the script itself.
EnvironmentFile=%h/.config/phoenix-watchdog.env
ExecStart=%h/opt/phoenix-watchdog/runner-watchdog.sh
# Best effort. A GitHub blip or an unset token must not mark the unit failed and stop
# the timer, because a stopped timer is the failure this exists to catch. Real problems
# leave by the alert channel, not by unit state.
SuccessExitStatus=0 1
TimeoutStartSec=120

[Install]
WantedBy=default.target
UNIT

cat > "$UNITS/phoenix-runner-watchdog.timer" <<'UNIT'
[Unit]
Description=Run the Phoenix CI runner watchdog every fifteen minutes

[Timer]
# An absolute schedule, and deliberately not OnBootSec with OnUnitActiveSec. Those anchor
# off boot and last activation, so a timer enabled long after boot that has never fired
# computes no next elapse and silently never runs. A watchdog that never runs is the
# failure it exists to catch.
OnCalendar=*:0/15
# Replay a window missed while the box was down, which is the case this exists for.
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now phoenix-runner-watchdog.timer
echo "  installed; next run:"
systemctl --user list-timers phoenix-runner-watchdog.timer --no-pager | sed -n '2p'
