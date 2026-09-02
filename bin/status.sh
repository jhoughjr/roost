#!/usr/bin/env bash
# status.sh — regenerate every board and deploy the status site.
# Invoked as `roost status ["message"]`. This is the ONE orchestration point:
#   • the site (ROOST_STATUS_SITE) is pure data — board.json + shells + manifest
#   • statusgen (ROOST_STATUSGEN) is the library — schema, renderer, validator,
#     and the generic collectors under bin/collect/
#   • roost (this repo) is the driver — resolves where everything lives (via
#     ~/.roostrc), runs the collectors, keeps the renderer in sync, gates on the
#     schema, and deploys.
# See statusgen/INTERFACES.md for the full contract.
set -euo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$BIN/roost-env.sh"

# == Homebrew on the PATH, whichever route the run came in by ==
# The collectors shell out to `gh`, which lives in /opt/homebrew/bin. A login
# shell has it; a non-interactive `ssh mini .../status.sh` does not.
#
# The loopback hop below used to be the only thing that put it there, and that
# hop is SKIPPED when SSH_CONNECTION is set - correctly, because an sshd-spawned
# process is already exempt from the gate the hop exists for. So a run driven
# over ssh from another machine ran without `gh`, every GitHub-backed collector
# took its "absent data leaves the board alone" path, and the run reported
# success over a board whose GitHub half had not moved:
#
#   ci-status: Phoenix-Electron did not answer - its tiles keep their last verdict
#   ci-health: no finished runs found (gh unavailable?) - leaving board as-is
#
# Nothing failed and nothing was marked stale. The tiles simply stopped
# tracking. The scheduled agent takes the hop and was never affected, which is
# why this only appeared in a hand-driven deploy.
# The location is overridable because it is not the same everywhere: Apple
# silicon puts it at /opt/homebrew/bin, an Intel Mac at /usr/local/bin. Only
# added when it exists, so a host without it is left exactly as it was.
ROOST_BREW_BIN="${ROOST_BREW_BIN:-/opt/homebrew/bin}"
case ":$PATH:" in
  *":$ROOST_BREW_BIN:"*) ;;
  # `|| true` is load-bearing under `set -e`: on a host with no such directory
  # the test is the branch's last command, and its non-zero status would end
  # the run before it started.
  *) [ -d "$ROOST_BREW_BIN" ] && { PATH="$ROOST_BREW_BIN:$PATH"; export PATH; } || true ;;
esac

# Say it once, loudly, rather than leaving it to a per-collector note in the
# middle of a long log. Without `gh` the run still succeeds and still deploys —
# every collector is non-fatal by contract — so the only thing distinguishing a
# healthy deploy from one that published a half-frozen board is this line.
command -v gh >/dev/null 2>&1 || \
  echo "⚠ gh is not on PATH — every GitHub-backed tile will keep its last value and the deploy will still report success" >&2

SITE="${ROOST_STATUS_SITE:-$HOME/status-site}"
SGEN="${ROOST_STATUSGEN:-$HOME/repos/statusgen}"
DOCS="${ROOST_DOCS:-$HOME/repos/docs}"

