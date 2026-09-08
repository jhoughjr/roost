#!/usr/bin/env bash
# lan-cert.sh <name> <user@host> <dir> [launchd label]
#
# A certificate for a LAN name, issued through DNS-01 and placed on the machine that serves it.
#
# The standby coop on the mini serves TLS at coop-mini.jimmyhoughjr.net, a name the LAN's dnsmasq answers with the mini's
# address, because the vault cookie travels only to the domain over https. The mini runs no container engine, so the
# certificate is issued here, on a box with docker, in the certbot image the forge's own certificate came from, and the
# two files go to the serving host over ssh. Nothing is exposed: DNS-01 needs no inbound request.
#
# One run does the whole job and is safe to repeat. certbot issues when there is no certificate and renews when the one
# it holds is within thirty days of expiry, otherwise it leaves it alone. The files go to the host only when they changed,
# and the serving job is kicked only then.
#
# Requires: docker, ~/.cf_api_token (chmod 600) with DNS edit on the zone, and key auth to <user@host>.
# State: ~/.lan-cert/<name>, certbot's own directory, kept so a renew is a renew and not a new issue.
set -euo pipefail

NAME="${1:-}"; HOST="${2:-}"; DIR="${3:-}"; LABEL="${4:-}"
[ -n "$NAME" ] && [ -n "$HOST" ] && [ -n "$DIR" ] || { sed -n '2,2p' "$0" | sed 's/^# //' >&2; exit 1; }
TOKEN_FILE="$HOME/.cf_api_token"
[ -s "$TOKEN_FILE" ] || { echo "lan-cert: no token in $TOKEN_FILE" >&2; exit 1; }
STATE="$HOME/.lan-cert/$NAME"
IMAGE="certbot/dns-cloudflare:latest"
# shellcheck source=/dev/null
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc"

say() { printf '%s\n' "$*"; }

# The credentials file lives in the state directory, which is this account's and mode 700, and never on a command line.
mkdir -p "$STATE"
chmod 700 "$HOME/.lan-cert" "$STATE"
umask 077
printf 'dns_cloudflare_api_token = %s\n' "$(cat "$TOKEN_FILE")" > "$STATE/cloudflare.ini"

email_args=(--register-unsafely-without-email)
[ -n "${ROOST_ACME_EMAIL:-}" ] && email_args=(-m "$ROOST_ACME_EMAIL")

# --keep-until-expiring makes the call idempotent: issue when absent, renew when due, otherwise do nothing.
docker run --rm -v "$STATE:/etc/letsencrypt" "$IMAGE" certonly \
  --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  --non-interactive --agree-tos "${email_args[@]}" \
  --keep-until-expiring -d "$NAME" > "$STATE/last.log" 2>&1 || { cat "$STATE/last.log" >&2; exit 1; }
grep -qi "not yet due\|Successfully received\|Certificate not yet due" "$STATE/last.log" && say "  certbot: $(grep -i -m1 'not yet due\|Successfully received' "$STATE/last.log" | sed 's/^ *//')"

# The live files are root-owned symlinks inside the state directory, so a container reads them out.
read_file() { docker run --rm -v "$STATE:/le:ro" alpine cat "/le/live/$NAME/$1"; }
local_sum=$(read_file fullchain.pem | sha256sum | cut -c1-64)
remote_sum=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "shasum -a 256 '$DIR/fullchain.pem' 2>/dev/null | cut -c1-64" || true)

if [ "$local_sum" = "$remote_sum" ]; then
  say "lan-cert: $NAME unchanged on $HOST, expires $(read_file fullchain.pem | openssl x509 -noout -enddate | cut -d= -f2)"
  exit 0
fi

# The files cross over the pipe with a private umask on the far side, and the key is never written where a listing could show it.
ssh -o BatchMode=yes "$HOST" "umask 077; mkdir -p '$DIR'"
read_file fullchain.pem | ssh -o BatchMode=yes "$HOST" "umask 077; cat > '$DIR/fullchain.pem'"
read_file privkey.pem | ssh -o BatchMode=yes "$HOST" "umask 077; cat > '$DIR/privkey.pem'"
if [ -n "$LABEL" ]; then
  ssh -o BatchMode=yes "$HOST" "launchctl kickstart -k gui/\$(id -u)/$LABEL" && say "  $LABEL restarted on $HOST"
fi
say "lan-cert: $NAME placed on $HOST, expires $(read_file fullchain.pem | openssl x509 -noout -enddate | cut -d= -f2)"
