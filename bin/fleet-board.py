#!/usr/bin/env python3
"""Roost fleet health → statusgen board.json.

Gathers live platform state over the dokku@ SSH channel (works from any
workstation — nothing runs on the host beyond dokku commands) and checks
each app's HTTP reachability through nginx. Emits a statusgen board.

Usage: fleet-board.py [output-path]   (default: ~/status-site/fleet/board.json)
"""
import json, os, re, subprocess, sys, datetime

DOKKU = "dokku@192.168.0.103"
HOST_IP = "192.168.0.103"
DOMAIN = "jimmyhoughjr.net"
METRIC_APP = "vault"  # any always-on app; used to read host metrics via `run`

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

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/status-site/fleet/board.json")
    apps = [a.strip() for a in ssh("apps:list").splitlines()
            if a.strip() and not a.startswith("=")]

    rows, up, ok, fleet_mb = [], 0, 0, 0.0
    for app in apps:
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
        healthy = running and code == "200"
        if running: up += 1
        if code == "200": ok += 1
        if mem_mb: fleet_mb += float(mem_mb.split()[0])
        note_bits = [f"http {code}", f"{procs} proc"]
        if mem_mb: note_bits.insert(1, mem_mb)
        if created: note_bits.append(f"container since {created}")
        rows.append({
            "id": app,
            "q": fqdn,
            "href": f"https://{fqdn}/",
            "note": " · ".join(note_bits),
            "pill": {"text": "up" if healthy else ("degraded" if running else "down"),
                     "tone": "go" if healthy else "srv"},
        })

    # host metrics via a container (shares the host kernel's view)
    mem_pct = disk_pct = load = "?"
    try:
        free = ssh("run", METRIC_APP, "free", "-m", timeout=60)
        m = re.search(r"^Mem:\s+(\d+)\s+(\d+)", free, re.M)
        if m:
            mem_pct = f"{int(m.group(2)) * 100 // int(m.group(1))}%"
        df = ssh("run", METRIC_APP, "df", "-h", "/", timeout=60)
        m = re.search(r"(\d+)%", df)
        if m:
            disk_pct = f"{m.group(1)}%"
        upt = ssh("run", METRIC_APP, "uptime", timeout=60)
        m = re.search(r"load average[s]?:\s*([\d.]+)", upt)
        if m:
            load = m.group(1)
    except subprocess.TimeoutExpired:
        pass

    def tone_pct(v, warn):
        try:
            return "srv" if int(v.rstrip("%")) >= warn else "go"
        except ValueError:
            return "wip"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    board = {
        "title": "Fleet Health",
        "eyebrow": "roost · live from dokku",
        "stamp": f"Updated {now} — collected over the dokku@ channel by roost/bin/fleet-board.py; "
                 "refreshed on every push-status.",
        "sections": [
            {"kind": "stats", "items": [
                {"n": f"{up}/{len(apps)}", "label": "Containers running",
                 "tone": "go" if up == len(apps) else "srv"},
                {"n": f"{ok}/{len(apps)}", "label": "Serving HTTP 200",
                 "tone": "go" if ok == len(apps) else "srv"},
                {"n": f"{fleet_mb:.0f} MB", "label": "Apps memory (sum)", "tone": "done"},
                {"n": mem_pct, "label": "Host memory used", "tone": tone_pct(mem_pct, 85)},
                {"n": disk_pct, "label": "Root disk used", "tone": tone_pct(disk_pct, 85)},
                {"n": load, "label": "Load average (1m)", "tone": "done"},
            ]},
            {"kind": "cards", "title": "Apps", "count": f"{len(apps)} deployed", "items": rows},
        ],
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(board, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"fleet: {up}/{len(apps)} running, {ok}/{len(apps)} http-ok, mem {mem_pct}, disk {disk_pct}, load {load}")

if __name__ == "__main__":
    main()
