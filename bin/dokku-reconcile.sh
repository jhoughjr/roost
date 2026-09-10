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
# So this does four things, in order, and reports:
#   1. starts any app dokku believes is deployed but is not running
#   2. names any app whose image is missing, because that one poisons the proxy and a person has to redeploy it
#   3. rebuilds every vhost, which is what unwedges nginx
#   4. asks each declared app its own health path, and names the one that answers 5xx behind a working vhost
#
# The fourth is the one the first three cannot see. nginx answering for a name says the proxy works, not that the app does.
# The health path comes from hatchery's declaration, so an app starts being asked the moment its kind file names a path.
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
#
# It answers for the declared jobs on this box the same way. A job runs under systemd rather than
# under docker, so systemd is asked instead: whether the last run exited zero, what it exited, and
# when. This pass never starts one either.
set -uo pipefail

# Safe to source: the secret reader reads ~/.roostrc key by key and never sources it, so that file
# cannot set a variable in this script, and this script decides what gets restarted.
# The repo keeps lib/ beside bin/, and the installed copy keeps its own lib/ inside the
# install directory, because install-dokku-reconcile.sh copies this one file out of the repo.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/lib"
[ -f "$LIB/roost-secret.sh" ] || LIB="$HERE/../lib"
# shellcheck source=/dev/null
. "$LIB/roost-secret.sh"

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
        # A job runs under systemd and not under docker, so asking the daemon about it would
        # report every declared job as a container the box does not hold.
        if service.get("kind") == "job":
            continue
        name = service.get("name")
        if name:
            print(name, states.get(name, "absent"))
' "$declared_file" \
  "$(docker ps -a --format '{{.Names}} {{.State}}' || true)" \
  "127.0.0.1 localhost $(hostname -I 2>/dev/null || true)")

