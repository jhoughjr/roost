#!/usr/bin/env bash
# dokku-reconcile.sh — put the box back in a working order, and say what it found.
#
# Written after a power cut on 2026-09-05. Twenty-one of twenty-three apps came
# back on their own; the restart policies were never the problem. What did go
# wrong is worse and quieter:
#
#   The `status` app lost its image, so it could not start. dokku writes
#   `invalid:80` as the upstream for an app with no container. nginx refuses to
#   reload while any vhost is invalid. So forgejo — untouched, healthy, and
#   nothing to do with `status` — served 502 for sixteen hours, and nothing said
#   a word.
#
# One dead app takes the whole proxy hostage, and every obvious recovery path is
# blocked with it: ps:start, proxy:disable and domains:disable all check for the
# image first and bail. What actually clears it is rebuilding every vhost.
#
# So this does three things, in order, and reports:
#   1. starts any app dokku believes is deployed but is not running
#   2. names any app whose image is missing, because that one poisons the proxy
#      and a person has to redeploy it
#   3. rebuilds every vhost, which is what unwedges nginx
#
# It needs no root: dokku is drivable over ssh as this account.
set -uo pipefail

DOKKU="${DOKKU_TARGET:-dokku@localhost}"
QUIET="${QUIET:-0}"

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }
dok() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$DOKKU" "$@" 2>&1; }

apps=$(dok apps:list | tail -n +2 | tr -d '\r')
[ -n "$apps" ] || { echo "dokku-reconcile: no apps listed; is dokku reachable at $DOKKU?" >&2; exit 1; }

started=0 imageless=0 checked=0
imageless_names=""

for app in $apps; do
  checked=$((checked + 1))
  report=$(dok ps:report "$app")
  running=$(printf '%s' "$report" | awk -F: '/Running:/ {gsub(/ /,"",$2); print $2}')
  deployed=$(printf '%s' "$report" | awk -F: '/Deployed:/ {gsub(/ /,"",$2); print $2}')

  # Only an app dokku thinks should be up. An app nobody has deployed is not a
  # fault, and starting one would be inventing intent.
  [ "$deployed" = "true" ] || continue
  [ "$running" = "false" ] || continue

  out=$(dok ps:start "$app")
  if printf '%s' "$out" | grep -qi "image.*not found\|Missing image"; then
    # The state that poisons the proxy. It cannot be fixed from here: the image
    # has to be rebuilt from wherever the app is deployed from.
    imageless=$((imageless + 1))
    imageless_names="$imageless_names $app"
    say "  $app: deployed, not running, and its image is gone — needs a redeploy"
  else
    started=$((started + 1))
    say "  $app: was down, started"
  fi
done

# The step that unwedges nginx. Run always, because a vhost can be stale without
# any app being down — which is exactly how forgejo stayed 502.
build=$(dok proxy:build-config --all)
if printf '%s' "$build" | grep -qi "Reloading nginx"; then
  say "  proxy: rebuilt, nginx reloaded"
else
  say "  proxy: rebuild did not report a reload — check nginx by hand"
fi

say "dokku-reconcile: $checked apps, $started started, $imageless with no image"
[ "$imageless" -eq 0 ] || say "  redeploy needed:$imageless_names"

# Exit non-zero only for the thing a person must act on, so a timer stays quiet
# when it has nothing to say.
[ "$imageless" -eq 0 ]
