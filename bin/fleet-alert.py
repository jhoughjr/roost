#!/usr/bin/env python3
"""Fleet watchdog: check the roost every 15 min, notify on state changes.

Reuses fleet-board.py's collection (runs it to a temp file), compares
against the previous run (~/.roost-fleet-state.json), and sends a macOS
notification only on TRANSITIONS — app went down, app recovered, disk or
memory crossed 85%. Silence means healthy (or unchanged-broken).
"""
import json, os, subprocess, sys, tempfile, urllib.request

BIN = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.roost-fleet-state.json")
# More apps than this changing in one pass is one estate-wide event, not many app events.
COLLAPSE_AT = 3
WARN_PCT = 85

sys.path.insert(0, BIN)
import roostlib  # noqa: E402

load_config = roostlib.read_rc

def notify(title, msg):
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "{title}" sound name "Basso"'],
                   capture_output=True)
    print(f"ALERT: {title} — {msg}")

    # Also POST to ntfy.sh if configured
    config = load_config()
    ntfy_topic = config.get("ROOST_NTFY_TOPIC")
    if ntfy_topic:
        try:
            url = f"https://ntfy.sh/{ntfy_topic}"
            # Determine priority and tags based on message content
            priority = "high" if ("DOWN" in msg.upper() or "trouble" in msg.lower() or "pressure" in msg.lower()) else "default"
            tags = "rotating_light" if "DOWN" in msg.upper() or "trouble" in msg.lower() or "pressure" in msg.lower() else "white_check_mark"

            body = msg.encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Title", title)
            req.add_header("Priority", priority)
            req.add_header("Tags", tags)

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status != 200:
                    print(f"ntfy warning: POST returned {response.status}")
        except Exception as e:
            # Don't break the watchdog if ntfy fails
            print(f"ntfy error (ignored): {e}")

    # The phone tells a person now, and forgets in about twelve hours.
    # pulse keeps the same event for two years, which is what answers "what broke while I was away".
    # Both, because neither alone is the whole job: one interrupts, the other remembers.
    record_event(config, title, msg)


def record_event(config, title, msg, kind="alert", subject=None):
    """Write one event to pulse's durable record.

    Never raises and never blocks the alert: a watchdog that dies because the
    recorder is unreachable is worse than one that forgets.
    """
    try:
        key_file = os.path.expanduser("~/.roost_node_key")
        with open(key_file) as fh:
            key = fh.read().strip()
        if not key:
            return
        pulse = config.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").rstrip("/")
        # The words can sit in either half: an estate-wide alert carries the fault in its
        # title and the app names in its message, and a single app is the other way round.
        # Reading only one half tagged the worst event on the board as a warning.
        both = f"{title} {msg}".lower()
        if "recovered" in both or "back up" in both or "back online" in both:
            tone = "go"
        elif any(w in both for w in ("trouble", "degraded", "down", "pressure", "failed", "unreachable")):
            tone = "bad"
        else:
            tone = "warn"
        body = json.dumps({
            "kind": kind,
            "source": "roost",
            "subject": subject,
            "tone": tone,
            "message": f"{title}: {msg}" if not msg.startswith(title) else msg,
        }).encode("utf-8")
        req = urllib.request.Request(f"{pulse}/api/events", data=body, method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("x-roost-node-key", key)
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status != 200:
                print(f"pulse warning: /api/events returned {response.status}")
    except Exception as e:
        print(f"pulse event not recorded (ignored): {e}")

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

    # One notification per app is right for one app, and wrong for the estate.
    # On 2026-09-07 a single wedged proxy flipped twenty apps in one pass and sent
    # forty-five notifications for one cause, which is a flood nobody can act on.
    # A change that touches many apps at once is one event and is reported as one.
    trouble = []
    recovered = []
    for app, state in apps.items():
        was = prev_apps.get(app, "up")
        if state != "up" and was == "up":
            trouble.append((app, state))
        elif state == "up" and was != "up" and app in prev_apps:
            recovered.append(app)

    # The apps are still named, because "the estate is degraded" alone sends a person
    # to the box with nothing to look at first.
    if len(trouble) > COLLAPSE_AT:
        names = ", ".join(app for app, _ in sorted(trouble))
        notify("Roost: the estate is degraded", f"{len(trouble)} apps at once: {names}")
    else:
        for app, state in trouble:
            notify("Roost: app trouble", f"{app} is {state}")

    if len(recovered) > COLLAPSE_AT:
        notify("Roost: recovered", f"{len(recovered)} apps are back up: {', '.join(sorted(recovered))}")
    else:
        for app in recovered:
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
