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
#
# It also reports what answers for the containers dokku does not own: a
# dnsmasq, a Postgres cluster, a CI runner. Those are named by hatchery's
# declaration, read from pulse rather than typed here, so a container declared
# in the manifest starts being asked about without an edit to this script. This
# pass never touches one. It says whether each is running, stopped or absent,
# and nothing more, because a container dokku did not make is not dokku's to
# start and starting one blind would be inventing intent.
set -uo pipefail

DOKKU="${DOKKU_TARGET:-dokku@localhost}"
QUIET="${QUIET:-0}"
PULSE="${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}"

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }
dok() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$DOKKU" "$@" 2>&1; }

# The declaration, cached. pulse is the copy and the manifest is the record, so
# a pulse that does not answer falls back to the last list this box saw, and a
# box that has never seen one reports dokku's containers alone.
DECLARED_CACHE="$HOME/.roost-reconcile-declared.json"
declared_file=""
if curl -sf -m 15 "$PULSE/api/declared" -o "$DECLARED_CACHE.new" 2>/dev/null; then
  mv -f "$DECLARED_CACHE.new" "$DECLARED_CACHE"
  declared_file="$DECLARED_CACHE"
elif [ -f "$DECLARED_CACHE" ]; then
  declared_file="$DECLARED_CACHE"
  say "  declared: pulse did not answer, so the previous run's list is used"
else
  say "  declared: pulse did not answer and none is cached, so only dokku's containers are reported"
fi
rm -f "$DECLARED_CACHE.new"