# The jobs the declaration names for this box, one per line with its unit type.
#
# A job is answered for by systemd rather than by docker, and by this box rather than by the Mac
# that declares it, so the platform decides whose question it is. A scheduled job has a timer unit;
# a kept-alive job has a service unit. The output is `name|type` where type is "timer" or "service".
declared_job_units=$(python3 -c '
import json, sys
declared_file, box_ips = sys.argv[1], sys.argv[2]
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
        if service.get("kind") != "job":
            continue
        if (service.get("platform") or "linux") != "linux":
            continue
        name = service.get("name")
        if name:
            keep_alive = service.get("keepAlive", False)
            unit_type = "service" if keep_alive else "timer"
            print(f"{name}|{unit_type}")
' "$declared_file" "127.0.0.1 localhost $(hostname -I 2>/dev/null || true)")

# What systemd says about each of them, as `name|state|exit|when`.
#
# The supervisor is the only witness a job has. It answers no address and owns no vhost, so its last
# exit and the time of it are the whole reading. A scheduled job has a timer unit that records its last
# trigger, so we query the timer for the timestamp and the service for the exit code and active state.
# A kept-alive job (no timer) reads the service only. The state is never-ran when the timer never fired or
# ExecMainStartTimestamp is empty, running when ActiveState is active or activating, ok when Result is success
# and ExecMainStatus is 0, and failed otherwise.
declared_job_states=""
while IFS='|' read -r unit unit_type; do
  [ -n "${unit:-}" ] || continue
  if [ "$unit_type" = "timer" ]; then
    shown=$(systemctl --user show "$unit.timer" \
      -p LastTriggerUSec 2>/dev/null || true)
    when=$(printf '%s\n' "$shown" | sed -n 's/^LastTriggerUSec=//p')
    shown_svc=$(systemctl --user show "$unit.service" \
      -p ExecMainStatus -p ActiveState -p Result 2>/dev/null || true)
    code=$(printf '%s\n' "$shown_svc" | sed -n 's/^ExecMainStatus=//p')
    active=$(printf '%s\n' "$shown_svc" | sed -n 's/^ActiveState=//p')
    result=$(printf '%s\n' "$shown_svc" | sed -n 's/^Result=//p')
    if [ -z "$when" ] || [ "$when" = "0" ]; then
      state="never-ran"
    elif [ "$active" = "active" ] || [ "$active" = "activating" ]; then
      state="running"
    elif [ "$result" = "success" ] && [ "$code" = "0" ]; then
      state="ok"
    else
      state="failed"
    fi
  else
    shown=$(systemctl --user show "$unit.service" \
      -p ExecMainStatus -p ActiveState -p Result -p ExecMainStartTimestamp 2>/dev/null || true)
    code=$(printf '%s\n' "$shown" | sed -n 's/^ExecMainStatus=//p')
    active=$(printf '%s\n' "$shown" | sed -n 's/^ActiveState=//p')
    result=$(printf '%s\n' "$shown" | sed -n 's/^Result=//p')
    when=$(printf '%s\n' "$shown" | sed -n 's/^ExecMainStartTimestamp=//p')
    if [ -z "$when" ]; then
      state="never-ran"
    elif [ "$active" = "active" ] || [ "$active" = "activating" ]; then
      state="running"
    elif [ "$result" = "success" ] && [ "$code" = "0" ]; then
      state="ok"
    else
      state="failed"
    fi
  fi
  declared_job_states="$declared_job_states$unit|$state|${code:-}|${when:-}
"
done <<< "$declared_job_units"

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
# The code each app gave is kept as well as whether it gave one. This pass asks only whether nginx answers, and the
# code itself is what fleet-board.py currently makes its own ssh round trip to learn.
app_codes=""
while read -r app hosts; do
  [ -n "${hosts:-}" ] || continue
  served=0
  for host in $hosts; do
    # An answer of any kind means nginx holds a vhost for the name, which is the only question here.
    code=$(probe_name "$host")
    if [ -n "$code" ] && [ "$code" != "000" ]; then served=1; break; fi
  done
  app_codes="$app_codes$app=$code
"
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

# Track restored and newly quarantined apps for event posting later.
restored_apps=""
newly_quarantined=""
# On every pass, check if any quarantined app's container is now running.
for app in $quarantined; do
  if docker ps --format '{{.Names}}' | grep -qx "${app}.web.1"; then
    dok proxy:enable "$app" > /dev/null 2>&1
    dok proxy:build-config "$app" > /dev/null 2>&1
    say "  proxy: $app is back, its vhost restored"
    restored_apps="$restored_apps $app"
    quarantined="${quarantined// "$app" / }"
    quarantined="${quarantined#"$app" }"
    quarantined="${quarantined% "$app"}"
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
      newly_quarantined="$newly_quarantined $app"
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
    # A rebuild that does not restore serving means the remedy is the wrong one,
    # and retrying it every ten minutes forever is not a fix, it is a silence.
    # rookery sat unreachable for three hours on 2026-09-09 while this exact line
    # was written to the journal each pass and nobody was told.
    #
    # Whether it is worth telling a person is pulse's to answer now. This pass says what it found,
    # in the journal and in the reading it posts, and pulse holds the memory of what changed.
    if [ -n "$still" ]; then
      say "  proxy: still not serving after the rebuild:$still"
    else
      say "  proxy: serving again for$unserved_names"
    fi
  fi
else
  say "  proxy: nothing changed since the last pass, left alone"
fi

# An app whose vhost works can still be broken: nginx answers for the name, and the app behind it does not.
# The pass above cannot see that, because it asks only whether an answer comes back, and 502 is an answer.
# That is how the status board served 502 while every inventory on this box called the estate healthy, and a person found it rather than this script.
#
# So this pass asks each app the question its declaration wrote down: the health path hatchery publishes from the kind file.
# It starts nothing and rebuilds nothing. A bad answer behind a working vhost is the app's own fault, and the proxy is not the remedy for it.
declared_health=$(python3 -c '
import json, sys
declared_file, box_ips = sys.argv[1], sys.argv[2]
here = set(box_ips.split())
declared = {}
if declared_file:
    try:
        with open(declared_file) as fh:
            declared = json.load(fh)
    except (OSError, ValueError):
        declared = {}
for stack in declared.get("stacks", []):
    if stack.get("backend") != "dokku":
        continue
    if (stack.get("host") or "").split("@")[-1] not in here:
        continue
    for service in stack.get("services", []):
        name = service.get("name")
        path = service.get("healthPath")
        domains = [domain for domain in service.get("domains", []) if domain]
        # A service that declares no path asks no question, and one with no name cannot be asked.
        if name and path and domains:
            print("%s|%s|%s" % (name, path, " ".join(domains)))
' "$declared_file" \
  "127.0.0.1 localhost $(hostname -I 2>/dev/null || true)")

# The code the app gives at its declared path, over the same two ports the serving probe uses.
probe_health() {
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 --resolve "$1:443:127.0.0.1" "https://$1$2" 2>/dev/null </dev/null)
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 -H "Host: $1" "http://127.0.0.1$2" 2>/dev/null </dev/null)
  fi
  printf '%s' "$code"
}

