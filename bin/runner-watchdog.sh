#!/usr/bin/env bash
set -euo pipefail
# Off-box watchdog for the self-hosted runner fleet.
#
# Why this exists, and why it must NOT run on the mini: every other alarm we
# have — notify-outcome.sh, check-nightly-drift.sh, update-board.sh — is a step
# inside the CI job itself. When the runner is the thing that's down, the job
# never starts, so none of them fire. CI goes dark completely silently and the
# board just keeps serving the last green run's numbers. That is exactly how
# 2026-07-21 went unnoticed for ~14h: the mini rebooted into a FileVault lock,
# its LaunchAgents never came back, and the only signal was a build date that
# looked stale to a human.
#
# So this runs on opi (Linux, always on, independent power) and watches the
# fleet from the outside via the GitHub API. It alerts on two things:
#   1. a runner that should be online reading "offline" for >= GRACE minutes
#   2. a workflow run sitting "queued" for >= QUEUED_MAX minutes (the symptom
#      when a job is pinned to a runner that isn't there — nothing fails, it
#      just waits forever)
#
# Deliberately depends on curl + python3 ONLY. Not `gh`: the repo-stats
# collector already died once on a missing `gh` in a non-interactive PATH, and
# a watchdog that silently fails is worse than no watchdog. Not `jq` either —
# it isn't guaranteed on a dokku host. python3 is already a CI-script
# dependency (see publish-build.sh).
#
# Usage: runner-watchdog.sh [--dry-run]
#   --dry-run  print what would be sent instead of pinging ntfy, and don't
#              persist state. Safe to run anywhere; used to verify config.

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# systemd hands the unit its EnvironmentFile, but a hand-run of this script
# gets nothing — so `--dry-run` would report a missing token while the timer
# worked fine, which is the most confusing possible split. Source it here too
# so a manual run and the service see identical config.
ENV_FILE="${WATCHDOG_ENV_FILE:-$HOME/.config/phoenix-watchdog.env}"
if [ -r "$ENV_FILE" ]; then
  # shellcheck source=/dev/null
  . "$ENV_FILE"
fi

roostrc_get() { grep "^$1=" "$HOME/.roostrc" 2>/dev/null | cut -d= -f2- || true; }
cfg() { # cfg NAME default  — env wins, then .roostrc, then default
  local v="${!1:-}"
  [ -n "$v" ] || v="$(roostrc_get "$1")"
  [ -n "$v" ] || v="$2"
  printf '%s' "$v"
}

REPO="$(cfg WATCHDOG_REPO 'Austin-MacWorks/Phoenix-Electron')"
# Runners we expect to be up. Comma-separated. A runner not in this list is
# ignored (the laptop comes and goes by design and must never page anyone).
EXPECTED="$(cfg WATCHDOG_RUNNERS 'jimmys-mac-mini')"
GRACE_MIN="$(cfg WATCHDOG_GRACE_MIN 30)"
QUEUED_MAX_MIN="$(cfg WATCHDOG_QUEUED_MIN 45)"
TOPIC="$(cfg ROOST_NTFY_TOPIC '')"
STATE_DIR="$(cfg WATCHDOG_STATE_DIR "$HOME/.ci-state/runner-watchdog")"
# Needs Administration:read (fine-grained) or classic `repo` scope to list
# runners. Kept separate from any push token — this one is read-only.
TOKEN="$(cfg WATCHDOG_TOKEN "$(cfg GITHUB_TOKEN '')")"

if [ -z "$TOKEN" ]; then
  echo "watchdog: no WATCHDOG_TOKEN/GITHUB_TOKEN configured; cannot query GitHub" >&2
  exit 1
fi

mkdir -p "$STATE_DIR"
NOW="$(date +%s)"

api() { # api <path> -> JSON on stdout, non-zero on HTTP error
  local path="$1" out code
  out="$(curl -sS -w $'\n%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/$REPO$path" 2>&1)" || { echo "$out" >&2; return 1; }
  code="$(printf '%s' "$out" | tail -1)"
  if [ "$code" != "200" ]; then
    echo "watchdog: GET $path -> HTTP $code" >&2
    printf '%s' "$out" | sed '$d' >&2
    return 1
  fi
  printf '%s' "$out" | sed '$d'
}

notify() { # notify <title> <priority> <message>
  local title="$1" prio="$2" msg="$3"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] ntfy: [$prio] $title — $msg"
    return 0
  fi
  if [ -z "$TOPIC" ]; then
    echo "watchdog: no ROOST_NTFY_TOPIC; would have sent: $title — $msg"
    return 0
  fi
  curl -s -H "Title: $title" -H "Priority: $prio" \
    -d "$msg" "https://ntfy.sh/$TOPIC" >/dev/null || true
}

