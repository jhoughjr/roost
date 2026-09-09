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
sys.path.insert(0, os.path.join(os.path.dirname(BIN), "lib"))
import roostlib  # noqa: E402
from roost_secret import roost_secret  # noqa: E402

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
        key = roost_secret("NODE_KEY")
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

# The line here already sits near 100 V against a 120 V nominal, so an absolute
# threshold would either fire for ever or be set so low it never fires at all.
# What is worth saying is a change: the line dropped away from where it has been
# sitting, which is what a compressor starting looks like.
VOLTS_DIP = 4.0
# Below this the supply is bad whatever the baseline, and saying so is worth a
# repeat that the dip check would suppress.
VOLTS_FLOOR = 92.0
# Long enough to hold a dip that happened between two runs of this watchdog,
# short enough that the median still describes the line as it is now.
VOLTS_WINDOW_MIN = 20


def check_volts(cfg, prev):
    """Compare each plug's lowest reading against the line it has been sitting on.

    Returns the state to carry forward, so a dip is reported once rather than on
    every pass while the same samples stay inside the window.
    """
    state = {}
    try:
        pulse = cfg.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").rstrip("/")
        url = f"{pulse}/api/history?hours={VOLTS_WINDOW_MIN / 60:g}"
        # A named agent: the edge refuses python-urllib's default.
        req = urllib.request.Request(url, headers={"User-Agent": "roost-fleet-alert"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception as e:
        print(f"volts check skipped (ignored): {e}")
        return prev.get("volts", {})

    series = {}
    for p in (d.get("samples") or d.get("points") or d.get("rows") or []):
        for t in (p.get("tapo") or []):
            if isinstance(t.get("v"), (int, float)):
                series.setdefault(t["n"], []).append(float(t["v"]))

    for name, vals in series.items():
        # Two readings cannot describe a baseline, so nothing is claimed from them.
        if len(vals) < 5:
            continue
        ordered = sorted(vals)
        median = ordered[len(ordered) // 2]
        low = ordered[0]
        drop = round(median - low, 1)
        state[name] = {"median": round(median, 1), "low": round(low, 1)}
        was = prev.get("volts", {}).get(name, {})

        if low < VOLTS_FLOOR:
            if was.get("low", 999) >= VOLTS_FLOOR:
                notify("Roost: mains voltage low",
                       f"{name} reached {low:.1f} V, under the {VOLTS_FLOOR:.0f} V floor")
        elif drop >= VOLTS_DIP:
            # Reported when the dip is new, so a sag sitting in the window does not
            # earn a message every pass until it ages out.
            if round(was.get("median", 0) - was.get("low", 0), 1) < VOLTS_DIP:
                notify("Roost: mains voltage dipped",
                       f"{name} fell {drop:.1f} V to {low:.1f} V from about {median:.1f} V")
    return state


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

    # The line is checked here rather than in its own watchdog, because this one already
    # runs on a schedule and already knows how to say something once instead of every pass.
    cur["volts"] = check_volts(load_config(), prev)

    json.dump(cur, open(STATE, "w"))
    print(f"ok: {sum(1 for s in apps.values() if s == 'up')}/{len(apps)} up, "
          f"mem {cur['mem']}, disk {cur['disk']}"
          + (", volts " + ", ".join(f"{n} {v['low']:.0f}-{v['median']:.0f}"
                                    for n, v in sorted(cur["volts"].items())) if cur["volts"] else ""))

if __name__ == "__main__":
    main()
