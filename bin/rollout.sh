#!/usr/bin/env bash
# rollout.sh — converge every writer machine on just-merged roost + statusgen.
# Invoked as `roost rollout [--kick]`.
#
# status.sh self-updates its own clones at the START of each run, so the fleet
# converges eventually — but between a merge and each machine's next run the
# writers sit on different versions and regenerate boards differently (they
# fight). This replaces the post-merge hand ritual (roost#23):
#   1. git pull roost + statusgen on the MacBook
#   2. same on the mini (the authoritative board writer)
#   3. optionally refresh from the mini
#
#   roost rollout           pull roost + statusgen here AND on every machine in
#                           ROOST_WRITERS (space-separated ssh targets; default:
#                           the status runner — same target as `roost kick`)
#   roost rollout --kick    ...then fire the runner's deploy agent right away
#
# Clone paths resolve per machine from ITS OWN ~/.roostrc (ROOST_CLONE /
# ROOST_STATUSGEN, defaulting to ~/repos/roost and ~/repos/statusgen). Pulls
# are ff-only: a dirty or diverged clone is never touched — it's reported ✗,
# left for a hand fix, and the run exits non-zero.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)"
ROOST_DIR="$(cd "$BIN/.." && pwd)"
# shellcheck source=/dev/null
. "$BIN/roost-env.sh"

KICK=""
for arg in "$@"; do
  case "$arg" in
    --kick) KICK=1 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: roost rollout [--kick]" >&2; exit 1 ;;
  esac
done

# ${VAR-default} (not :-): an explicitly EMPTY ROOST_WRITERS means "no remote
# machines", e.g. a single-machine setup or the tests.
WRITERS="${ROOST_WRITERS-$ROOST_STATUS_RUNNER}"

# One pull script, run verbatim both locally and over ssh, so each machine
# resolves clone paths from its own rc. Single-quoted: expands on the target.
PULL='
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc" >/dev/null 2>&1 || true
rc=0
for d in "${ROOST_CLONE:-$HOME/repos/roost}" "${ROOST_STATUSGEN:-$HOME/repos/statusgen}"; do
  if [ ! -d "$d/.git" ]; then echo "  - $d: no clone here"; continue; fi
  before="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo "?")"
  if git -C "$d" pull --ff-only >/dev/null 2>&1; then
    after="$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo "?")"
    if [ "$before" = "$after" ]; then echo "  ✓ $d: fresh ($after)"
    else echo "  ✓ $d: $before → $after"; fi
  else
    echo "  ✗ $d: pull failed (dirty or diverged) — fix by hand"; rc=1
  fi
done
exit $rc
'

fails=0
echo "==> local ($(hostname -s))"
# Locally the roost clone is the repo THIS script runs from, wherever it lives.
if ! ROOST_CLONE="${ROOST_CLONE:-$ROOST_DIR}" bash -c "$PULL"; then
  fails=$((fails + 1))
fi

for m in $WRITERS; do
  echo "==> $m"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$m" bash -s <<<"$PULL"; then
    echo "  ✗ $m: unreachable or pull failed"
    fails=$((fails + 1))
  fi
done

if [ "$fails" -gt 0 ]; then
  echo "✗ rollout incomplete — $fails machine(s) need a hand pull" >&2
  exit 1
fi
echo "✓ rollout complete — every writer is on the merged version"
if [ -n "$KICK" ]; then
  "$BIN/roost" kick
fi