unhealthy_names=""
while IFS='|' read -r app path hosts; do
  [ -n "${app:-}" ] || continue
  code=""
  for host in $hosts; do
    code=$(probe_health "$host" "$path")
    # A name nginx holds no vhost for is the pass above's to report, so this one asks the next name instead.
    if [ -n "$code" ] && [ "$code" != "000" ]; then break; fi
  done
  case "$code" in
    5*)
      unhealthy_names="$unhealthy_names $app"
      say "  $app: nginx answers for it, and the app gives $code at $path"
      ;;
  esac
done <<< "$declared_health"

# The names this pass asked, so the reading can say `healthy` for them and null for every app that declares no path.
asked_names=$(printf '%s\n' "$declared_health" | cut -d'|' -f1 | tr '\n' ' ')

declared_up=0 declared_down=""
while read -r name state; do
  [ -n "${name:-}" ] || continue
  if [ "$state" = "running" ]; then
    declared_up=$((declared_up + 1))
  else
    declared_down="$declared_down $name($state)"
  fi
done <<< "$declared_states"

# Collect database states for declared host containers.
# For each container with a databases list, query its postgres cluster if running.
database_states=$(python3 -c '
import json, sys, subprocess
declared_file, box_ips, container_states = sys.argv[1], sys.argv[2], sys.argv[3]
here = set(box_ips.split())
declared = {}
if declared_file:
    try:
        with open(declared_file) as fh:
            declared = json.load(fh)
    except (OSError, ValueError):
        declared = {}

# Parse current container states.
states = dict(
    (parts[0], parts[1])
    for parts in (line.split() for line in container_states.splitlines())
    if len(parts) == 2
)

for stack in declared.get("stacks", []):
    if stack.get("backend") != "host":
        continue
    if (stack.get("host") or "").split("@")[-1] not in here:
        continue
    for service in stack.get("services", []):
        name = service.get("name")
        if not name or not service.get("databases"):
            continue
        container_state = states.get(name)
        # Query databases only if the container is running.
        if container_state != "running":
            for db_decl in service.get("databases", []):
                db_name = db_decl.get("name")
                if db_name:
                    print(f"{name}/{db_name} unreachable")
            continue
        # Check postgres readiness and list databases.
        try:
            subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                          check=True, capture_output=True, timeout=10)
            # pg_isready succeeded, query the database list.
            result = subprocess.run(["docker", "exec", name, "psql", "-U", "postgres", "-Atc", "select datname from pg_database"],
                                   capture_output=True, text=True, timeout=10)
            databases = set(result.stdout.strip().split())
            for db_decl in service.get("databases", []):
                db_name = db_decl.get("name")
                if db_name:
                    state = "present" if db_name in databases else "missing"
                    print(f"{name}/{db_name} {state}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # pg_isready failed or query timed out.
            for db_decl in service.get("databases", []):
                db_name = db_decl.get("name")
                if db_name:
                    print(f"{name}/{db_name} unreachable")
' "$declared_file" \
  "127.0.0.1 localhost $(hostname -I 2>/dev/null || true)" \
  "$(docker ps -a --format '{{.Names}} {{.State}}' || true)")

say "dokku-reconcile: $checked apps, $started started, $imageless with no image, $unserved not served"
if [ "$declared_up" -gt 0 ] || [ -n "$declared_down" ]; then
  say "  declared containers: $declared_up running${declared_down:+, not running:$declared_down}"
fi

jobs_ok=0 jobs_bad=""
while IFS='|' read -r unit state code when; do
  [ -n "${unit:-}" ] || continue
  if [ "$state" = "ok" ] || [ "$state" = "running" ]; then
    jobs_ok=$((jobs_ok + 1))
  elif [ "$state" = "never-ran" ]; then
    jobs_bad="$jobs_bad $unit(never-ran)"
  else
    jobs_bad="$jobs_bad $unit(exit $code)"
  fi
done <<< "$declared_job_states"
if [ "$jobs_ok" -gt 0 ] || [ -n "$jobs_bad" ]; then
  say "  declared jobs: $jobs_ok ok${jobs_bad:+, not ok:$jobs_bad}"
fi
[ "$imageless" -eq 0 ] || say "  redeploy needed:$imageless_names"
[ -z "$unhealthy_names" ] || say "  not healthy:$unhealthy_names"

# Report what answers into pulse, so a page off this box can draw it beside what hatchery declares.
# Non-fatal by contract: no key means no report, and a failed post changes nothing about the exit below.
# The key travels in a header file and never on a command line.
NODE_KEY="$(roost_secret NODE_KEY || true)"
PULSE="${ROOST_PULSE_URL:-https://pulse.jimmyhoughjr.net}"
if [ -n "$NODE_KEY" ]; then
  # The boot time rides along, because a box that reset is a better why than any app-level fact.
  running=$(docker ps --format '{{.Names}}' | grep -E '^[a-zA-Z0-9_.-]+\.[a-z]+\.[0-9]+$' || true)
  # Memory and age for every container in two calls, rather than one `dokku enter` per app from a Mac.
  # fleet-board.py asks the box for these one app at a time over ssh, which is twenty-four round trips for facts
  # the box can read about all of them at once. Reading them here is what lets that collector stop asking.
  # `docker stats` takes a second or so because it samples, which is affordable once every ten minutes and was not
  # affordable per app.
  container_mem=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null || true)
  container_age=$(docker ps -a --format '{{.Names}} {{.CreatedAt}}' 2>/dev/null || true)
  reading=$(printf '%s\n' "$vhosts" | python3 -c '
import json, sys
still = set(sys.argv[1].split())
imageless = set(sys.argv[2].split())
running = set(line.split(".")[0] for line in sys.argv[3].split())
asked = set(sys.argv[10].split())
unhealthy = set(sys.argv[11].split())

# What the box knows about the containers of each app, which nothing off the box can read cheaply.
# procs counts the containers an app owns rather than the process types dokku declares. They agree for every app the
# estate runs, and this one is a fact about what is up rather than about what was asked for.
def megabytes(text):
    """docker stats writes 12.34MiB / 7.75GiB, and only the first half belongs to this container."""
    used = text.split("/")[0].strip()
    for suffix, scale in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024.0), ("B", 1 / 1048576.0)):
        if used.endswith(suffix):
            try:
                return float(used[: -len(suffix)]) * scale
            except ValueError:
                return None
    return None

