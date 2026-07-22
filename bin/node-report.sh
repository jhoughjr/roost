#!/bin/bash
# node-report.sh — report this machine's usage to pulse as a roost node.
#
# Runs on macOS and on Linux (the Pi/opi class of box). Everything below the
# metric-collection `case` is platform-agnostic: both branches fill the same
# variables, so the runner badge and the POST body are written once.
#
# One-shot: run it and pulse's /api/stats gains a `nodes` entry for this
# machine (shown on watts.jimmyhoughjr.net/roost/). Run it every 30 s for a
# live feed — launchd on macOS, a systemd user timer on Linux; both are set up
# by install-node-report.sh.
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

IDLE_W=${ROOST_NODE_IDLE_W:-5}
MAX_W=${ROOST_NODE_MAX_W:-40}
PULSE=${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}

# Everything from here to the runner block is per-platform. Both branches set
# the same variables: NAME LOAD1 CORES MODEL MEM_*_MB DISK_*_MB and the
# NET_JSON / WATTS_JSON / POWER_JSON fragments (empty string = field omitted).
case "$(uname -s)" in
Darwin)

NAME=${ROOST_NODE_NAME:-$(scutil --get ComputerName | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-*$//;s/^-*//' | cut -c1-32)}

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

# Power source: on wall power ("ac") or on battery, plus charge % on laptops.
# A desktop Mac (mini/Studio) always reads "AC Power" with no battery line, so
# batteryPct is only sent when a battery actually exists.
POWER_JSON=""
BATT=$(pmset -g batt 2>/dev/null || true)
case "$(printf '%s\n' "$BATT" | awk -F"'" '/Now drawing/{print $2; exit}')" in
  "AC Power")      POWER_JSON=",\"power\":\"ac\"" ;;
  "Battery Power") POWER_JSON=",\"power\":\"battery\"" ;;
esac
PCT=$(printf '%s\n' "$BATT" | grep -oE '[0-9]+%' | head -1 | tr -d '%' || true)
# `if`, not `[ ... ] && ...`: on a desktop PCT is empty, and a bare failing
# &&-list would trip `set -e` and abort before the POST.
if [ -n "$PCT" ]; then POWER_JSON="$POWER_JSON,\"batteryPct\":$PCT"; fi

;;
Linux)

NAME=${ROOST_NODE_NAME:-$(hostname -s 2>/dev/null || hostname | cut -d. -f1)}
NAME=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-*$//;s/^-*//' | cut -c1-32)

LOAD1=$(awk '{print $1}' /proc/loadavg)
CORES=$(nproc)
# Model: SBCs (Orange/Raspberry Pi) publish a device-tree model — NUL-padded,
# hence the tr; x86 boxes publish DMI; anything else falls back to the arch.
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
[ -n "$MODEL" ] || MODEL=$(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || true)
[ -n "$MODEL" ] || MODEL=$(uname -m)
MODEL=$(printf '%s' "$MODEL" | tr -d '"\\' | cut -c1-48)

# MemAvailable (not MemFree) is the kernel's own estimate of what a new
# workload could claim — the honest analogue of the Mac free+inactive sum.
read -r MEM_TOTAL_KB MEM_AVAIL_KB <<< "$(awk '
  /^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {print t, a}' /proc/meminfo)"
MEM_TOTAL_MB=$(( MEM_TOTAL_KB / 1024 ))
MEM_USED_MB=$(( (MEM_TOTAL_KB - MEM_AVAIL_KB) / 1024 ))

