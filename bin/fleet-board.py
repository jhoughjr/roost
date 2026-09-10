#!/usr/bin/env python3
"""Roost fleet health → statusgen board.json.

Reads what the box already reported to pulse, and emits a statusgen board.

This used to collect over the dokku@ SSH channel: four round trips per app for
running, the names, the container age and the memory inside the container, plus
one `dokku run` for the host figures. Twenty-four apps is about a hundred round
trips from a Mac, for facts the box reads about every container at once and
posts to pulse every ten minutes anyway. The reconcile carries them now, so this
asks pulse instead and the ssh channel is not needed to draw the board.

A pulse that does not answer is a failure rather than a board of guesses. The
previous board.json stays where it is, and the collector record says this one
did not run, which is what the Collectors section on the clauffice board draws.

Usage: fleet-board.py [output-path]   (default: ~/status-site/fleet/board.json)
"""
import concurrent.futures, json, os, subprocess, sys, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roostlib  # noqa: E402

_RC = roostlib.read_rc()
DOMAIN = roostlib.rc("ROOST_DOMAIN")
PULSE = _RC.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").rstrip("/")
# Only for the probe below. Nothing here opens an ssh channel any more.
HOST_IP = roostlib.rc("ROOST_DOKKU_HOST").split("@")[-1]
# Whose figures are the host figures. The box that runs the apps is the one whose memory and disk this board is about.
FLEET_NODE = _RC.get("ROOST_FLEET_NODE", "opi")
STATUS_SITE = os.path.expanduser(_RC.get("ROOST_STATUS_SITE", "~/status-site"))
EXPECTED = {}
for pair in _RC.get("ROOST_EXPECTED_HTTP", "").split(","):
    if ":" in pair:
        app, code = pair.split(":", 1)
        EXPECTED[app.strip()] = code.strip()

def fetch(path):
    """One document from pulse. A failure is raised, because a board drawn from half an answer is worse than yesterday's board."""
    request = urllib.request.Request(PULSE + path, headers={"User-Agent": "roost-fleet-board"})
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.loads(answer.read())


def http_check(fqdn):
    """The code nginx gives for a name on port 80, asked at the box.

    The reading from the box carries a code too, and it is not this one: the reconcile asks 443 first and falls back to
    80, so an app that redirects to https answers 200 there and 301 here. `ROOST_EXPECTED_HTTP` is written against this
    one, and swapping the source would call forgejo and vault degraded while both serve correctly.

    It stays a probe from wherever this runs because it was never the expensive part. The hundred ssh round trips were.
    """
    try:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "-m", "8", "-H", f"Host: {fqdn}", f"http://{HOST_IP}/"],
                           capture_output=True, text=True, timeout=12)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "000"


def is_app(row):
    """Whether an answered row is a dokku app rather than a container, a job or a database.

    A job and a database name their kind. A container the box runs outside dokku owns no vhost, so its names are empty,
    and an app always has at least one.
    """
    return "kind" not in row and bool(row.get("names"))


def collect_app(row):
    """One app's row, from what the box already said about it.

    A redirect is a healthy answer, not a fault. The probe behind this reaches nginx over plain http at the box, so every
    app that sends callers to https answers 301 or 302, and comparing that against a flat "200" called forgejo, vault,
    rookery and rulings degraded while all four were serving correctly. An estate that reads as permanently half broken
    is one nobody looks at, which is the same fatigue as an alert flood.

    A 4xx still counts as a fault unless ROOST_EXPECTED_HTTP names it, because a 404 can equally mean an app with no
    root route or an app whose routes are wrong, and this cannot tell those apart. Naming the ones that are known good
    is the job of a person.
    """
    app = row["name"]
    names = row.get("names") or []
    fqdn = next((d for d in names if d.endswith(DOMAIN)), names[0] if names else f"{app}.{DOMAIN}")
    running = bool(row.get("running"))
    code = http_check(fqdn) if row.get("image", True) else "—"
    expected = EXPECTED.get(app)
    if expected:
        healthy = running and code == expected
    else:
        healthy = running and code.isdigit() and 200 <= int(code) < 400
    http_bit = f"http {code}"
    if expected != "200" and code == expected:
        http_bit += " (expected)"
    mem_mb = f"{row['memMb']:.0f} MB" if isinstance(row.get("memMb"), (int, float)) else ""
    note_bits = [http_bit, f"{row.get('procs', 0)} proc"]
    if mem_mb:
        note_bits.insert(1, mem_mb)
    if row.get("createdAt"):
        note_bits.append(f"container since {row['createdAt']}")
    board_row = {
        "id": app,
        "q": fqdn,
        "href": f"https://{fqdn}/",
        "note": " · ".join(note_bits),
        "pill": {"text": "up" if healthy else ("degraded" if running else "down"),
                 "tone": "go" if healthy else "srv"},
    }
    return board_row, running, healthy, float(row.get("memMb") or 0.0)


