# roost-collector.sh — run a board collector and record whether it ran.
#
# Every collector is non-fatal by contract: one that breaks must not stop the nineteen behind it. That is why each
# invocation used to end in `|| echo "note: ... failed"`. What that shape could not do is say afterwards which of twenty
# ran and which did not, so a collector broken for a week looked exactly like one that had nothing to say, and the board
# went on showing its stale tile.
#
# This runs the collector, times it, prints the same note on failure, and keeps the outcome. `collector_report` writes
# the record out. The record merges by name rather than replacing the file, because `roost stats` and `status.sh` each
# run their own collectors and neither of them is the whole list.
COLLECTOR_LOG="${ROOST_COLLECTOR_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/roost-collectors.json}"
# One record per line, as `name|ok|ms|at|exit`, because bash has no other way to carry a list of records.
COLLECTOR_RUNS=""

collector_now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

# collector <name> <command...>
collector() {
  local name="$1"; shift
  local started ended code
  started=$(collector_now_ms)
  # The failure is the thing being recorded, so it must not end the run under `set -e`.
  set +e
  "$@"
  code=$?
  set -e
  ended=$(collector_now_ms)
  if [ "$code" -ne 0 ]; then
    echo "note: $name failed (non-fatal)"
  fi
  COLLECTOR_RUNS="$COLLECTOR_RUNS$name|$code|$((ended - started))|$ended
"
}

# Write what this pass learned, merged into what the other pass learned.
collector_report() {
  [ -n "$COLLECTOR_RUNS" ] || return 0
  install -d "$(dirname "$COLLECTOR_LOG")" 2>/dev/null || true
  COLLECTOR_LOG="$COLLECTOR_LOG" COLLECTOR_RUNS="$COLLECTOR_RUNS" python3 -c '
import json, os, time

path = os.environ["COLLECTOR_LOG"]
try:
    with open(path) as fh:
        record = json.load(fh)
except (OSError, ValueError):
    record = {}
# A run this pass did not make is kept, because the other pass made it and its collector is no less real.
runs = {r["name"]: r for r in record.get("runs", []) if isinstance(r, dict) and r.get("name")}
for line in os.environ["COLLECTOR_RUNS"].splitlines():
    parts = line.split("|")
    if len(parts) != 4:
        continue
    name, code, ms, at = parts
    run = {"name": name, "ok": code == "0", "ms": int(ms), "at": int(at)}
    if code != "0":
        run["exit"] = int(code)
    runs[name] = run
record = {"at": int(time.time() * 1000), "runs": sorted(runs.values(), key=lambda r: r["name"])}
tmp = path + ".new"
with open(tmp, "w") as fh:
    json.dump(record, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
' || echo "note: the collector record was not written (non-fatal)"
}
