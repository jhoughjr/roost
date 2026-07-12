#!/bin/bash
# Fixture test for node-report.sh's Linux branch, runnable on macOS.
# Rewrites /proc and /sys to a fixture tree, shims Linux-only commands,
# captures the curl payload, and asserts every field.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/bin/node-report.sh"
FIX=$(mktemp -d)
trap 'rm -rf "$FIX"' EXIT

# --- fixture /proc and /sys ---------------------------------------------
mkdir -p "$FIX/proc/net" "$FIX/sys/devices/virtual/dmi/id" \
  "$FIX/sys/class/powercap/intel-rapl:0" "$FIX/sys/class/powercap/intel-rapl:0:0" \
  "$FIX/home"
echo "0.42 0.35 0.30 2/345 12345" > "$FIX/proc/loadavg"
printf 'MemTotal:       16321204 kB\nMemFree:         1000000 kB\nMemAvailable:    9876543 kB\nBuffers:          200000 kB\n' > "$FIX/proc/meminfo"
# glued name:counter variant on purpose
printf 'Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n  eth0:123456789  1000 0 0 0 0 0 0 987654321 2000 0 0 0 0 0 0\n' > "$FIX/proc/net/dev"
printf 'Test Linux Box' > "$FIX/sys/devices/virtual/dmi/id/product_name"
echo 1000000 > "$FIX/sys/class/powercap/intel-rapl:0/energy_uj"
echo 5000000 > "$FIX/sys/class/powercap/intel-rapl:0:0/energy_uj"   # subzone: must be EXCLUDED
echo test-key > "$FIX/home/.roost_node_key"

# --- command shims -------------------------------------------------------
mkdir -p "$FIX/bin"
cat > "$FIX/bin/uname" <<'EOF'
#!/bin/bash
[ "${1:-}" = "-m" ] && echo x86_64 || echo Linux
EOF
cat > "$FIX/bin/nproc" <<'EOF'
#!/bin/bash
echo 8
EOF
cat > "$FIX/bin/hostname" <<'EOF'
#!/bin/bash
echo Test-Linux-Box
EOF
cat > "$FIX/bin/ip" <<EOF
#!/bin/bash
case "\$*" in
  *"route show default"*) echo "default via 192.168.0.1 dev eth0 proto dhcp metric 100";;
  *"addr show dev eth0"*) echo "2: eth0    inet 192.168.0.55/24 brd 192.168.0.255 scope global dynamic eth0";;
esac
EOF
cat > "$FIX/bin/df" <<'EOF'
#!/bin/bash
printf 'Filesystem     1K-blocks     Used Available Use%% Mounted on\n/dev/root      100000000 40000000  60000000  40%% /\n'
EOF
# sleep shim: advance the RAPL counters between the two samples
cat > "$FIX/bin/sleep" <<EOF
#!/bin/bash
echo 2000000 > "$FIX/sys/class/powercap/intel-rapl:0/energy_uj"
echo 99999999 > "$FIX/sys/class/powercap/intel-rapl:0:0/energy_uj"
EOF
cat > "$FIX/bin/curl" <<EOF
#!/bin/bash
while [ \$# -gt 0 ]; do [ "\$1" = "-d" ] && { printf '%s' "\$2" > "$FIX/payload.json"; shift; }; shift; done
EOF
chmod +x "$FIX/bin/"*

# --- run the Linux branch ------------------------------------------------
sed -e "s#/proc/#$FIX/proc/#g" -e "s#/sys/#$FIX/sys/#g" "$SRC" > "$FIX/nr.sh"
chmod +x "$FIX/nr.sh"
HOME="$FIX/home" PATH="$FIX/bin:$PATH" bash "$FIX/nr.sh"

# --- assert --------------------------------------------------------------
python3 - "$FIX/payload.json" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
assert p["name"] == "test-linux-box", p["name"]
assert p["load1"] == 0.42
assert p["cores"] == 8
assert p["model"] == "Test Linux Box", p["model"]
assert p["memTotalMb"] == 16321204 // 1024, p["memTotalMb"]
assert p["memUsedMb"] == (16321204 - 9876543) // 1024, p["memUsedMb"]
assert p["diskTotalMb"] == 100000000 // 1024
assert p["diskUsedMb"] == (100000000 - 60000000) // 1024
assert p["iface"] == "eth0" and p["ip"] == "192.168.0.55"
assert p["netRxB"] == 123456789 and p["netTxB"] == 987654321, (p["netRxB"], p["netTxB"])
# RAPL: (2000000-1000000) µJ over the sample = 1.0 W; subzone excluded
assert p["wattsW"] == 1.0, p["wattsW"]
print("LINUX-FIXTURE-OK:", json.dumps(p))
EOF