def host_metrics(nodes):
    """(mem%, disk%, load) for the box the apps run on, from the reading node-report already posts."""
    node = next((n for n in nodes if n.get("name") == FLEET_NODE), None)
    if not node:
        return "?", "?", "?"

    def percent(used, total):
        try:
            return f"{int(used) * 100 // int(total)}%" if int(total) else "?"
        except (TypeError, ValueError):
            return "?"

    load = node.get("load1")
    return (percent(node.get("memUsedMb"), node.get("memTotalMb")),
            percent(node.get("diskUsedMb"), node.get("diskTotalMb")),
            f"{load:.2f}" if isinstance(load, (int, float)) else "?")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(STATUS_SITE, "fleet/board.json")
    answered = [row for row in (fetch("/api/answers").get("apps") or []) if is_app(row)]
    apps = sorted(answered, key=lambda row: row["name"])
    mem_pct, disk_pct, load = host_metrics(fetch("/api/stats").get("nodes") or [])

    # Six workers, as before: enough to collapse the wall time of twenty-four probes, few enough to be polite.
    rows, up, ok, fleet_mb = [], 0, 0, 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        collected = list(pool.map(collect_app, apps))
    for row, running, http_ok, mb in collected:
        rows.append(row)
        if running: up += 1
        if http_ok: ok += 1
        fleet_mb += mb

    def tone_pct(v, warn):
        try:
            return "srv" if int(v.rstrip("%")) >= warn else "go"
        except ValueError:
            return "wip"

    board = {
        "title": "Fleet Health",
        "eyebrow": "roost · as the box reported it",
        # No baked timestamp: the renderer shows a "Generated <local time>" stamp
        # from the board.json's HTTP Last-Modified, in the viewer's timezone.
        "stamp": "Read from pulse by roost/bin/fleet-board.py, with the http code probed here; "
                 "refreshed on every roost status.",
        "sections": [
            {"kind": "stats", "items": [
                {"n": f"{up}/{len(apps)}", "label": "Containers running",
                 "tone": "go" if up == len(apps) else "srv"},
                {"n": f"{ok}/{len(apps)}", "label": "Serving expected HTTP",
                 "tone": "go" if ok == len(apps) else "srv"},
                {"n": f"{fleet_mb:.0f} MB", "label": "Apps memory (sum)", "tone": "done"},
                {"n": mem_pct, "label": "Host memory used", "tone": tone_pct(mem_pct, 85)},
                {"n": disk_pct, "label": "Root disk used", "tone": tone_pct(disk_pct, 85)},
                {"n": load, "label": "Load average (1m)", "tone": "done"},
            ]},
            {"kind": "cards", "title": "Apps", "count": f"{len(apps)} deployed", "items": rows},
        ],
    }
    # Roost TODOs: render "- item -- detail" lines from TODO.md (repo root) as a
    # board section, so operational reminders live in git and survive regeneration.
    todo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "TODO.md")
    if os.path.exists(todo_path):
        items = []
        for line in open(todo_path):
            line = line.strip()
            if line.startswith("- "):
                text = line[2:].strip()
                q, _, note = text.partition(" -- ")
                items.append({"q": q.strip(), "note": note.strip()})
        if items:
            board["sections"].append({
                "kind": "cards", "title": "Roost TODOs",
                "count": f"{len(items)} open",
                "desc": "from roost/TODO.md — delete lines there when done",
                "items": items,
            })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(board, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"fleet: {up}/{len(apps)} running, {ok}/{len(apps)} http-ok, mem {mem_pct}, disk {disk_pct}, load {load}")

if __name__ == "__main__":
    main()
