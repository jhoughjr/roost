#!/usr/bin/env bash
# install-lan-cert.sh - renew a LAN name's certificate on a schedule, as this account.
#
# A user timer on a Linux box with docker. Daily is often enough, because lan-cert.sh renews only inside the last
# thirty days and does nothing otherwise. Linger keeps user units running when nobody is logged in.
#
# Config, via ~/.roostrc KEY=VALUE lines:
#   ROOST_LAN_CERT_NAME    the name, e.g. coop-mini.jimmyhoughjr.net (required)
#   ROOST_LAN_CERT_HOST    user@host that serves it, e.g. jimmyhoughjr@jimmys-mac-mini.local (required)
#   ROOST_LAN_CERT_DIR     the directory on that host, e.g. /Users/jimmyhoughjr/coop/tls (required)
#   ROOST_LAN_CERT_LABEL   a launchd label to kick after a change, e.g. net.jimmyhoughjr.coop (optional)
#   ROOST_ACME_EMAIL       the account email for Let's Encrypt (optional)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${DEST:-$HOME/opt/lan-cert}"
UNITS="$HOME/.config/systemd/user"
# shellcheck source=/dev/null
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc"

[ "$(uname -s)" = "Linux" ] || { echo "install: the issuing box needs docker and a systemd user timer, so this installs on Linux only" >&2; exit 1; }
for key in ROOST_LAN_CERT_NAME ROOST_LAN_CERT_HOST ROOST_LAN_CERT_DIR; do
  [ -n "${!key:-}" ] || { echo "install: set $key in ~/.roostrc" >&2; exit 1; }
done
[ -s "$HOME/.cf_api_token" ] || { echo "install: put the Cloudflare token in ~/.cf_api_token (chmod 600) first" >&2; exit 1; }
command -v docker > /dev/null || { echo "install: docker is not on this box" >&2; exit 1; }

install -d "$DEST" "$UNITS"
install -m 755 "$SRC/lan-cert.sh" "$DEST/lan-cert.sh"
cat > "$UNITS/lan-cert.service" <<UNIT
[Unit]
Description=Issue or renew the certificate for $ROOST_LAN_CERT_NAME and place it on $ROOST_LAN_CERT_HOST
Documentation=https://github.com/jhoughjr/roost/blob/main/bin/lan-cert.sh
[Service]
Type=oneshot
ExecStart=$DEST/lan-cert.sh $ROOST_LAN_CERT_NAME $ROOST_LAN_CERT_HOST $ROOST_LAN_CERT_DIR ${ROOST_LAN_CERT_LABEL:-}
UNIT
cat > "$UNITS/lan-cert.timer" <<UNIT
[Unit]
Description=Renew the certificate for $ROOST_LAN_CERT_NAME daily
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now lan-cert.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "✓ lan-cert.timer installed - $ROOST_LAN_CERT_NAME renews daily when due, placed on $ROOST_LAN_CERT_HOST"
echo "  run it now: systemctl --user start lan-cert.service && journalctl --user -u lan-cert.service -n 5 --no-pager"