# One line of `name state` per container the declaration names for this box.
# One reader, so the summary line below and the reading posted to pulse cannot
# disagree about what answered.
#
# A name the box does not hold at all is absent, which is a different fault
# from one that exists and stopped: the first was never made, and the second
# ran and gave up.
declared_states=$(python3 -c '
import json, sys
declared_file, states_text, box_ips = sys.argv[1], sys.argv[2], sys.argv[3]
states = dict(
    (parts[0], parts[1])
    for parts in (line.split() for line in states_text.splitlines())
    if len(parts) == 2
)
here = set(box_ips.split())
declared = {}
if declared_file:
    try:
        with open(declared_file) as fh:
            declared = json.load(fh)
    except (OSError, ValueError):
        declared = {}
for stack in declared.get("stacks", []):
    if stack.get("backend") != "host":
        continue
    if (stack.get("host") or "").split("@")[-1] not in here:
        continue
    for service in stack.get("services", []):
        name = service.get("name")
        if name:
            print(name, states.get(name, "absent"))
' "$declared_file" \
  "$(docker ps -a --format '{{.Names}} {{.State}}' || true)" \
  "127.0.0.1 localhost $(hostname -I 2>/dev/null || true)")

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
# The apps still not served once the pass is over. The rebuild below narrows it when it runs.
still="$unserved_names"
rebuilt=0

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

# Parse the previous quarantined list from the state file.
quarantined=""
if [ -f "$state" ]; then
  quarantined=$(grep -o "quarantined=[^ ]*" "$state" 2>/dev/null | cut -d= -f2- || true)
fi

# On every pass, check if any quarantined app's container is now running.
for app in $quarantined; do
  if docker ps --format '{{.Names}}' | grep -qx "${app}.web.1"; then
    dok proxy:enable "$app" > /dev/null 2>&1
    dok proxy:build-config "$app" > /dev/null 2>&1
    say "  proxy: $app is back, its vhost restored"
    quarantined="${quarantined// $app / }"
    quarantined="${quarantined#$app }"
    quarantined="${quarantined% $app}"
  fi
done

now="started=$started imageless=$imageless_names unserved=$unserved_names quarantined=$quarantined"
was=$(cat "$state" 2>/dev/null || true)
printf '%s\n' "$now" > "$state" 2>/dev/null || true

if [ "$started" -gt 0 ] || [ "$unserved" -gt 0 ] || [ "$now" != "$was" ]; then
  # Capture rebuild output and check for poisoned apps.
  build=""
  rebuild_count=0
  max_rebuilds=3
  quarantine_queue=""

  while [ $rebuild_count -lt $max_rebuilds ]; do
    rebuild_count=$((rebuild_count + 1))
    build=$(dok proxy:build-config --all)

    if printf '%s' "$build" | grep -qi "Reloading nginx"; then
      say "  proxy: rebuilt, nginx reloaded"
      rebuilt=1
    else
      say "  proxy: rebuild did not report a reload - check nginx by hand"
    fi

    # Extract poisoned app from error message.
    poisoned_app=$(printf '%s' "$build" | grep -o 'host not found in upstream "invalid:[^"]*" in /home/dokku/[^/]*/nginx.conf' | head -1 | sed 's|.*in /home/dokku/||; s|/.*||')

    if [ -n "$poisoned_app" ]; then
      say "  proxy: quarantined $poisoned_app, its container has no address"
      quarantine_queue="$quarantine_queue $poisoned_app"
      # Disable the app via the same ssh channel.
      dok proxy:disable "$poisoned_app" > /dev/null 2>&1
    else
      # No more poisoned apps found.
      break
    fi
  done

  # Add newly quarantined apps to the list.
  for app in $quarantine_queue; do
    if ! printf ' %s ' "$quarantined" | grep -q " $app "; then
      quarantined="$quarantined $app"
    fi
  done

  # Clean up whitespace.
  quarantined=$(printf '%s' "$quarantined" | xargs)

  # Update state file with the potentially changed quarantined list.
  now="started=$started imageless=$imageless_names unserved=$unserved_names quarantined=$quarantined"
  printf '%s\n' "$now" > "$state" 2>/dev/null || true

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

declared_up=0 declared_down=""
while read -r name state; do
  [ -n "${name:-}" ] || continue
  if [ "$state" = "running" ]; then
    declared_up=$((declared_up + 1))
  else
    declared_down="$declared_down $name($state)"
  fi
done <<< "$declared_states"

say "dokku-reconcile: $checked apps, $started started, $imageless with no image, $unserved not served"
if [ "$declared_up" -gt 0 ] || [ -n "$declared_down" ]; then
  say "  declared containers: $declared_up running${declared_down:+, not running:$declared_down}"
fi
[ "$imageless" -eq 0 ] || say "  redeploy needed:$imageless_names"

# Report what answers into pulse, so a page off this box can draw it beside what hatchery declares.
# Non-fatal by contract: no key file means no report, and a failed post changes nothing about the exit below.
# The key travels in a header file and never on a command line.
KEY_FILE="$HOME/.roost_node_key"
PULSE="${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}"
if [ -f "$KEY_FILE" ]; then
  # The boot time rides along, because a box that reset is a better why than any app-level fact.
  running=$(docker ps --format '{{.Names}}' | grep -E '^[a-zA-Z0-9_.-]+\.[a-z]+\.[0-9]+$' || true)
  reading=$(printf '%s\n' "$vhosts" | python3 -c '
import json, sys
still = set(sys.argv[1].split())
imageless = set(sys.argv[2].split())
running = set(line.split(".")[0] for line in sys.argv[3].split())
apps = []
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    apps.append({"name": parts[0], "names": parts[1:], "served": parts[0] not in still, "running": parts[0] in running, "image": parts[0] not in imageless})

# A declared container carries the same keys as a dokku app so one reader draws
# both. It owns no vhost, and it is reached through itself, so served follows
# running; a name the box does not hold has no image under that name either.
seen = set(app["name"] for app in apps)
for line in sys.argv[7].splitlines():
    parts = line.split()
    if len(parts) != 2 or parts[0] in seen:
        continue
    name, state = parts
    seen.add(name)
    apps.append({"name": name, "names": [], "served": state == "running", "running": state == "running", "image": state != "absent", "state": state})

print(json.dumps({"host": "opi", "bootedAt": sys.argv[6], "apps": apps, "started": int(sys.argv[4]), "rebuilt": sys.argv[5] == "1"}))
' "$still" "$imageless_names" "$running" "$started" "$rebuilt" "$(uptime -s 2>/dev/null || true)" "$declared_states")
  HDR=$(mktemp)
  chmod 600 "$HDR"
  printf 'x-roost-node-key: %s\n' "$(cat "$KEY_FILE")" > "$HDR"
  if curl -sf -m 20 -X POST "$PULSE/api/answers" -H "content-type: application/json" -H "@$HDR" --data-binary "$reading" > /dev/null; then
    say "  report: what answers is on pulse"
  else
    say "  report: pulse did not take the reading"
  fi

  # The events, for the record that outlives the alert channel. A boot that changed since the last pass, and one line per thing this pass did.
  # A quiet pass posts nothing, so the record holds only what happened.
  BOOT_FILE="$HOME/.dokku-reconcile.boot"
  booted="$(uptime -s 2>/dev/null || true)"
  last_boot="$(cat "$BOOT_FILE" 2>/dev/null || true)"
  events=$(python3 -c '
import json, sys, time
booted, last_boot, started, imageless_names, unserved_names, still, rebuilt = sys.argv[1:8]
events = []
def add(kind, tone, message, subject="opi", at=None, detail=None):
    e = {"kind": kind, "source": "roost", "subject": subject, "tone": tone, "message": message}
    if at: e["at"] = at
    if detail: e["detail"] = detail
    events.append(e)
if booted and booted != last_boot:
    try:
        at = int(time.mktime(time.strptime(booted, "%Y-%m-%d %H:%M:%S")) * 1000)
    except ValueError:
        at = None
    add("boot", "warn", "opi booted at " + booted + (", after a reset nobody asked for" if last_boot else ""), at=at, detail={"bootedAt": booted, "before": last_boot or None})
if int(started) > 0:
    add("reconcile", "warn", "started %s app(s) that were deployed and down" % started)
if imageless_names.split():
    add("reconcile", "bad", "no image, and the proxy is poisoned until a redeploy: " + " ".join(imageless_names.split()), detail={"apps": imageless_names.split()})
if still.split():
    add("reconcile", "bad", "still not served after the proxy rebuild: " + " ".join(still.split()), detail={"apps": still.split()})
elif unserved_names.split():
    add("reconcile", "warn", "proxy rebuilt, serving again for " + " ".join(unserved_names.split()), detail={"apps": unserved_names.split()})
elif rebuilt == "1":
    add("reconcile", "warn", "rebuilt every vhost")
print(json.dumps({"events": events}) if events else "")
' "$booted" "$last_boot" "$started" "$imageless_names" "$unserved_names" "$still" "$rebuilt")
  if [ -n "$events" ]; then
    if curl -sf -m 20 -X POST "$PULSE/api/events" -H "content-type: application/json" -H "@$HDR" --data-binary "$events" > /dev/null; then
      say "  report: the events are on pulse"
    else
      say "  report: pulse did not take the events"
    fi
  fi
  [ -n "$booted" ] && printf '%s\n' "$booted" > "$BOOT_FILE"
  rm -f "$HDR"
fi

# Exit non-zero only for the thing a person must act on, so a timer stays quiet
# when it has nothing to say.
[ "$imageless" -eq 0 ]
