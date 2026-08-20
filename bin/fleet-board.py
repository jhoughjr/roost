#!/usr/bin/env python3
"""Roost fleet health → statusgen board.json.

Gathers live platform state over the dokku@ SSH channel (works from any
workstation — nothing runs on the host beyond dokku commands) and checks
each app's HTTP reachability through nginx. Emits a statusgen board.

Usage: fleet-board.py [output-path]   (default: ~/status-site/fleet/board.json)
"""
import concurrent.futures, json, os, re, subprocess, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roostlib  # noqa: E402

_RC = roostlib.read_rc()
DOKKU = roostlib.rc("ROOST_DOKKU_HOST")
HOST_IP = DOKKU.split("@")[-1]
DOMAIN = roostlib.rc("ROOST_DOMAIN")
METRIC_APP = _RC.get("ROOST_METRIC_APP", "vault")  # host metrics via `run`
STATUS_SITE = os.path.expanduser(_RC.get("ROOST_STATUS_SITE", "~/status-site"))
EXPECTED = {}
for pair in _RC.get("ROOST_EXPECTED_HTTP", "").split(","):
    if ":" in pair:
        app, code = pair.split(":", 1)
        EXPECTED[app.strip()] = code.strip()

def ssh(*args, timeout=30):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", DOKKU, *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout

def report_field(text, field):
    m = re.search(rf"^\s*{re.escape(field)}:\s*(.+?)\s*$", text, re.M)
    return m.group(1) if m else ""

def http_check(fqdn):
    try:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "-m", "8", "-H", f"Host: {fqdn}", f"http://{HOST_IP}/"],
                           capture_output=True, text=True, timeout=12)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "000"

def collect_app(app):
    """One app's row, over its own ssh calls, so the fleet collects in
    parallel. 21 apps at 4-5 serial round-trips each cost the pipeline 140
    seconds per push (measured 2026-08-20); the wall time is now the slowest
    single app."""
    rep = ssh("ps:report", app)
    running = report_field(rep, "Running") == "true"
    deployed = report_field(rep, "Deployed") == "true"
    procs = report_field(rep, "Processes") or "0"
    domains = ssh("domains:report", app, "--domains-app-vhosts").split()
    fqdn = next((d for d in domains if d.endswith(DOMAIN)), domains[0] if domains else f"{app}.{DOMAIN}")
    code = http_check(fqdn) if deployed else "—"
    created = ""
    try:
        insp = json.loads(ssh("ps:inspect", app))
        created = insp[0].get("Created", "")[:10]
    except (json.JSONDecodeError, IndexError, ValueError):
        pass
    mem_mb = ""
    if running:
        # sum process RSS inside the container (cgroup files reflect the
        # exec scope, not the app — learned the hard way)
        rss_kb = sum(int(x) for x in ssh("enter", app, "web", "ps", "-o", "rss=").split() if x.isdigit())
        if rss_kb:
            mem_mb = f"{rss_kb / 1024:.0f} MB"
    expected = EXPECTED.get(app, "200")
    healthy = running and code == expected
    http_bit = f"http {code}"
    if expected != "200" and code == expected:
        http_bit += " (expected)"
    note_bits = [http_bit, f"{procs} proc"]
    if mem_mb: note_bits.insert(1, mem_mb)
    if created: note_bits.append(f"container since {created}")
    row = {
        "id": app,
        "q": fqdn,
        "href": f"https://{fqdn}/",
        "note": " · ".join(note_bits),
        "pill": {"text": "up" if healthy else ("degraded" if running else "down"),
                 "tone": "go" if healthy else "srv"},
    }
    return row, running, code == expected, float(mem_mb.split()[0]) if mem_mb else 0.0


def host_metrics():
    """(mem%, disk%, load) via ONE container. This was three `dokku run`
    calls, and every `dokku run` boots a fresh container - three boots per
    push to read three numbers."""
    mem_pct = disk_pct = load = "?"
    try:
        combined = ssh("run", METRIC_APP, "sh", "-c",
                       "'free -m; df -h /; uptime'", timeout=60)
        m = re.search(r"^Mem:\s+(\d+)\s+(\d+)", combined, re.M)
        if m:
            mem_pct = f"{int(m.group(2)) * 100 // int(m.group(1))}%"
        m = re.search(r"(\d+)%", combined)
        if m:
            disk_pct = f"{m.group(1)}%"
        m = re.search(r"load average[s]?:\s*([\d.]+)", combined)
        if m:
            load = m.group(1)
    except subprocess.TimeoutExpired:
        pass
    return mem_pct, disk_pct, load


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(STATUS_SITE, "fleet/board.json")
    apps = [a.strip() for a in ssh("apps:list").splitlines()
            if a.strip() and not a.startswith("=")]

    # 6 workers: enough to collapse the wall time, few enough to stay clear of
    # sshd's connection throttle on the host.
    rows, up, ok, fleet_mb = [], 0, 0, 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        metrics = pool.submit(host_metrics)
        for row, running, http_ok, mb in pool.map(collect_app, apps):
            rows.append(row)
            if running: up += 1
            if http_ok: ok += 1
            fleet_mb += mb
        mem_pct, disk_pct, load = metrics.result()

    def tone_pct(v, warn):
        try:
            return "srv" if int(v.rstrip("%")) >= warn else "go"
        except ValueError:
            return "wip"

    board = {
        "title": "Fleet Health",
        "eyebrow": "roost · live from dokku",
        # No baked timestamp: the renderer shows a "Generated <local time>" stamp
        # from the board.json's HTTP Last-Modified, in the viewer's timezone.
        "stamp": "Collected over the dokku@ channel by roost/bin/fleet-board.py; "
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
