#!/bin/bash
# node-report.sh — report this Mac's usage to pulse as a roost node.
#
# One-shot: run it and pulse's /api/stats gains a `nodes` entry for this
# machine (shown on watts.jimmyhoughjr.net/roost/). Run it from launchd
# every 30 s for a live feed — see install-node-report.sh.
#
# Config (all optional), via ~/.roostrc KEY=VALUE lines:
#   ROOST_NODE_NAME    node name  (default: ComputerName, lowercased a-z0-9-)
#   ROOST_NODE_IDLE_W  idle watts (default: 5  — Apple-silicon Mac mini/laptop)
#   ROOST_NODE_MAX_W   full-tilt watts (default: 40 — M-series mini under load)
#   ROOST_PULSE_URL    pulse base URL (default: https://pulse.jimmyhoughjr.net)
# Shared key: ~/.roost_node_key (chmod 600), must match `dokku config pulse NODE_KEY`.
set -euo pipefail

RC="$HOME/.roostrc"
[ -f "$RC" ] && . "$RC"

KEY_FILE="$HOME/.roost_node_key"
[ -f "$KEY_FILE" ] || { echo "node-report: missing $KEY_FILE (the pulse NODE_KEY)" >&2; exit 1; }
KEY=$(cat "$KEY_FILE")

NAME=${ROOST_NODE_NAME:-$(scutil --get ComputerName | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-*$//;s/^-*//' | cut -c1-32)}
IDLE_W=${ROOST_NODE_IDLE_W:-5}
MAX_W=${ROOST_NODE_MAX_W:-40}
PULSE=${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}

LOAD1=$(sysctl -n vm.loadavg | awk '{print $2}')
CORES=$(sysctl -n hw.ncpu)
MODEL=$(sysctl -n hw.model)
MEM_TOTAL_MB=$(( $(sysctl -n hw.memsize) / 1048576 ))
# Used ≈ total − (free + inactive + speculative + purgeable) pages.
PAGE=$(sysctl -n hw.pagesize)
FREE_PAGES=$(vm_stat | awk -F'[: .]+' '
  /Pages free/ {f=$3} /Pages inactive/ {i=$3} /Pages speculative/ {s=$3} /Pages purgeable/ {p=$3}
  END {print f+i+s+p}')
MEM_USED_MB=$(( MEM_TOTAL_MB - FREE_PAGES * PAGE / 1048576 ))

# Disk: the APFS data volume (fall back to /); used = size − available,
# since APFS volumes share the container's free space.
DISK=$(df -k /System/Volumes/Data 2>/dev/null | awk 'NR==2{print $2, $4}')
[ -n "$DISK" ] || DISK=$(df -k / | awk 'NR==2{print $2, $4}')
DISK_TOTAL_MB=$(( ${DISK%% *} / 1024 ))
DISK_USED_MB=$(( ( ${DISK%% *} - ${DISK##* } ) / 1024 ))

# Networking: default-route interface → LAN IP + cumulative link bytes.
# pulse turns consecutive byte counters into live ↓/↑ rates for the map.
NET_JSON=""
IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
if [ -n "$IFACE" ]; then
  IP=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)
  read -r RX_B TX_B <<< "$(netstat -ibn -I "$IFACE" | awk 'NR==2{print $7, $10}')"
  [ -n "$IP" ] && NET_JSON=",\"ip\":\"$IP\",\"iface\":\"$IFACE\",\"netRxB\":${RX_B:-0},\"netTxB\":${TX_B:-0}"
fi

# Measured system watts via macmon (sudoless SMC read, Apple silicon) when
# installed; when absent the field is omitted and pulse/watts fall back to
# the idleW/maxW load estimate (shown with a ~ on the roost page).
WATTS_JSON=""
for MACMON in "$(command -v macmon || true)" /opt/homebrew/bin/macmon /usr/local/bin/macmon; do
  [ -n "$MACMON" ] && [ -x "$MACMON" ] || continue
  SYS_W=$("$MACMON" pipe -s 1 2>/dev/null | grep -oE '"sys_power":[0-9.]+' | cut -d: -f2 || true)
  [ -n "$SYS_W" ] && WATTS_JSON=",\"wattsW\":$SYS_W"
  break
done

curl -sf -m 10 -X POST "$PULSE/api/nodes" \
  -H "x-roost-node-key: $KEY" \
  -H "content-type: application/json" \
  -d "{\"name\":\"$NAME\",\"load1\":$LOAD1,\"cores\":$CORES,\"memTotalMb\":$MEM_TOTAL_MB,\"memUsedMb\":$MEM_USED_MB,\"diskTotalMb\":$DISK_TOTAL_MB,\"diskUsedMb\":$DISK_USED_MB,\"idleW\":$IDLE_W,\"maxW\":$MAX_W,\"model\":\"$MODEL\"$WATTS_JSON$NET_JSON}" \
  > /dev/null
