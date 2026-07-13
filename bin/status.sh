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
# clobber the site with stale boards.
git pull --rebase origin main 2>/dev/null || echo "note: mirror pull failed (non-fatal) — pushing local state"
git push dokku main
git push origin main 2>/dev/null || echo "note: GitHub mirror push failed (non-fatal)"
echo "✓ status deployed — https://status.jimmyhoughjr.net/"
