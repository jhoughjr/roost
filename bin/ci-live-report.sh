#!/bin/bash
# ci-live-report.sh — push each watched repo's LIVE CI runs (queued/in-progress)
# to the ci-live app, which the status board's live-console section polls.
#
# The static statusgen collector (ci_status.py) deliberately DROPS in-progress /
# queued runs — a push-based board can't show a run that outlives its own update
# step. This poller is the inverse: it keeps ONLY the live states and streams
# them to ci.jimmyhoughjr.net, so the board shows "running now" in real time.
#
# One-shot: run it and each project's live runs land at the endpoint. Run it
# from launchd every ~20 s for a live feed — see install-ci-live-report.sh.
#
# Config (~/.roostrc KEY=VALUE lines):
#   ROOST_CI_LIVE_REPOS     owner/repo:project:intervalSec,...  (required)
#                           e.g. Austin-MacWorks/Phoenix-Electron:phoenix:30
#   ROOST_CI_LIVE_ENDPOINT  ci-live base URL (default https://ci.jimmyhoughjr.net)
# Shared key: ~/.roost_ci_key (chmod 600), must match `dokku config ci-live CI_KEY`.
#
# Non-fatal per project: a repo that errors is skipped; the rest still push.
set -uo pipefail

# launchd runs with a minimal PATH that omits Homebrew, so `gh`/`jq` (in
# /opt/homebrew/bin) aren't found. Prepend the brew bins so the job works the
# same from launchd as from an interactive shell.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

RC="$HOME/.roostrc"
[ -f "$RC" ] && . "$RC"

KEY_FILE="$HOME/.roost_ci_key"
[ -f "$KEY_FILE" ] || { echo "ci-live-report: missing $KEY_FILE (the ci-live CI_KEY)" >&2; exit 1; }
KEY=$(cat "$KEY_FILE")

REPOS="${ROOST_CI_LIVE_REPOS:-}"
[ -n "$REPOS" ] || { echo "ci-live-report: ROOST_CI_LIVE_REPOS not set — nothing to do" >&2; exit 0; }
ENDPOINT="${ROOST_CI_LIVE_ENDPOINT:-https://ci.jimmyhoughjr.net}"

command -v gh  >/dev/null || { echo "ci-live-report: gh not found" >&2; exit 1; }
command -v jq  >/dev/null || { echo "ci-live-report: jq not found" >&2; exit 1; }

# The live states — exactly the ones ci_status.py's CONSOLE_SKIP drops.
LIVE='["in_progress","queued","waiting","requested"]'

IFS=',' read -r -a SPECS <<< "$REPOS"
for SPEC in "${SPECS[@]}"; do
  SPEC="$(echo "$SPEC" | tr -d '[:space:]')"
  [ -n "$SPEC" ] || continue
  REPO="${SPEC%%:*}"; REST="${SPEC#*:}"
  PROJECT="${REST%%:*}"; INTERVAL="${REST##*:}"
  # Defaults if the spec omitted fields.
  [ "$PROJECT" = "$REST" ] && PROJECT="${REPO##*/}"      # no project → repo name
  case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=30 ;; esac    # non-numeric → 30s
  [ -n "$REPO" ] && [ -n "$PROJECT" ] || continue

  RUNS=$(gh run list --repo "$REPO" --limit 12 \
    --json status,conclusion,headBranch,event,createdAt,url,databaseId 2>/dev/null) || RUNS=""
  [ -n "$RUNS" ] || { echo "ci-live-report: $REPO — gh returned nothing, skipping"; continue; }

  # Keep only live states; build the exact console-line dicts the board expects.
  # text  "<Label> · <headBranch>"   (Label = repo name, title-ish)
  # meta  "· <event>"     tone "wip"   ts createdAt   href url
  # cmd   "gh run watch <id> -R <repo>"  → the copy-to-clipboard "watch it live" chip
  LINES=$(echo "$RUNS" | jq -c --arg repo "$REPO" --argjson live "$LIVE" '
    ($repo | split("/") | last) as $label
    | [ .[]
        | select(.status as $s | $live | index($s))
        | {
            status: (.status | gsub("_"; " ")),
            tone:   "wip",
            text:   ($label + " · " + (.headBranch // "?")),
            meta:   ("· " + (.event // "")),
            ts:     .createdAt,
            href:   .url,
            cmd:    ("gh run watch " + (.databaseId | tostring) + " -R " + $repo)
          } ]
  ') || { echo "ci-live-report: $REPO — jq transform failed, skipping"; continue; }

  BODY=$(jq -n --arg project "$PROJECT" --argjson lines "$LINES" \
    --argjson intervalMs "$(( INTERVAL * 1000 ))" \
    '{project: $project, lines: $lines, intervalMs: $intervalMs}')

  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST "$ENDPOINT/api/runs" \
    -H "x-roost-ci-key: $KEY" \
    -H "content-type: application/json" \
    -d "$BODY") || CODE=000
  N=$(echo "$LINES" | jq 'length')
  echo "ci-live-report: $PROJECT ($REPO) — $N live run(s) → $CODE"
done
