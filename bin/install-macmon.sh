#!/usr/bin/env bash
# install-macmon.sh — put macmon on a Mac, so node-report can read its die.
#
#   bin/install-macmon.sh                    this machine
#   bin/install-macmon.sh user@host          another Mac, over ssh
#
# macmon is what gives a Mac its measured watts and its CPU and GPU temperatures.
# Without it node-report falls back to an idle/max estimate and sends no temperatures
# at all, and nothing on the board says the figure is a guess except a tilde.
#
# Three ways in, tried in order, because no single one works on every machine here:
#
#   1. It is already installed and runs. Nothing to do, and this is the common case.
#   2. Homebrew is present, so `brew install macmon` is the maintained path.
#   3. Neither, so the binary is copied from a machine that has it. macmon links only
#      against system frameworks and libSystem, no Homebrew dylibs, so a copy runs on
#      any Apple silicon Mac. The mini is exactly this case: it has neither Homebrew
#      nor Rust, and macmon publishes no prebuilt binary, only a source tarball.
#
# The copy is re-signed. macOS ties an ad-hoc signature to where a binary was built, so
# a plain scp lands one the kernel kills on sight: no output, no message, exit 137, and
# a person concluding the download was corrupt. This cost an hour on hatchery today.
#
# Every path ends by running the thing and reading a temperature back. An install that
# reports success without producing a reading is the failure this whole estate keeps
# having, and it is cheap to refuse to do it.
set -euo pipefail

TARGET="${1:-}"
# A user path by default, so this needs no root. /usr/local/bin does, and asking for a
# password over a batch ssh connection fails with no terminal to ask on. macmon reads the
# SMC through IOReport without privileges, so nothing about the tool wanted root; only the
# destination did. node-report looks here.
DEST="${MACMON_DEST:-$HOME/bin/macmon}"
# Where to copy from when the target has no package manager. Any Mac that already has it.
DONOR="${MACMON_DONOR:-}"

on_target() {
  if [ -z "$TARGET" ]; then bash -c "$1"; else
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$1"
  fi
}

say() { printf '%s\n' "$*" >&2; }
where() { [ -z "$TARGET" ] && printf 'this machine' || printf '%s' "$TARGET"; }

# Reads a temperature rather than asking for a version. A binary that answers --version
# and cannot read the SMC has not given this estate anything it wanted.
proves_itself() {
  on_target 'for M in "$(command -v macmon || true)" "$HOME/bin/macmon" \
      /opt/homebrew/bin/macmon /usr/local/bin/macmon; do
      [ -n "$M" ] && [ -x "$M" ] || continue
      "$M" pipe -s 1 2>/dev/null | grep -q "cpu_temp_avg" && exit 0
    done; exit 1' 2>/dev/null
}

# Apple silicon is a requirement of this capability, not a preference. macmon reads the
# SMC through IOReport on an M-series die; on an Intel Mac it installs cleanly and then
# reads nothing, which would leave a binary in place that answers every check except the
# one that matters. Refusing here is the honest answer, and it names what is missing.
# TODO: Jimmy, 2026-09-08 - an Intel Mac needs its own path here. macmon cannot serve
# one, so the capability is a different instrument rather than the same one missing:
# smcFanControl's smc, iStats, or powermetrics under sudo read an Intel die, and none of
# them speak macmon's shape. Whatever is chosen has to fill the same `temps` entries so
# the board does not learn two vocabularies for one fact.
ARCH="$(on_target 'uname -m' 2>/dev/null || echo unknown)"
if [ "$ARCH" != "arm64" ]; then
  say "macmon needs Apple silicon and $(where) reports '$ARCH'."
  say "Nothing was installed. That Mac reports no watts and no temperatures, and pulse"
  say "falls back to the idle/max estimate, which the board already marks with a tilde."
  exit 1
fi

if proves_itself; then
  say "macmon already reads temperatures on $(where)"
  exit 0
fi

say "macmon is not reading temperatures on $(where); installing"

# 2. Homebrew, where it exists.
if on_target 'command -v brew >/dev/null 2>&1'; then
  say "  installing through Homebrew"
  on_target 'brew install macmon'
  if proves_itself; then say "macmon installed and reading on $(where)"; exit 0; fi
  say "  Homebrew finished and macmon still does not read; falling through to a copy"
fi

# 3. Copy from a donor and re-sign.
if [ -z "$DONOR" ]; then
  # This machine, if it has one worth copying.
  if command -v macmon >/dev/null 2>&1; then DONOR="local"; fi
fi
[ -n "$DONOR" ] || {
  say "macmon is absent, there is no Homebrew on $(where), and no donor was named."
  say "Name one with MACMON_DONOR=user@host, or MACMON_DONOR=local to copy from here."
  exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
if [ "$DONOR" = "local" ]; then
  cp "$(command -v macmon)" "$STAGE/macmon"
else
  scp -q -o BatchMode=yes "$DONOR:\$(command -v macmon)" "$STAGE/macmon"
fi
[ -s "$STAGE/macmon" ] || { say "the donor gave nothing"; exit 1; }
say "  copied $(wc -c < "$STAGE/macmon" | tr -d ' ') bytes from $DONOR"

# Root only when the destination needs it. A path under the account's own home does not.
NEEDS_ROOT=""
case "$DEST" in /usr/*|/opt/*) NEEDS_ROOT="sudo " ;; esac

if [ -z "$TARGET" ]; then
  mkdir -p "$(dirname "$DEST")"
  ${NEEDS_ROOT}install -m 0755 "$STAGE/macmon" "$DEST"
  ${NEEDS_ROOT}codesign --force --sign - "$DEST"
else
  scp -q -o BatchMode=yes "$STAGE/macmon" "$TARGET:/tmp/macmon.incoming"
  # Installed and signed on the far side in one hop: the signature has to be applied
  # where the file will live, not where it was copied from.
  on_target "mkdir -p \"\$(dirname '$DEST')\" \
             && ${NEEDS_ROOT}install -m 0755 /tmp/macmon.incoming '$DEST' \
             && ${NEEDS_ROOT}codesign --force --sign - '$DEST' \
             && rm -f /tmp/macmon.incoming"
fi

proves_itself || {
  say "macmon was placed at $DEST on $(where) and still does not read a temperature."
  say "Check its exit code: 137 means macOS killed it, which means the signature did not take."
  exit 1
}
say "macmon installed at $DEST and reading on $(where)"
