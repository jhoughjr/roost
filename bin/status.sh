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
[ -f "$HOME/.roostrc" ] && . "$HOME/.roostrc"
SITE="${ROOST_STATUS_SITE:-$HOME/status-site}"
SGEN="${ROOST_STATUSGEN:-$HOME/repos/statusgen}"
DOCS="${ROOST_DOCS:-$HOME/repos/docs}"
MSG="${1:-update}"

[ -d "$SITE" ] || { echo "roost status: site not found at $SITE (set ROOST_STATUS_SITE)" >&2; exit 1; }
[ -d "$SGEN" ] || { echo "roost status: statusgen not found at $SGEN (set ROOST_STATUSGEN)" >&2; exit 1; }

# == Source Freshness Phase ==
# Ensure all input repos are current before collectors run. Nothing used to
# refresh these clones, so they could drift 1-2 commits stale, causing silent
# feature reverts (statusgen) or stale board data (collector sources).
# All steps are non-fatal to keep the pipeline flowing (boards regenerate
# next run if a pull fails). Dry-run mode bypasses fetches but notes what
# would have happened.

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

# Pull site from dokku (primary remote for this machine).
if [ -z "${ROOST_STATUS_DRYRUN:-}" ]; then
  if git -C "$SITE" pull --rebase dokku main 2>/dev/null; then
    echo "✓ site: fresh (dokku/main)"
  else
    echo "note: site pull failed (conflict?) — adopting dokku/main (boards regenerate next run)"
    git -C "$SITE" reset --hard dokku/main 2>/dev/null || echo "note: site reset failed (remote may be unreachable)"
  fi
else
  echo "note: [dry-run] site would pull --rebase dokku main"
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
    echo "note: rebase conflict — adopting origin/main (boards regenerate next run)"
    git rebase --abort >/dev/null 2>&1 || true
    git reset -q --hard origin/main
  fi
else
  echo "note: mirror fetch failed (non-fatal) — pushing local state"
fi
git push dokku main
git push origin main 2>/dev/null || echo "note: GitHub mirror push failed (non-fatal)"
echo "✓ status deployed — https://status.jimmyhoughjr.net/"
