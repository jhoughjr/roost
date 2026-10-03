#!/bin/bash
# worktree-build-sweep.sh - remove the build products of finished git worktrees, so a Mac's disk does not fill.
#
# A Swift worktree keeps about 1 GB in .build after its branch merges. The air ran out of disk on 2026-10-01 and
# again on 2026-10-03 from these. A build tree is removed only when all of these are true:
#   - the worktree's HEAD is merged into its repository's main branch
#   - the worktree has no uncommitted file
#   - nothing touched the build tree in the last day
#   - no swift process runs in it
# A source tree is never removed. A worktree that fails a rule is named with the rule, and kept.
#
#   worktree-build-sweep.sh            sweep, and print one line for each tree removed or kept
#   worktree-build-sweep.sh --dry-run  print what a sweep would remove, and remove nothing
#
# ROOST_SWEEP_ROOT names the directory of repositories. The default is ~/repos.
set -u
shopt -s nullglob
dry=0; [ "${1:-}" = "--dry-run" ] && dry=1
root="${ROOST_SWEEP_ROOT:-$HOME/repos}"

free() { df -k / | awk 'NR==2 {printf "%.1f GiB", $4/1048576}'; }

# The main branch a worktree is measured against: the forge's, then origin's, then the local one.
main_of() {
  local ref
  for ref in forge/main origin/main origin/master main master; do
    git -C "$1" rev-parse --verify -q "$ref" >/dev/null 2>&1 && { echo "$ref"; return 0; }
  done
  return 1
}

echo "$(date '+%F %T') sweep of $root starts with $(free) free$([ $dry = 1 ] && echo ', dry run')"
removed=0; kept=0
for build in "$root"/*/.worktrees/*/.build "$root"/*/.claude/worktrees/*/.build; do
  tree=$(dirname "$build"); name=${tree#"$root"/}
  main=$(main_of "$tree") || { echo "kept $name: no main branch found"; kept=$((kept+1)); continue; }
  git -C "$tree" merge-base --is-ancestor HEAD "$main" 2>/dev/null || { echo "kept $name: not merged into $main"; kept=$((kept+1)); continue; }
  [ -z "$(git -C "$tree" status --short 2>/dev/null)" ] || { echo "kept $name: uncommitted files"; kept=$((kept+1)); continue; }
  [ -z "$(find "$build" -maxdepth 2 -mtime -1 -print -quit 2>/dev/null)" ] || { echo "kept $name: touched in the last day"; kept=$((kept+1)); continue; }
  pgrep -f "swift.*$tree" >/dev/null 2>&1 && { echo "kept $name: a swift process runs in it"; kept=$((kept+1)); continue; }
  size=$(du -sh "$build" 2>/dev/null | cut -f1)
  if [ $dry = 1 ]; then echo "would remove $name/.build ($size)"; else rm -rf "$build" && echo "removed $name/.build ($size)"; fi
  removed=$((removed+1))
done
echo "$(date '+%F %T') sweep ends: $removed $([ $dry = 1 ] && echo 'to remove' || echo 'removed'), $kept kept, $(free) free"
