#!/usr/bin/env bash
# mesh-alert.sh — say one line over LoRa, when the network cannot carry it.
#
# The estate already reports to pulse, and pulse is on the network. When nginx
# wedges, or DNS goes sideways, or the tunnel drops, the reporting path dies with
# the thing it was reporting on — which is how a broken proxy went unnoticed for
# sixteen hours. Meshtastic is the one channel that does not share a fate with
# any of that: its own radio, its own link, no LAN and no internet.
#
#   mesh-alert.sh "opi: status imageless, proxy rebuilt"
#
# MESH_DEST is the node to tell, as its id (!a1b2c3d4). Without one this does
# nothing and says so, rather than broadcasting the estate's state to whoever is
# listening.
set -uo pipefail

DEST="${MESH_DEST:-}"
PORT="${MESH_PORT:-/dev/ttyUSB0}"
# LoRa payloads are small and the band is duty-cycled. A line long enough to
# need two packets is a line that will not arrive whole.
LIMIT="${MESH_LIMIT:-160}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/mesh-alert.last"
# A fault that persists would otherwise send the same line every ten minutes —
# a hundred and forty-four transmissions a day saying nothing new, on a band
# where airtime is the scarce thing.
QUIET_HOURS="${MESH_QUIET_HOURS:-6}"

message="${1:-}"
[ -n "$message" ] || { echo "mesh-alert: nothing to say" >&2; exit 0; }
message="$(printf '%s' "$message" | tr '\n' ' ' | cut -c1-"$LIMIT")"

if [ -z "$DEST" ]; then
  echo "mesh-alert: MESH_DEST is unset, so nothing was sent: $message" >&2
  exit 0
fi
command -v meshtastic >/dev/null 2>&1 || {
  echo "mesh-alert: no meshtastic cli, so nothing was sent: $message" >&2
  exit 0
}
[ -r "$PORT" ] || {
  echo "mesh-alert: cannot read $PORT (dialout group?), so nothing was sent: $message" >&2
  exit 0
}

install -d "$(dirname "$STATE")" 2>/dev/null || true
last_line=$(sed -n 1p "$STATE" 2>/dev/null || true)
last_at=$(sed -n 2p "$STATE" 2>/dev/null || echo 0)
now=$(date +%s)
age=$(( now - ${last_at:-0} ))

if [ "$message" = "$last_line" ] && [ "$age" -lt $(( QUIET_HOURS * 3600 )) ]; then
  echo "mesh-alert: unchanged and sent $(( age / 60 ))m ago, held back" >&2
  exit 0
fi

# Best effort, always. An alert path that can fail the thing it watches is worse
# than one that stays quiet: the reconcile must finish whatever the radio does.
if timeout 60 meshtastic --port "$PORT" --dest "$DEST" --sendtext "$message" >/dev/null 2>&1; then
  printf '%s\n%s\n' "$message" "$now" > "$STATE" 2>/dev/null || true
  echo "mesh-alert: sent to $DEST: $message" >&2
else
  echo "mesh-alert: the radio did not take it: $message" >&2
fi
exit 0