app_facts = {}
for line in sys.argv[12].splitlines():
    name, _, usage = line.partition(" ")
    if "." not in name or not usage:
        continue
    mb = megabytes(usage)
    if mb is None:
        continue
    entry = app_facts.setdefault(name.split(".")[0], {})
    entry["memMb"] = round(entry.get("memMb", 0.0) + mb, 1)
    entry["procs"] = entry.get("procs", 0) + 1
for line in sys.argv[14].splitlines():
    app_name, _, code = line.partition("=")
    if app_name and code:
        app_facts.setdefault(app_name, {})["httpCode"] = code.strip()
for line in sys.argv[13].splitlines():
    name, _, created = line.partition(" ")
    if "." not in name or not created:
        continue
    entry = app_facts.setdefault(name.split(".")[0], {})
    # The oldest container an app owns says how long the app has been up.
    if "createdAt" not in entry or created < entry["createdAt"]:
        entry["createdAt"] = created.strip()[:10]
apps = []
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    # `healthy` is absent for an app that declares no health path: nothing was asked of it, so nothing is claimed.
    app = {"name": parts[0], "names": parts[1:], "served": parts[0] not in still, "running": parts[0] in running, "image": parts[0] not in imageless}
    if parts[0] in asked:
        app["healthy"] = parts[0] not in unhealthy
    facts = app_facts.get(parts[0])
    if facts:
        app.update(facts)
    apps.append(app)

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

