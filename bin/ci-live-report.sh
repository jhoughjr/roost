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
#   ROOST_CI_LIVE_REPOS     owner/repo:project:intervalSec[:Label],...  (required)
#                           e.g. Austin-MacWorks/Phoenix-Electron:phoenix:30:Phoenix
#                           Label is what the board's console prints; it
#                           defaults to the repo name, so match it to the
#                           label in ROOST_CI_REPOS or the same repo will be
#                           called two different things depending on whether
#                           its run is still going.
#   ROOST_CI_LIVE_AGGREGATE optional project name carrying EVERY repo's live
#                           runs merged into one feed, newest first. A board's
#                           live-console polls exactly one project, so without
#                           this a two-repo board can only ever show one repo
#                           running — and the long build is the one you most
#                           want to watch. Per-repo projects are still pushed;
#                           give this a name none of them use (e.g. `all`).
#   ROOST_CI_LIVE_ENDPOINT  ci-live base URL (default https://ci.jimmyhoughjr.net)
# Shared key: ~/.roost_ci_key (chmod 600), must match `dokku config ci-live CI_KEY`.
#
# Non-fatal per project: a repo that errors is skipped; the rest still push.
set -uo pipefail

# launchd runs with a minimal PATH that omits Homebrew, so `gh`/`jq` (in
# /opt/homebrew/bin) aren't found. Prepend the brew bins so the job works the
# same from launchd as from an interactive shell.
#
# Only when they're actually missing: an unconditional prepend also overrides a
# caller who put its own `gh` ahead of the real one, which is exactly how this
# script is tested against canned runs.
if ! command -v gh >/dev/null || ! command -v jq >/dev/null; then
  export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
fi

RC="$HOME/.roostrc"
# shellcheck source=/dev/null
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

AGGREGATE="${ROOST_CI_LIVE_AGGREGATE:-}"
ALL_LINES='[]'      # every repo's live lines, for the aggregate feed
AGG_INTERVAL=''     # the aggregate refreshes as often as its fastest member
AGG_REPOS=0         # how many repos actually answered this pass

# POST one project's console lines to the ci-live store.
push_project() {
  local project="$1" lines="$2" interval="$3" source="$4" body code n
  body=$(jq -n --arg project "$project" --argjson lines "$lines" \
    --argjson intervalMs "$(( interval * 1000 ))" \
    '{project: $project, lines: $lines, intervalMs: $intervalMs}')
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST "$ENDPOINT/api/runs" \
    -H "x-roost-ci-key: $KEY" \
    -H "content-type: application/json" \
    -d "$body") || code=000
  n=$(jq 'length' <<< "$lines")
  echo "ci-live-report: $project ($source) — $n live run(s) → $code"
}

IFS=',' read -r -a SPECS <<< "$REPOS"
for SPEC in "${SPECS[@]}"; do
  SPEC="$(echo "$SPEC" | tr -d '[:space:]')"
  [ -n "$SPEC" ] || continue
  # Split on ':' by field rather than by prefix/suffix trimming: with the
  # optional 4th field, "last field" is the label, not the interval.
  IFS=':' read -r REPO PROJECT INTERVAL LABEL <<< "$SPEC"
  # Defaults if the spec omitted fields.
  [ -n "$PROJECT" ] || PROJECT="${REPO##*/}"              # no project → repo name
  case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=30 ;; esac    # non-numeric → 30s
  [ -n "$LABEL" ] || LABEL="${REPO##*/}"                  # no label → repo name
  [ -n "$REPO" ] && [ -n "$PROJECT" ] || continue

  RUNS=$(gh run list --repo "$REPO" --limit 12 \
    --json status,conclusion,headBranch,event,createdAt,url,databaseId 2>/dev/null) || RUNS=""
  [ -n "$RUNS" ] || { echo "ci-live-report: $REPO — gh returned nothing, skipping"; continue; }

  # Keep only live states; build the exact console-line dicts the board expects.
  # text  "<Label> · <headBranch>"   (Label from the spec, or the repo name)
  # meta  "· <event>"     tone "wip"   ts createdAt   href url
  # cmd   "gh run watch <id> -R <repo>"  → the copy-to-clipboard "watch it live" chip
  LINES=$(echo "$RUNS" | jq -c --arg repo "$REPO" --arg label "$LABEL" \
    --argjson live "$LIVE" '
    [ .[]
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

  AGG_REPOS=$(( AGG_REPOS + 1 ))
  if [ -n "$AGGREGATE" ]; then
    ALL_LINES=$(jq -c -n --argjson a "$ALL_LINES" --argjson b "$LINES" '$a + $b')
    if [ -z "$AGG_INTERVAL" ] || [ "$INTERVAL" -lt "$AGG_INTERVAL" ]; then
      AGG_INTERVAL="$INTERVAL"
    fi
  fi

  push_project "$PROJECT" "$LINES" "$INTERVAL" "$REPO"
done

# The merged feed, newest run first regardless of which repo it came from.
#
# Only when at least one repo answered: an empty aggregate posted after a
# wholesale gh failure would read as "nothing is building" and quietly replace
# a feed that was correct a minute ago. Per-repo pushes already skip on error
# for the same reason. An empty aggregate from repos that DID answer is real
# and must be posted, or finished runs would linger as "running" forever.
if [ -n "$AGGREGATE" ] && [ "$AGG_REPOS" -gt 0 ]; then
  MERGED=$(jq -c 'sort_by(.ts // "") | reverse' <<< "$ALL_LINES") || MERGED="$ALL_LINES"
  push_project "$AGGREGATE" "$MERGED" "${AGG_INTERVAL:-30}" "$AGG_REPOS repos"
fi
