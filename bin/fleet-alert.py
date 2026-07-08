#!/usr/bin/env python3
"""Fleet watchdog: check the roost every 15 min, notify on state changes.

Reuses fleet-board.py's collection (runs it to a temp file), compares
against the previous run (~/.roost-fleet-state.json), and sends a macOS
notification only on TRANSITIONS — app went down, app recovered, disk or
memory crossed 85%. Silence means healthy (or unchanged-broken).
"""
import json, os, subprocess, sys, tempfile

BIN = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.roost-fleet-state.json")
WARN_PCT = 85

def notify(title, msg):
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "{title}" sound name "Basso"'],
                   capture_output=True)
    print(f"ALERT: {title} — {msg}")

def main():
    tmp = os.path.join(tempfile.gettempdir(), "roost-fleet-check.json")
    r = subprocess.run([os.path.join(BIN, "fleet-board.py"), tmp],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        # collection itself failing is an alertable transition
        prev_ok = True
        try:
            prev_ok = json.load(open(STATE)).get("collect_ok", True)
        except OSError:
            pass
        if prev_ok:
            notify("Roost watchdog", "fleet collection failed — pi unreachable?")
        json.dump({"collect_ok": False}, open(STATE, "w"))
        sys.exit(1)

    board = json.load(open(tmp))
    tiles = {t["label"]: t["n"] for t in board["sections"][0]["items"]}
    apps = {i["id"]: i["pill"]["text"] for i in board["sections"][1]["items"]}
    cur = {"collect_ok": True, "apps": apps,
           "mem": tiles.get("Host memory used", "?"),
           "disk": tiles.get("Root disk used", "?")}

    prev = {}
    try:
        prev = json.load(open(STATE))
    except (OSError, json.JSONDecodeError):
        pass
    prev_apps = prev.get("apps", {})

    for app, state in apps.items():
        was = prev_apps.get(app, "up")
        if state != "up" and was == "up":
            notify("Roost: app trouble", f"{app} is {state}")
        elif state == "up" and was != "up" and app in prev_apps:
            notify("Roost: recovered", f"{app} is back up")

    for label, key in [("memory", "mem"), ("disk", "disk")]:
        try:
            now_pct = int(cur[key].rstrip("%"))
            was_pct = int(str(prev.get(key, "0")).rstrip("%"))
            if now_pct >= WARN_PCT > was_pct:
                notify("Roost: host pressure", f"{label} at {now_pct}%")
        except ValueError:
            pass

    if not prev.get("collect_ok", True):
        notify("Roost: recovered", "fleet collection working again")

    json.dump(cur, open(STATE, "w"))
    print(f"ok: {sum(1 for s in apps.values() if s == 'up')}/{len(apps)} up, "
          f"mem {cur['mem']}, disk {cur['disk']}")

if __name__ == "__main__":
    main()