# == Local Network Privacy hop ==
# macOS gates a launchd agent's connections to the local subnet, and the grant flaps.
# On 2026-08-20 every scheduled run from 00:16 to past 09:00 got EHOSTUNREACH to dokku,
# while an interactive ssh session on the same box reached it 3 of 3 at the same time.
# sshd-spawned processes are exempt from the gate.
# So the run re-execs itself through ssh to localhost, and every LAN touch (the dokku push, the fleet collector) runs in the exempt context.
# The hop is non-fatal: with no self key or with Remote Login off, the run continues in place and only the LAN steps risk the gate.
if [ -z "${ROOST_LOOPBACK:-}" ] && [ -z "${SSH_CONNECTION:-}" ] && [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=5 localhost true 2>/dev/null; then
    echo "✓ loopback hop: re-running via ssh localhost (Local Network Privacy exemption)"
    exec ssh -o BatchMode=yes localhost "ROOST_LOOPBACK=1 PATH=/opt/homebrew/bin:\$PATH exec bash '$BIN/status.sh' $(printf '%q' "${1:-}")"
  fi
  echo "note: loopback hop unavailable (ssh localhost failed) - running in place"
fi
# The narrative is the status message, captured per-revision by the history
# collector. Left to a human it drifts stale while the auto-collected tiles stay
# fresh. So when no message is given, compose one from what actually merged —
# non-fatal, falls back to "update" on any failure.
if [ -n "${1:-}" ]; then
  MSG="$1"
elif [ -n "${ROOST_STATS_GH_REPO:-}" ]; then
  MSG="$("$BIN/gen-narrative.py" "$ROOST_STATS_GH_REPO" \
          --branch "${ROOST_STATS_CI_BRANCH:-dev}" \
          --since-days "${ROOST_NARRATIVE_SINCE_DAYS:-1}" \
          ${ROOST_STATS_LABEL:+--label "$ROOST_STATS_LABEL"} 2>/dev/null)"
  [ -n "$MSG" ] || MSG="update"
else
  MSG="update"
fi

[ -d "$SITE" ] || { echo "roost status: site not found at $SITE (set ROOST_STATUS_SITE)" >&2; exit 1; }
[ -d "$SGEN" ] || { echo "roost status: statusgen not found at $SGEN (set ROOST_STATUSGEN)" >&2; exit 1; }

# == Source Freshness Phase ==
# Ensure all input repos are current before collectors run. Nothing used to
# refresh these clones, so they could drift 1-2 commits stale, causing silent
# feature reverts (statusgen) or stale board data (collector sources).
# All steps are non-fatal to keep the pipeline flowing (boards regenerate
# next run if a pull fails). Dry-run mode bypasses fetches but notes what
# would have happened.

# Roost itself (the driver running this script). Site/statusgen/source clones
# refresh below, but nothing refreshed THIS repo, so driver-side changes sat
# stale on each machine until a hand pull (roost#23). Pull ff-only; if HEAD
# moved, re-exec the new script once — ROOST_SELF_UPDATED guards against a
# loop. Safe mid-run: git replaces files by rename, so the copy bash is
# reading stays intact until the exec.
ROOST_DIR="$(cd "$BIN/.." && pwd)"
if [ -n "${ROOST_STATUS_DRYRUN:-}" ]; then
  echo "note: [dry-run] roost would pull --ff-only (+ re-exec if updated)"
elif [ -z "${ROOST_SELF_UPDATED:-}" ] && [ -d "$ROOST_DIR/.git" ]; then
  before="$(git -C "$ROOST_DIR" rev-parse HEAD 2>/dev/null || true)"
  if git -C "$ROOST_DIR" pull --ff-only 2>/dev/null; then
    after="$(git -C "$ROOST_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [ -n "$before" ] && [ "$before" != "$after" ]; then
      echo "✓ roost: self-updated (${before:0:7} → ${after:0:7}) — re-running with the new driver"
      ROOST_SELF_UPDATED=1 exec "$BIN/status.sh" ${1+"$@"}
    fi
    echo "✓ roost: fresh"
  else
    echo "note: roost pull failed (dirty clone or diverged branch) — continuing with local version"
  fi
fi

# Site repo: abort any in-flight rebase/merge (derived data, so remote always wins).
# A previous run's rebase may have wedged the clone: conflict markers crash
# the collectors below and every later push silently no-ops (bitten twice).
if [ -d "$SITE/.git/rebase-merge" ] || [ -d "$SITE/.git/rebase-apply" ]; then
  if [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
    echo "note: $SITE was mid-rebase — resolving (derived data)"
    git -C "$SITE" rebase --abort >/dev/null 2>&1 || true
    git -C "$SITE" merge --abort >/dev/null 2>&1 || true
  else
    echo "note: [dry-run] $SITE would resolve mid-rebase"
  fi
fi

# Pull site from origin (GitHub, the canonical mirror — see the publish
# step below for why dokku is deliberately NOT trusted as a source). Every
# writer's push, hand-typed lede or scheduled refresh, lands on origin;
# regenerating from a checkout that's missing one means our force-push to
# dokku overwrites it outright. Bit the mini's scheduled refresh twice
# (2026-07-26, 2026-07-29): a hand lede deployed minutes earlier got
# steamrolled by a run that started from a stale local checkout. Falls back
# to dokku (LAN, usually reachable even when GitHub isn't) so a WAN blip
# doesn't stall the run — regenerating from *some* remote beats regenerating
# from whatever this clone happened to have sitting around.
if [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
  if git -C "$SITE" fetch -q origin 2>/dev/null && git -C "$SITE" rebase -q origin/main 2>/dev/null; then
    echo "✓ site: fresh (origin/main)"
  else
    git -C "$SITE" rebase --abort >/dev/null 2>&1 || true
    if git -C "$SITE" pull --rebase dokku main 2>/dev/null; then
      echo "note: site pull from origin failed (offline or conflict) — fresh via dokku/main instead"
    else
      echo "note: site pull failed from both origin and dokku (conflict?) — adopting dokku/main (boards regenerate next run)"
      git -C "$SITE" reset --hard dokku/main 2>/dev/null || echo "note: site reset failed (remote may be unreachable)"
    fi
  fi
else
  echo "note: [dry-run] site would pull --rebase origin main (falling back to dokku/main)"
fi

# Pull statusgen (renderer library).
if [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
  if git -C "$SGEN" pull --ff-only 2>/dev/null; then
    echo "✓ statusgen: fresh"
  else
    echo "note: statusgen pull failed (dirty clone or diverged branch) — continuing with local version"
  fi
else
  echo "note: [dry-run] statusgen would pull --ff-only"
fi

# Pull collector source repos. Optional colon-separated list of paths in
# ROOST_SOURCE_REPOS (e.g., ROOST_SOURCE_REPOS="/path/to/phoenix:/path/to/clauffice").
# Also pulls ROOST_STATS_REPO_DIR if set (usually a duplicate but deduplicated here).
if [ -n "${ROOST_SOURCE_REPOS:-}" ] || [ -n "${ROOST_STATS_REPO_DIR:-}" ]; then
  src_list="${ROOST_SOURCE_REPOS:-}"
  if [ -n "${ROOST_STATS_REPO_DIR:-}" ]; then
    # Add ROOST_STATS_REPO_DIR if not already in the list (simple string check).
    if [ -n "$src_list" ]; then
      src_list="$src_list:$ROOST_STATS_REPO_DIR"
    else
      src_list="$ROOST_STATS_REPO_DIR"
    fi
  fi

  # Pull each repo.
  IFS=:
  for src in $src_list; do
    IFS=" " # Reset IFS for the loop body.
    if [ -z "$src" ]; then continue; fi
    if [ -d "$src/.git" ]; then
      if [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
        if git -C "$src" pull --ff-only 2>/dev/null; then
          echo "✓ $src: fresh"
        else
          echo "note: $src pull failed — continuing with local version"
        fi
      else
        echo "note: [dry-run] $src would pull --ff-only"
      fi
    fi
  done
  unset IFS
fi

# 1. Collectors regenerate the generated boards (fleet, stat tiles, history).
"$BIN/fleet-board.py" "$SITE/fleet/board.json" || echo "note: fleet collection failed (non-fatal)"
"$BIN/roost" stats || echo "note: stat collectors failed (non-fatal)"
STATUS_SITE_DIR="$SITE" python3 "$SGEN/bin/collect/history.py" || echo "note: history collection failed (non-fatal)"

# 2. Keep the deployed renderer in lockstep with statusgen. Nothing used to do
#    this on deploy, so an edited renderer could silently never reach the site;
#    syncing here (with statusgen's content-hash versioning) closes that gap.
"$SGEN/bin/sync-renderer.sh" "$SITE" || echo "note: renderer sync failed (non-fatal)"

# 3. Gate: every board must satisfy the statusgen schema (top-level boards plus
#    the generated <slug>/history/ detail pages). Fatal on failure.
shopt -s nullglob
python3 "$SGEN/bin/validate-board.py" "$SITE"/*/board.json "$SITE"/*/*/board.json
shopt -u nullglob

# 4. Claude usage ledger + docs site (optional — skipped if docs isn't cloned).
if [ -x "$DOCS/bin/usage-report.py" ]; then
  "$DOCS/bin/usage-report.py" || echo "note: usage report failed (non-fatal)"
else
  echo "note: usage report skipped ($DOCS/bin/usage-report.py absent)"
fi

# Dry-run stops before git — exercise the whole pipeline without committing or
# deploying the live site:  ROOST_STATUS_DRYRUN=1 roost status
if [ -n "${ROOST_STATUS_DRYRUN:-}" ]; then
  echo "✓ dry-run: boards regenerated + validated, no commit/deploy"
  exit 0
fi

# 5. Commit and deploy.
cd "$SITE"
git add -A
git commit -q -m "status: ${MSG} ($(date +%F))" || echo "nothing new to commit"
# Two machines push this repo (MacBook + mini's hourly refresh). Rebase on the
# GitHub mirror first so whoever fell behind can't silently diverge and later
# clobber the site with stale boards. Both machines regenerate the same
# board.json files, so rebase conflicts are routine — and never deserve a
# wedge: everything here is derived, so on conflict adopt the mirror and let
# the next run regenerate on top of it.
if git fetch -q origin 2>/dev/null; then
  if ! git rebase -q origin/main; then
    git rebase --abort >/dev/null 2>&1 || true
    git reset -q --hard origin/main
    # The dropped commit was derived boards + the status message, so the cure
    # is regenerate-on-top-of-whoever-won, not merge: adopt origin/main and
    # re-run the whole pipeline once. Losing a second race in the same run
    # means the window is hot — then fail LOUDLY instead of printing
    # "deployed" over a discarded commit (roost#27).
    if [ -z "${ROOST_STATUS_RETRIED:-}" ]; then
      echo "note: rebase conflict (concurrent writer) — adopted origin/main, regenerating on top of it"
      ROOST_STATUS_RETRIED=1 ROOST_SELF_UPDATED=1 exec "$BIN/status.sh" ${1+"$@"}
    fi
    echo "✗ status NOT posted: rebase conflict again after retry (two-writer race) — run roost status again" >&2
    exit 1
  fi
else
  # Can't confirm this commit sits on top of the canonical mirror, so we
  # can't trust that force-pushing dokku won't steamroll a writer whose
  # push we never saw — the exact failure that bit the mini's scheduled
  # refresh twice (2026-07-26, 2026-07-29): "mirror fetch failed (non-fatal)
  # — pushing local state" used to mean push-anyway. A missed cycle is
  # recoverable; an overwritten hand lede isn't, so fail loudly instead of
  # guessing (same idiom as the rebase-conflict retry above, roost#27).
  echo "✗ status NOT posted: mirror fetch failed (offline?) — refusing to push over an unverified base; run roost status again" >&2
  exit 1
fi
# Dokku is a deploy SINK, not a source: nothing ever merges back from it and
# the GitHub mirror is canonical. When an hourly run's mirror push loses a
# race (e.g. with a PR squash-merge), dokku ends up with a commit GitHub
# never saw and a plain push is rejected non-fast-forward forever after —
# killing the whole run under set -e. Forcing is correct here: the tree we
# push is always canonical-mirror + freshly regenerated boards.
# The push is the last step of a run that took minutes, and losing one ssh race
# threw all of it away: the collectors had already produced a fresh board, and the
# run died here under set -e with the site still serving the previous one. On
# 2026-08-18 that left the board reporting a four-day-old "last green" while two
# green runs sat in the history underneath it. A blip is worth retrying rather
# than discarding a whole cycle for, so the attempts back off and only then give up.
push_to_dokku() {
  local attempt=1 delay=5
  until git push --force dokku main; do
    if [ "$attempt" -ge 4 ]; then
      echo "✗ deploy push failed 4 times — the site keeps the board it last published" >&2
      return 1
    fi
    echo "note: deploy push failed (attempt $attempt), retrying in ${delay}s" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}
# The deploy and the mirror fail independently, so one must not take the other
# down. `push_to_dokku` reaches the opi across the LAN, and that push has been
# seen to fail with "No route to host" while the host answers ping and accepts
# ssh seconds later. Under `set -e` a failure here used to end the run before
# the mirror push, so a flaky LAN moment stranded every board on this machine:
# 21 commits and six hours of them on 2026-08-18, with the site still serving
# what it last published.
#
# The mirror is the durable copy and goes over the WAN, so it runs either way.
# A failed deploy is still a failed run, and the next one retries it.
deployed=1
push_to_dokku || deployed=0

git push origin main 2>/dev/null || echo "note: GitHub mirror push failed (non-fatal)"

if [ "$deployed" -eq 1 ]; then
  echo "✓ status deployed — https://status.${ROOST_DOMAIN}/"
else
  echo "✗ status NOT deployed, but the boards are committed and mirrored — the next run retries the deploy" >&2
  exit 1
fi
