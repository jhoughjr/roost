#!/bin/bash
# node-report.sh — report this machine's usage to pulse as a roost node.
#
# One-shot: run it and pulse's /api/stats gains a `nodes` entry for this
# machine (shown on watts.jimmyhoughjr.net/roost/). Run it every 30 s for a
# live feed — see install-node-report.sh (launchd on macOS, systemd on Linux).
#
# Config (all optional), via ~/.roostrc KEY=VALUE lines:
#   ROOST_NODE_NAME    node name  (default: hostname, lowercased a-z0-9-)
#   ROOST_NODE_IDLE_W  idle watts (default: 5  — Apple-silicon Mac mini/laptop)
#   ROOST_NODE_MAX_W   full-tilt watts (default: 40 — M-series mini under load)
#   ROOST_PULSE_URL    pulse base URL (default: https://pulse.jimmyhoughjr.net)
# Shared key: ~/.roost_node_key (chmod 600), must match `dokku config pulse NODE_KEY`.
set -euo pipefail

OS=$(uname -s)   # Darwin or Linux

RC="$HOME/.roostrc"
[ -f "$RC" ] && . "$RC"

KEY_FILE="$HOME/.roost_node_key"
[ -f "$KEY_FILE" ] || { echo "node-report: missing $KEY_FILE (the pulse NODE_KEY)" >&2; exit 1; }
KEY=$(cat "$KEY_FILE")

if [ "$OS" = Darwin ]; then
  RAW_NAME=$(scutil --get ComputerName)
else
  RAW_NAME=$(hostname -s 2>/dev/null || hostname)
fi
NAME=${ROOST_NODE_NAME:-$(printf '%s' "$RAW_NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-*$//;s/^-*//' | cut -c1-32)}
IDLE_W=${ROOST_NODE_IDLE_W:-5}
MAX_W=${ROOST_NODE_MAX_W:-40}
PULSE=${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}

if [ "$OS" = Darwin ]; then
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
else
  LOAD1=$(awk '{print $1}' /proc/loadavg)
  CORES=$(nproc)
  # Model: DMI product name (PCs/VMs), device-tree (ARM SBCs), else the arch.
  MODEL=$(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null \
    || tr -d '\0' < /proc/device-tree/model 2>/dev/null \
    || uname -m)
  MODEL=$(printf '%s' "$MODEL" | tr -d '"\\' | cut -c1-48)
  MEM_TOTAL_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
  # Used = total − MemAvailable (the kernel's own reclaimable-aware estimate).
  MEM_USED_MB=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print int((t-a)/1024)}' /proc/meminfo)

  DISK=$(df -k / | awk 'NR==2{print $2, $4}')
fi
DISK_TOTAL_MB=$(( ${DISK%% *} / 1024 ))
DISK_USED_MB=$(( ( ${DISK%% *} - ${DISK##* } ) / 1024 ))

# Networking: default-route interface → LAN IP + cumulative link bytes.
# pulse turns consecutive byte counters into live ↓/↑ rates for the map.
NET_JSON="" IP="" IFACE=""
if [ "$OS" = Darwin ]; then
  IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
  if [ -n "$IFACE" ]; then
    IP=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)
    read -r RX_B TX_B <<< "$(netstat -ibn -I "$IFACE" | awk 'NR==2{print $7, $10}')"
  fi
else
  IFACE=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev") {print $(i+1); exit}}')
  if [ -n "$IFACE" ]; then
    IP=$(ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}')
    # /proc/net/dev row for the iface: after stripping the name, rx bytes is
    # field 1 and tx bytes field 9 (the name can be glued to the first counter).
    read -r RX_B TX_B <<< "$(sed -n "s/^ *$IFACE: */ /p" /proc/net/dev | awk '{print $1, $9}')"
  fi
fi
[ -n "$IFACE" ] && [ -n "$IP" ] && NET_JSON=",\"ip\":\"$IP\",\"iface\":\"$IFACE\",\"netRxB\":${RX_B:-0},\"netTxB\":${TX_B:-0}"

# Measured watts. macOS: macmon (sudoless SMC read, Apple silicon) — whole-
# system watts. Linux: Intel RAPL package counters when readable (root-only
# 0400 on many kernels) — CPU package only, an undercount but a real reading.
# When neither is available the field is omitted and pulse/watts fall back to
# the idleW/maxW load estimate (shown with a ~ on the roost page).
WATTS_JSON=""
if [ "$OS" = Darwin ]; then
  for MACMON in "$(command -v macmon || true)" /opt/homebrew/bin/macmon /usr/local/bin/macmon; do
    [ -n "$MACMON" ] && [ -x "$MACMON" ] || continue
    SYS_W=$("$MACMON" pipe -s 1 2>/dev/null | grep -oE '"sys_power":[0-9.]+' | cut -d: -f2 || true)
    [ -n "$SYS_W" ] && WATTS_JSON=",\"wattsW\":$SYS_W"
    break
  done
else
  # Top-level package domains only (intel-rapl:N, not the :N:M subzones).
  RAPL_DIRS=$(ls -d /sys/class/powercap/intel-rapl:[0-9]* 2>/dev/null | grep -E 'intel-rapl:[0-9]+$' || true)
  FIRST=$(printf '%s\n' "$RAPL_DIRS" | head -1)
  if [ -n "$RAPL_DIRS" ] && [ -r "$FIRST/energy_uj" ]; then
    E0=0; for d in $RAPL_DIRS; do E0=$(( E0 + $(cat "$d/energy_uj") )); done
    sleep 1
    E1=0; for d in $RAPL_DIRS; do E1=$(( E1 + $(cat "$d/energy_uj") )); done
    # Skip on counter wrap (delta would be negative).
    if [ "$E1" -gt "$E0" ]; then
      WATTS_JSON=",\"wattsW\":$(awk -v d=$((E1 - E0)) 'BEGIN{printf "%.1f", d/1000000}')"
    fi
  fi
fi

curl -sf -m 10 -X POST "$PULSE/api/nodes" \
  -H "x-roost-node-key: $KEY" \
  -H "content-type: application/json" \
  -d "{\"name\":\"$NAME\",\"load1\":$LOAD1,\"cores\":$CORES,\"memTotalMb\":$MEM_TOTAL_MB,\"memUsedMb\":$MEM_USED_MB,\"diskTotalMb\":$DISK_TOTAL_MB,\"diskUsedMb\":$DISK_USED_MB,\"idleW\":$IDLE_W,\"maxW\":$MAX_W,\"model\":\"$MODEL\"$WATTS_JSON$NET_JSON}" \
  > /dev/null
