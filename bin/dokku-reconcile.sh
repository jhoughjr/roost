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
BOX="${BOX_TARGET:-localhost}"
QUIET="${QUIET:-0}"

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }
dok() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$DOKKU" "$@" 2>&1; }

# Ask docker what is running, not dokku. `ps:report` costs a round trip per app
# — six and a half minutes across twenty-three — and asking for every app at
# once is slower still, because it inspects each one. Docker answers the same
# question about all of them in a quarter of a second.
#
# An app counts as deployed-and-down when its container exists but is not
# running: something started it once and it is not up now. An app that has never
# run has no container, and starting one would be inventing intent.
# Docker runs on this box, so this asks it directly. An earlier cut shelled out
# to localhost over ssh and matched nothing, which reported a healthy estate of
# zero apps — a check that finds nothing wrong because it looked nowhere.
running=$(docker ps --format '{{.Names}}' | grep -E '^[a-zA-Z0-9_.-]+\.web\.[0-9]+$' || true)
known=$(docker ps -a --format '{{.Names}}' | grep -E '^[a-zA-Z0-9_.-]+\.web\.[0-9]+$' || true)

# An app dokku knows about that has no container at all is the case this exists
# for: `status` lost its image, so nothing was ever created, so a container-only
# inventory sees a healthy estate while its stale upstream holds nginx hostage.
# One cheap call, and it catches the failure the expensive version was written
# for and the fast version had just lost.
declared=$(dok apps:list | tail -n +2 | tr -d '\r')
# Every container dokku made, whatever the process type. Matching only `.web.`
# treated an app whose processes are named anything else as having none, and
# then "started" it — restarting healthy apps to check whether they were
# healthy. An app with a worker and no web is not a fault.
anycontainer=$(docker ps -a --format '{{.Names}}' | grep -E '^[a-zA-Z0-9_.-]+\.[a-z]+\.[0-9]+$' || true)

started=0 imageless=0 checked=0
imageless_names=""

for app in $declared; do
  printf '%s\n' "$anycontainer" | grep -qE "^${app}\.[a-z]+\.[0-9]+$" && continue
  # No container of any kind was ever made for this app. Only then is asking
  # dokku to start it safe: there is nothing running to disturb.
  out=$(dok ps:start "$app" 2>&1)
  if printf '%s' "$out" | grep -qi "image.*not found\|Missing image"; then
    imageless=$((imageless + 1))
    imageless_names="$imageless_names $app"
    say "  $app: dokku knows it, it has no image, and its upstream poisons nginx"
  fi
done

for container in $known; do
  app="${container%%.web.*}"
  checked=$((checked + 1))
  printf '%s\n' "$running" | grep -qx "$container" && continue

  out=$(dok ps:start "$app")
  if printf '%s' "$out" | grep -qi "image.*not found\|Missing image"; then
    # The state that poisons the proxy. It cannot be fixed from here: the image
    # has to be rebuilt wherever the app is deployed from.
    imageless=$((imageless + 1))
    imageless_names="$imageless_names $app"
    say "  $app: deployed, not running, and its image is gone — needs a redeploy"
  else
    started=$((started + 1))
    say "  $app: was down, started"
  fi
done

# Rebuilding every vhost is what unwedges nginx, and it is also the expensive
# part — five minutes across twenty-three apps. Doing it every ten minutes to
# find nothing wrong is a watchdog that spends its life rebuilding a working
# proxy, so it runs only when this pass touched something: an app started, or an
# app that cannot start and whose stale upstream is the thing that poisons nginx.
# Rebuild when the picture changed, not while it stays broken. An app whose
# image is gone stays gone until a person redeploys it, and rebuilding every
# vhost every ten minutes on its account is five minutes of work to reach the
# same place. The vhost needs clearing once; after that the fault is a thing to
# report, not a thing to keep fixing.
state="${XDG_STATE_HOME:-$HOME/.local/state}/dokku-reconcile.last"
install -d "$(dirname "$state")" 2>/dev/null || true
now="started=$started imageless=$imageless_names"
was=$(cat "$state" 2>/dev/null || true)
printf '%s\n' "$now" > "$state" 2>/dev/null || true

if [ "$started" -gt 0 ] || [ "$now" != "$was" ]; then
  build=$(dok proxy:build-config --all)
  if printf '%s' "$build" | grep -qi "Reloading nginx"; then
    say "  proxy: rebuilt, nginx reloaded"
  else
    say "  proxy: rebuild did not report a reload — check nginx by hand"
  fi
else
  say "  proxy: nothing changed since the last pass, left alone"
fi

say "dokku-reconcile: $checked apps, $started started, $imageless with no image"
[ "$imageless" -eq 0 ] || say "  redeploy needed:$imageless_names"

# Exit non-zero only for the thing a person must act on, so a timer stays quiet
# when it has nothing to say.
[ "$imageless" -eq 0 ]