# Add database rows from the database_states.
for line in sys.argv[8].splitlines():
    parts = line.split()
    if len(parts) != 2:
        continue
    name, state = parts
    # Database row: kind is "database", name is "container/db", state maps to presence.
    # running and served depend on the database state.
    apps.append({"name": name, "kind": "database", "state": state, "running": state == "present", "served": state == "present"})

# A declared job carries the app keys too, so the same reader draws it, and the state from systemd.
# The state comes from the bash reading: never-ran when the timer never fired or the service never started,
# running when ActiveState is active or activating, ok when Result is success and ExecMainStatus is 0,
# and failed otherwise.
for line in sys.argv[9].splitlines():
    parts = line.split("|")
    if len(parts) != 4 or not parts[0] or parts[0] in seen:
        continue
    name, state, code, when = parts
    seen.add(name)
    apps.append({"name": name, "names": [], "kind": "job", "state": state,
                 "exit": int(code) if code and code.lstrip("-").isdigit() else None, "at": when,
                 "served": state == "ok", "running": state == "running", "image": True})

print(json.dumps({"node": "opi", "host": "opi", "bootedAt": sys.argv[6], "apps": apps, "started": int(sys.argv[4]), "rebuilt": sys.argv[5] == "1"}))
' "$still" "$imageless_names" "$running" "$started" "$rebuilt" "$(uptime -s 2>/dev/null || true)" "$declared_states" "$database_states" "$declared_job_states" "$asked_names" "$unhealthy_names" "$container_mem" "$container_age" "$app_codes")
  HDR=$(mktemp)
  chmod 600 "$HDR"
  printf 'x-roost-node-key: %s\n' "$NODE_KEY" > "$HDR"
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
booted, last_boot, started, imageless_names, unserved_names, still, rebuilt, restored_apps, newly_quarantined, unhealthy_names = sys.argv[1:11]
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
# Post quarantine events for newly quarantined apps.
for app in newly_quarantined.split():
    add("quarantine", "warn", app + " quarantined, its container has no address", subject=app, detail={"reason": "poisoned upstream"})
# Post restore events for restored apps.
for app in restored_apps.split():
    add("restore", "go", app + " is back, vhost restored", subject=app)
if still.split():
    add("unserved", "err", "still not served after the proxy rebuild: " + " ".join(still.split()), detail={"apps": still.split()})
elif unserved_names.split():
    add("reconcile", "warn", "proxy rebuilt, serving again for " + " ".join(unserved_names.split()), detail={"apps": unserved_names.split()})
elif rebuilt == "1":
    add("reconcile", "warn", "rebuilt every vhost")
if unhealthy_names.split():
    add("unhealthy", "err", "answering, and failing its own health path: " + " ".join(unhealthy_names.split()), detail={"apps": unhealthy_names.split()})
print(json.dumps({"events": events}) if events else "")
' "$booted" "$last_boot" "$started" "$imageless_names" "$unserved_names" "$still" "$rebuilt" "$restored_apps" "$newly_quarantined" "$unhealthy_names")
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