# ---------------------------------------------------------------- runners ---
# Emits one "<name>\t<status>" line per runner.
runners_json="$(api /actions/runners)" || exit 1
runner_status="$(printf '%s' "$runners_json" | python3 -c '
import json, sys
for r in json.load(sys.stdin).get("runners", []):
    print("%s\t%s" % (r["name"], r["status"]))
')"

alerted_any=0
IFS=',' read -ra want <<< "$EXPECTED"
for name in "${want[@]}"; do
  name="$(printf '%s' "$name" | tr -d '[:space:]')"
  [ -n "$name" ] || continue
  status="$(printf '%s' "$runner_status" | awk -F'\t' -v n="$name" '$1==n {print $2}')"
  # A runner that has been de-registered entirely is as bad as one that's
  # offline — treat a missing name as offline rather than skipping it.
  [ -n "$status" ] || status="missing"

  since_file="$STATE_DIR/${name}.offline_since"
  alerted_file="$STATE_DIR/${name}.alerted"

  if [ "$status" = "online" ]; then
    if [ -f "$alerted_file" ]; then
      down_for=$(( (NOW - $(cat "$since_file" 2>/dev/null || echo "$NOW")) / 60 ))
      notify "Runner back online" "default" \
        "$name is online again (was down ~${down_for}m). Queued jobs should drain now."
      alerted_any=1
    fi
    [ "$DRY_RUN" = "1" ] || rm -f "$since_file" "$alerted_file"
    echo "runner $name: online"
    continue
  fi

  # offline / missing
  if [ -f "$since_file" ]; then
    since="$(cat "$since_file")"
  else
    since="$NOW"
    [ "$DRY_RUN" = "1" ] || printf '%s\n' "$since" > "$since_file"
  fi
  down_min=$(( (NOW - since) / 60 ))
  echo "runner $name: $status for ${down_min}m (grace ${GRACE_MIN}m)"

  # Alert once per outage, not once per tick — this runs on a timer and a
  # 14h outage must not produce 28 notifications.
  if [ "$down_min" -ge "$GRACE_MIN" ] && [ ! -f "$alerted_file" ]; then
    notify "Runner offline: $name" "high" \
      "$name has been $status for ${down_min}m. CI on $REPO cannot run — no job-level alert will fire, because the alerts run on the runner."
    alerted_any=1
    [ "$DRY_RUN" = "1" ] || touch "$alerted_file"
  fi
done

# ----------------------------------------------------------- queued runs ---
# A job pinned to an absent runner doesn't fail, it queues forever. Catch that
# directly so the alert names the stuck run rather than just the dead host.
queued_json="$(api "/actions/runs?status=queued&per_page=30")" || queued_json=''
if [ -n "$queued_json" ]; then
  stuck="$(printf '%s' "$queued_json" | python3 -c '
import json, sys, datetime
now = datetime.datetime.now(datetime.timezone.utc)
limit = int(sys.argv[1])
for r in json.load(sys.stdin).get("workflow_runs", []):
    created = datetime.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
    mins = int((now - created).total_seconds() // 60)
    if mins >= limit:
        print("%s\t%s\t%s\t%s" % (r["id"], mins, r["head_branch"], r["event"]))
' "$QUEUED_MAX_MIN")"

  while IFS=$'\t' read -r run_id mins branch event; do
    [ -n "$run_id" ] || continue
    seen_file="$STATE_DIR/queued-${run_id}.alerted"
    echo "queued run $run_id: ${mins}m ($branch/$event)"
    if [ ! -f "$seen_file" ]; then
      hrs=$(( mins / 60 )); rem=$(( mins % 60 ))
      notify "CI run stuck in queue" "high" \
        "run $run_id ($branch, $event) has been queued ${hrs}h${rem}m with no runner to pick it up."
      alerted_any=1
      [ "$DRY_RUN" = "1" ] || touch "$seen_file"
    fi
  done <<< "$stuck"
fi

# Prune queued-run markers older than a week so the state dir can't grow
# without bound as run ids churn.
find "$STATE_DIR" -name 'queued-*.alerted' -mtime +7 -delete 2>/dev/null || true

[ "$alerted_any" = "1" ] && echo "watchdog: alerts sent" || echo "watchdog: nothing to report"
exit 0
