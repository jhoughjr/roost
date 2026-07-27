# shellcheck shell=bash
# roost-env.sh — sourced, never executed: ONE home for the ~/.roostrc load
# and the personal fallbacks on the bash side (bin/roostlib.py DEFAULTS is
# the Python mirror). Every bash entrypoint sources this instead of carrying
# its own copies, so the portability sweep (roost#10) has exactly two files
# to visit. Values already in the environment or ~/.roostrc win; the := only
# fills gaps.
# shellcheck source=/dev/null
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc"
: "${ROOST_DOKKU_HOST:=dokku@192.168.0.103}"
: "${ROOST_DOMAIN:=jimmyhoughjr.net}"
: "${ROOST_STATUS_RUNNER:=jimmyhoughjr@jimmys-mac-mini.local}"
: "${ROOST_STATUS_AGENT:=net.jimmyhoughjr.roost-status}"
# The short names every caller already uses. Unused *here* by construction —
# this file exists to define them for whoever sourced it.
# shellcheck disable=SC2034
DOKKU="$ROOST_DOKKU_HOST"
# shellcheck disable=SC2034
DOMAIN="$ROOST_DOMAIN"
