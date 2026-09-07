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

# Containers and images are not the whole health of an app.
# On 2026-09-07 every app had a container and an image, and every site answered 502, because nginx held no TLS server block for their names.
# dokku clears each app's vhost at boot and rebuilds it per app, so one app that fails to restore aborts the run and leaves the rest cleared.
# cloudflared then reaches nginx over TLS at 127.0.0.1:443, gets "unrecognized name", and answers 502 to the world.
# This pass therefore asks the question the tunnel asks: does nginx serve this name at all.
#
# A fault here is the absence of an HTTP answer, never a bad one.
# An app that answers 500 has a working vhost and a problem of its own, and restarting the proxy for it would fix nothing.
unserved=0
unserved_names=""
# One container read for every app, because /home/dokku belongs to dokku and this account is not in that group.
vhosts=$(docker run --rm -v /home/dokku:/d:ro alpine sh -c 'for a in /d/*/VHOST; do [ -f "$a" ] || continue; printf "%s %s\n" "$(basename "$(dirname "$a")")" "$(tr "\n" " " < "$a")"; done' 2>/dev/null || true)

# An app is served when nginx answers for one of its names, on either port.
# Only two apps hold a certificate and the rest reach the tunnel over port 80, so asking 443 alone condemns every app that was never meant to answer there.
# That error also rebuilds the whole proxy on every pass, which is the expense this script exists to avoid.
probe_name() {
  # -k because the certificate does not matter here, only that nginx answers for the name.
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 --resolve "$1:443:127.0.0.1" "https://$1/" 2>/dev/null </dev/null)
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 -H "Host: $1" "http://127.0.0.1/" 2>/dev/null </dev/null)
  fi
  printf '%s' "$code"
}

# Every name the app owns, because the public name is often not the first line of VHOST.
while read -r app hosts; do
  [ -n "${hosts:-}" ] || continue
  served=0
  for host in $hosts; do
    # An answer of any kind means nginx holds a vhost for the name, which is the only question here.
    code=$(probe_name "$host")
    if [ -n "$code" ] && [ "$code" != "000" ]; then served=1; break; fi
  done
  if [ "$served" -eq 0 ]; then
    unserved=$((unserved + 1))
    unserved_names="$unserved_names $app"
    say "  $app: nginx answers for none of its names, so the tunnel cannot reach it"
  fi
done <<< "$vhosts"

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
now="started=$started imageless=$imageless_names unserved=$unserved_names"
was=$(cat "$state" 2>/dev/null || true)
printf '%s\n' "$now" > "$state" 2>/dev/null || true

if [ "$started" -gt 0 ] || [ "$unserved" -gt 0 ] || [ "$now" != "$was" ]; then
  build=$(dok proxy:build-config --all)
  if printf '%s' "$build" | grep -qi "Reloading nginx"; then
    say "  proxy: rebuilt, nginx reloaded"
  else
    say "  proxy: rebuild did not report a reload - check nginx by hand"
  fi
  # A rebuild that reported a reload and still does not serve is the failure that looks like success.
  # Ask again rather than trust the reload, because the reload is what was trusted last time.
  if [ "$unserved" -gt 0 ]; then
    still=""
    while read -r app hosts; do
      [ -n "${hosts:-}" ] || continue
      case " $unserved_names " in *" $app "*) ;; *) continue ;; esac
      served=0
      for host in $hosts; do
        code=$(probe_name "$host")
        if [ -n "$code" ] && [ "$code" != "000" ]; then served=1; break; fi
      done
      if [ "$served" -eq 0 ]; then
        still="$still $app"
      fi
    done <<< "$vhosts"
    if [ -n "$still" ]; then
      say "  proxy: still not serving after the rebuild:$still"
    else
      say "  proxy: serving again for$unserved_names"
    fi
  fi
else
  say "  proxy: nothing changed since the last pass, left alone"
fi

say "dokku-reconcile: $checked apps, $started started, $imageless with no image, $unserved not served"
[ "$imageless" -eq 0 ] || say "  redeploy needed:$imageless_names"

# Exit non-zero only for the thing a person must act on, so a timer stays quiet
# when it has nothing to say.
[ "$imageless" -eq 0 ]