# Disk: the root filesystem; used = size − available, matching the Mac branch
# (so a reserved-blocks gap counts as used on both, not as phantom free space).
DISK=$(df -k / | awk 'NR==2{print $2, $4}')
DISK_TOTAL_MB=$(( ${DISK%% *} / 1024 ))
DISK_USED_MB=$(( ( ${DISK%% *} - ${DISK##* } ) / 1024 ))

# Networking: the default route names both the interface and this box's source
# IP, so one `ip route` gives what `route -n get` + `ipconfig getifaddr` do on
# the Mac. Link bytes come from sysfs rather than /proc/net/dev — same counters,
# but no column-parsing of a table whose name field runs into the colon.
NET_JSON=""
read -r IFACE IP <<< "$(ip route show default 2>/dev/null | awk '
  {for (i=1;i<NF;i++) {if ($i=="dev") d=$(i+1); if ($i=="src") s=$(i+1)}}
  END {print d, s}')"
if [ -n "${IFACE:-}" ] && [ -n "${IP:-}" ]; then
  STATS="/sys/class/net/$IFACE/statistics"
  RX_B=$(cat "$STATS/rx_bytes" 2>/dev/null || true)
  TX_B=$(cat "$STATS/tx_bytes" 2>/dev/null || true)
  NET_JSON=",\"ip\":\"$IP\",\"iface\":\"$IFACE\",\"netRxB\":${RX_B:-0},\"netTxB\":${TX_B:-0}"
fi

# No sudoless system-power sensor on these boards (the Pi PMIC doesn't expose
# one), so wattsW is never sent and pulse falls back to the idleW/maxW load
# estimate — the `~` on the roost page is accurate here, not a gap to fill.
WATTS_JSON=""

# Power source: a box with no battery power_supply is mains-fed, which is what
# a Mac desktop reports as "ac". Battery-backed Linux (laptop, UPS-as-battery)
# reports its charge the same way the laptop branch does.
POWER_JSON=""
BATT_DIR=""
for d in /sys/class/power_supply/*; do
  if [ -r "$d/type" ] && [ "$(cat "$d/type")" = "Battery" ]; then BATT_DIR="$d"; break; fi
done
if [ -z "$BATT_DIR" ]; then
  POWER_JSON=",\"power\":\"ac\""
else
  case "$(cat "$BATT_DIR/status" 2>/dev/null || true)" in
    Discharging) POWER_JSON=",\"power\":\"battery\"" ;;
    *)           POWER_JSON=",\"power\":\"ac\"" ;;
  esac
  PCT=$(cat "$BATT_DIR/capacity" 2>/dev/null || true)
  if [ -n "$PCT" ]; then POWER_JSON="$POWER_JSON,\"batteryPct\":$PCT"; fi
fi

;;
*)
  echo "node-report: unsupported platform $(uname -s)" >&2; exit 1 ;;
esac

# GitHub Actions runners hosted on this box. A registered runner idles as a
# `Runner.Listener` process; while it executes a job it also has a
# `Runner.Worker`. So listeners = runners online, workers = jobs building.
# We report the count whenever a runner is INSTALLED here (a `.runner` config
# in a known runner dir) — even when it's 0 — so a runner that should be up but
# crashed reports `runners:0` and shows as "down" on the map instead of
# silently vanishing (a dead Runner.Listener would otherwise send nothing, the
# blind spot that queued CI for two days). Boxes with no runner installed send
# nothing and stay unbadged. NB: `if`, never `[ … ] && …` — a false test under
# `set -e` would abort before the POST (see the batteryPct note above).
RUNNER_JSON=""
RUNNERS=$(pgrep -f 'Runner\.Listener' 2>/dev/null | wc -l | tr -d ' ') || RUNNERS=0
RUNNER_INSTALLED=0
for d in "$HOME"/actions-runner "$HOME"/github-runner "$HOME"/github-runner-*; do
  if [ -e "$d/.runner" ]; then RUNNER_INSTALLED=1; break; fi
done
if [ "${RUNNERS:-0}" -gt 0 ] || [ "$RUNNER_INSTALLED" = 1 ]; then
  BUSY=$(pgrep -f 'Runner\.Worker' 2>/dev/null | wc -l | tr -d ' ') || BUSY=0
  RUNNER_JSON=",\"runners\":$RUNNERS,\"runnersBusy\":${BUSY:-0}"
fi

curl -sf -m 10 -X POST "$PULSE/api/nodes" \
  -H "x-roost-node-key: $KEY" \
  -H "content-type: application/json" \
  -d "{\"name\":\"$NAME\",\"load1\":$LOAD1,\"cores\":$CORES,\"memTotalMb\":$MEM_TOTAL_MB,\"memUsedMb\":$MEM_USED_MB,\"diskTotalMb\":$DISK_TOTAL_MB,\"diskUsedMb\":$DISK_USED_MB,\"idleW\":$IDLE_W,\"maxW\":$MAX_W,\"model\":\"$MODEL\"$WATTS_JSON$NET_JSON$POWER_JSON$RUNNER_JSON}" \
  > /dev/null
