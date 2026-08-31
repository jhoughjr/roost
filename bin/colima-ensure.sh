#!/bin/bash
# colima-ensure.sh - keep the colima virtual machine running on this Mac.
#
# This machine's amd64 capability lives inside that VM. The VM carries the
# Rosetta binfmt handler, which is what makes `docker run --platform
# linux/amd64` work on Apple silicon. No other class of box in the estate can
# do it: the arm64 Linux boxes have QEMU user-mode emulation, and a Swift
# server binary segfaults under it.
#
# Nothing restarted the VM after a reboot before this script existed, so the
# machine lost that capability silently. A CI job which needs amd64 then queues
# forever or fails with a docker error about the daemon, and neither says the
# real cause, which is that the runtime went away.
#
# One-shot and idempotent. launchd runs it every few minutes through
# install-colima-ensure.sh, so the VM comes back after a reboot and after a
# crash.
#
# It never stops, deletes or reconfigures the VM. A running VM and its
# containers are safe from it, because it starts a VM only when one is not
# running. `colima start` with no arguments reads ~/.colima/default/colima.yaml,
# so the VM keeps the cpu, memory, vmType and rosetta settings it already has.
#
# That file is load-bearing, and this script is the reason. Because it passes no
# flags, the capability comes from the config rather than from here. A machine
# whose colima.yaml loses `vmType: vz` or `rosetta: true` starts a VM which
# looks healthy and cannot execute amd64. The check after the start below does
# catch it, and it catches it after the fact, so treat those two keys as
# settings which matter rather than as defaults. To read them back:
#
#     grep -E '^(vmType|rosetta):' ~/.colima/default/colima.yaml
#
# Config, via ~/.roostrc KEY=VALUE lines:
#   ROOST_COLIMA_PROFILE   colima profile name (default: default)
set -euo pipefail

# launchd gives an agent a minimal PATH, and colima is installed per-user.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

# The `|| true` matters. Under `set -e` a bare `[ -f x ] && . x` exits the
# script when the file is absent, which is the common case on a fresh machine.
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc" || true
PROFILE="${ROOST_COLIMA_PROFILE:-default}"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

if ! command -v colima > /dev/null 2>&1; then
  echo "$(stamp) colima-ensure: colima is not on PATH, so this Mac cannot run amd64"
  exit 1
fi

# Silence is the healthy path. This runs every few minutes, and a line per run
# would bury the one line that matters in the log.
if colima status --profile "$PROFILE" > /dev/null 2>&1; then
  exit 0
fi

echo "$(stamp) colima-ensure: the '$PROFILE' VM is not running, starting it"

# No sizing flags on purpose. `colima start` reads this machine's own
# ~/.colima/$PROFILE/colima.yaml, so the same script suits a 4 CPU mini and a
# 2 CPU laptop. Passing --memory or --cpu here would carry one machine's
# numbers onto every other machine.
if ! colima start --profile "$PROFILE"; then
  echo "$(stamp) colima-ensure: the VM did not start"
  exit 1
fi

# A VM which starts without working translation looks healthy and cannot run
# amd64. The label a workflow selects on is a capability claim, so the machine
# proves it rather than asserting it. This runs a real amd64 container, which
# is a stronger test than reading the binfmt entry, because a registered
# handler and a working one are not the same thing.
arch="$(docker run --rm --platform linux/amd64 alpine uname -m 2>/dev/null | tr -d '\r\n' || true)"
if [ "$arch" = "x86_64" ]; then
  echo "$(stamp) colima-ensure: the VM is up and amd64 translation works"
else
  echo "$(stamp) colima-ensure: WARNING the VM is up and amd64 answered '$arch', not x86_64"
  echo "$(stamp) colima-ensure: set 'rosetta: true' and 'vmType: vz' in ~/.colima/$PROFILE/colima.yaml"
fi
