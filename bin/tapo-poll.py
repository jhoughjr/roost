#!/usr/bin/env python3
"""tapo-poll.py — read TP-Link Tapo devices over the local KLAP API.

Every Tapo device on the LAN answers a local, cloud-free API (KLAP on port 80)
authenticated with the TP-Link *account* credentials — no Matter controller and
no cloud round-trip, even on the Matter "M" models. This polls the configured
devices and does two things with the result:

  1. writes ~/.roost-tapo.json — the cache node-report.sh reads to fill
     `wattsW` for the box its plug powers (the opi), so pulse plots MEASURED
     watts for it instead of the idleW/maxW-by-load estimate.
  2. POSTs the whole device set to pulse /api/tapo (same NODE_KEY auth as
     /api/nodes), which watts' /home/ page renders.

Usage:
  tapo-poll.py             one read → write cache, POST to pulse, print a table
  tapo-poll.py --json      one read → JSON on stdout (no cache write, no POST)
  tapo-poll.py --watch     poll forever, every ROOST_TAPO_INTERVAL seconds
  tapo-poll.py --discover  broadcast-discover every Tapo device and print it
                           (works WITHOUT credentials — use it to find IPs)

Dependency: python-kasa, which is not stdlib — roost's one exception. It lives
in a venv at ~/.roost-tapo-venv (created by install-tapo-poll.sh) and this
script re-execs into it, so the shebang stays a plain python3.

Config, via ~/.roostrc KEY=VALUE lines:
  ROOST_TAPO_EMAIL     TP-Link account e-mail (required)
  ROOST_TAPO_DEVICES   comma list of `label=ip` (required), e.g.
                       opi=192.168.0.27,fridge=192.168.0.57
  ROOST_TAPO_FLEET     label of the plug powering THIS box (default: opi); its
                       watts land in the cache as `fleetWatts` for node-report
  ROOST_TAPO_INTERVAL  --watch seconds (default 30, matching node-report)
  ROOST_TAPO_PARENT    comma list of `child=parent` saying which plug feeds
                       which, e.g. fridge=room,opi=room,induction=room. A child
                       is physically downstream, so the parent's meter ALREADY
                       includes it — only roots are summed into totalWatts.
                       Omitting this double-counts every sub-metered device.
  ROOST_TAPO_BULB_W    dimmable bulb's rated draw at 100% (default 8.7, the
                       L530/L530E figure) — bulbs have no current sensor, so
                       their wattage is brightness × this, flagged `derived`
                       and kept out of every measured total
  ROOST_PULSE_URL      pulse base URL (default https://pulse.jimmyhoughjr.net)

Secrets: ~/.tapo_pass (chmod 600) holds the TP-Link account password; trailing
whitespace is stripped, so a trailing newline is fine. ~/.roost_node_key is the
pulse NODE_KEY — absent, the POST is skipped and the cache is still written.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

# python-kasa lives in its own venv; re-exec into it on first import failure.
# The "already inside it?" guard compares sys.prefix, NOT the interpreter path:
# a venv's bin/python3 is a symlink to the base interpreter, so realpath() makes
# the two look identical and the re-exec would be skipped forever.
VENV = os.path.expanduser("~/.roost-tapo-venv")
VENV_PY = os.path.join(VENV, "bin", "python3")
try:
    import kasa  # noqa: F401
except ImportError:
    if os.path.exists(VENV_PY) and os.path.normpath(sys.prefix) != os.path.normpath(VENV):
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("tapo-poll: python-kasa not installed — run install-tapo-poll.sh")

import asyncio  # noqa: E402

from kasa import Credentials, Discover  # noqa: E402

BIN = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BIN)
import roostlib  # noqa: E402

CACHE = os.path.expanduser("~/.roost-tapo.json")
PASS_FILE = os.path.expanduser("~/.tapo_pass")
KEY_FILE = os.path.expanduser("~/.roost_node_key")


def config():
    """Resolved config, or exit with a message naming the missing key."""
    cfg = roostlib.read_rc()
    email = cfg.get("ROOST_TAPO_EMAIL", "").strip()
    devices = cfg.get("ROOST_TAPO_DEVICES", "").strip()
    if not email:
        sys.exit("tapo-poll: set ROOST_TAPO_EMAIL in ~/.roostrc")
    if not devices:
        sys.exit("tapo-poll: set ROOST_TAPO_DEVICES in ~/.roostrc (label=ip,label=ip)")
    try:
        with open(PASS_FILE) as f:
            password = f.read().strip()
    except OSError:
        sys.exit(f"tapo-poll: missing {PASS_FILE} (TP-Link account password, chmod 600)")
    if not password:
        sys.exit(f"tapo-poll: {PASS_FILE} is empty")

    targets = []
    for pair in devices.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            sys.exit(f"tapo-poll: ROOST_TAPO_DEVICES entry {pair!r} is not label=ip")
        label, ip = pair.split("=", 1)
        targets.append((label.strip(), ip.strip()))
    return {
        "email": email,
        "password": password,
        "targets": targets,
        "fleet": cfg.get("ROOST_TAPO_FLEET", "opi").strip(),
        "interval": max(5, int(cfg.get("ROOST_TAPO_INTERVAL", "30") or 30)),
        # Which plug feeds which. Everything except the bulbs hangs off one
        # outlet here, so summing every plug counts the same joules twice —
        # invisibly at idle, and by ~1500 W the moment the kettle runs. A
        # device with a parent is INSIDE its parent's reading; only roots are
        # summed. See parents().
        "parents": parents(cfg),
        # Rated draw of a dimmable bulb at 100%, used for the derived wattage.
        # 8.7 W is the L530/L530E figure; a mixed set of bulbs would need this
        # per-device, which is a change worth making only once that's true.
        "bulbW": float(cfg.get("ROOST_TAPO_BULB_W", "8.7") or 8.7),
        "pulse": cfg.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").rstrip("/"),
    }


def parents(cfg):
    """`child=parent` map from ROOST_TAPO_PARENT, e.g. fridge=room,opi=room.

    A child plug is physically downstream of its parent, so the parent's meter
    already includes it. Roots are what you sum; children are a breakdown of
    where a root's watts went, and the difference between a root and its
    children is the unmetered rest of that circuit.
    """
    out = {}
    for pair in (cfg.get("ROOST_TAPO_PARENT", "") or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            sys.exit(f"tapo-poll: ROOST_TAPO_PARENT entry {pair!r} is not child=parent")
        child, parent = pair.split("=", 1)
        out[child.strip()] = parent.strip()
    return out


def energy_module(dev):
    """The Energy module across python-kasa key styles (enum, str, lowercase)."""
    mods = getattr(dev, "modules", None) or {}
    try:
        from kasa import Module

        if Module.Energy in mods:
            return mods[Module.Energy]
    except Exception:
        pass
    for key in ("Energy", "energy", "emeter"):
        if key in mods:
            return mods[key]
    return None


def num(v):
    """Finite float, else None — device fields go null rather than absent."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def brightness_of(dev):
    """Bulb brightness 0–100, or None on a device that has no dimmer."""
    mods = getattr(dev, "modules", None) or {}
    for key in ("Brightness", "Light"):
        m = mods.get(key)
        if m is not None:
            b = num(getattr(m, "brightness", None))
            if b is not None:
                return b
    return None


async def bulb_usage(dev, entry):
    """Cumulative energy for a meterless bulb, via get_device_usage.

    L530s answer get_device_usage but NOT get_current_power or
    get_energy_usage — they have no current sensor. What comes back is
    modelled: solve the device's own `saved_power` for its baseline and it is
    exactly 60.0 W, a hardcoded incandescent reference. So these are the same
    numbers the Tapo app shows, and they are a calculation, not a measurement.
    Recorded because they're still useful, flagged because they aren't metered.
    """
    try:
        r = await dev._query_helper("get_device_usage", {})
    except Exception:
        return
    du = (r or {}).get("get_device_usage") or {}
    power = du.get("power_usage") or {}
    today = num(power.get("today"))
    month = num(power.get("past30"))
    if today is not None:
        entry["kwhToday"] = round(today / 1000, 3)   # device reports Wh
    if month is not None:
        entry["kwhMonth"] = round(month / 1000, 3)
    mins = num((du.get("time_usage") or {}).get("today"))
    if mins is not None:
        entry["minsToday"] = int(mins)


async def read_dev(handle, creds, bulb_w=8.7):
    """One device → a plain dict. Never raises: failures come back as `err`.

    `handle` is a mutable [label, ip, device] triple so --watch reuses the
    authenticated session across ticks — a KLAP handshake is two extra round
    trips per device, which is most of the cost of a poll. A failed read drops
    the cached device so the next tick reconnects instead of wedging forever.
    """
    label, ip, dev = handle
    entry = {"label": label, "ip": ip}
    try:
        if dev is None:
            dev = await Discover.discover_single(ip, credentials=creds)
            handle[2] = dev
        await dev.update()
    except Exception as e:
        handle[2] = None
        entry["err"] = f"{type(e).__name__}: {e}"
        return entry
    try:
        entry["alias"] = getattr(dev, "alias", None)
        entry["model"] = getattr(dev, "model", None)
        entry["on"] = bool(getattr(dev, "is_on", False))
        rssi = num(getattr(dev, "rssi", None))
        if rssi is not None:
            entry["rssi"] = rssi

        em = energy_module(dev)
        if em is not None:
            w = num(getattr(em, "current_consumption", None))
            if w is not None:
                entry["watts"] = round(w, 2)
            today = num(getattr(em, "consumption_today", None))
            if today is not None:
                entry["kwhToday"] = round(today, 3)
            month = num(getattr(em, "consumption_this_month", None))
            if month is not None:
                entry["kwhMonth"] = round(month, 3)
            # Voltage/current are P110-family extras, absent on bulbs.
            status = getattr(em, "status", None) or {}
            if isinstance(status, dict):
                v = num(status.get("voltage_mv"))
                a = num(status.get("current_ma"))
                if v is not None:
                    entry["volts"] = round(v / 1000, 1)
                if a is not None:
                    entry["amps"] = round(a / 1000, 3)
        else:
            # No Energy module at all → no meter. A dimmable bulb still knows
            # its brightness, and brightness × rated draw is what the Tapo app
            # itself reports, so compute it — but mark it `derived` so nothing
            # downstream sums it into a total labelled "measured".
            bri = brightness_of(dev)
            if bri is not None:
                entry["brightness"] = round(bri)
                entry["derived"] = True
                entry["watts"] = round(bulb_w * bri / 100 if entry["on"] else 0.0, 2)
                await bulb_usage(dev, entry)
    except Exception as e:
        entry["err"] = f"{type(e).__name__}: {e}"
    return entry


async def close_all(handles):
    """Drop the KLAP sessions. --watch deliberately holds them open; a one-shot
    run must not, or aiohttp complains about unclosed connectors at exit."""
    for h in handles:
        if h[2] is not None:
            try:
                await h[2].disconnect()
            except Exception:
                pass
            h[2] = None


async def once(handles, creds, cfg):
    try:
        return await read_all(handles, creds, cfg)
    finally:
        await close_all(handles)


async def read_all(handles, creds, cfg):
    devices = list(await asyncio.gather(*(read_dev(h, creds, cfg["bulbW"]) for h in handles)))
    # Metered only, on both counts. fleetWatts becomes a node's wattsW in pulse,
    # so a derived figure must never reach it; and a total mixing measured plugs
    # with modelled bulbs would be neither one thing nor the other.
    fleet_w = next(
        (d.get("watts") for d in devices
         if d["label"] == cfg["fleet"] and d.get("watts") is not None and not d.get("derived")),
        None,
    )
    # Record the topology on each device so every consumer downstream can get
    # the same sum right without re-deriving it.
    for d in devices:
        p = cfg["parents"].get(d["label"])
        if p:
            d["parent"] = p

    # Sum ROOTS only. A child plug sits downstream of its parent, so the
    # parent's meter already counted it; adding both bills the same watts
    # twice. Invisible while the children idle, ~1500 W wrong when the kettle
    # runs. Derived (bulb) figures stay out of measured totals entirely.
    total = sum(d["watts"] for d in devices
                if d.get("watts") is not None and not d.get("derived") and not d.get("parent"))
    return {
        "t": int(time.time()),
        "fleetLabel": cfg["fleet"],
        "fleetWatts": fleet_w,
        "totalWatts": round(total, 2),
        "devices": devices,
    }


def write_cache(payload):
    """Atomic — node-report.sh reads this on a 30 s timer and must never see
    a half-written file."""
    tmp = f"{CACHE}.tmp"
    with open(tmp, "w") as f:
        # Compact separators on purpose: node-report.sh greps this file with a
        # plain ERE (no jq on the opi), and json.dump's default `", "` / `": "`
        # would put a space after every colon that a naive pattern misses.
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, CACHE)


def post_pulse(payload, pulse):
    try:
        with open(KEY_FILE) as f:
            key = f.read().strip()
    except OSError:
        return "no node key, POST skipped"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{pulse}/api/tapo", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-roost-node-key", key)
    # Cloudflare sits in front of pulse and 403s urllib's default
    # "Python-urllib/3.x" agent outright — verified: same URL, same key, same
    # body, 403 as Python-urllib and 200 as anything else. node-report never hit
    # this because it posts with curl. Identify honestly instead.
    req.add_header("user-agent", "roost-tapo-poll/1 (+https://github.com/jhoughjr/roost)")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"pulse {r.status}"
    except urllib.error.HTTPError as e:
        return f"pulse HTTP {e.code}"
    except Exception as e:
        return f"pulse unreachable: {type(e).__name__}"


def render(payload):
    devs = payload["devices"]

    def line(d, depth):
        pad = "  " * depth
        if d.get("err"):
            return f"  {pad}{d['label']:<12} {d['ip']:<15} ERROR  {d['err']}"
        w = d.get("watts")
        # "~" marks a derived (bulb) figure, so the table can't be misread as
        # measurements when one of them is arithmetic.
        watts = f"{'~' if d.get('derived') else ' '}{w:>6.2f} W" if w is not None else "      — "
        today = f" {d['kwhToday']:>6.3f} kWh today" if d.get("kwhToday") is not None else ""
        return (f"  {pad}{d['label']:<12} {d['ip']:<15} {'on ' if d.get('on') else 'off'} "
                f"{watts}{today}  {d.get('alias') or ''}")

    # Draw the wiring: each root, then its children indented beneath it, then
    # what is left on that circuit. A reader has to be able to see that the
    # children are part of the parent rather than additional to it.
    rows, seen = [], set()
    for root in [d for d in devs if not d.get("parent")]:
        rows.append(line(root, 0))
        seen.add(id(root))
        kids = [d for d in devs if d.get("parent") == root["label"]]
        for k in kids:
            rows.append(line(k, 1))
            seen.add(id(k))
        metered = [k for k in kids if k.get("watts") is not None]
        if metered and root.get("watts") is not None:
            rest = root["watts"] - sum(k["watts"] for k in metered)
            rows.append(f"    {'(unmetered)':<12} {'':<15}    {rest:>7.2f} W  rest of {root['label']}")
    # Anything whose parent is missing from the config still has to appear.
    for d in devs:
        if id(d) not in seen:
            rows.append(line(d, 0))

    head = f"tapo — {payload['totalWatts']:.2f} W metered"
    if payload.get("fleetWatts") is not None:
        head += f", fleet({payload['fleetLabel']}) {payload['fleetWatts']:.2f} W"
    return "\n".join([head, *rows])


async def discover():
    """Credential-free broadcast discovery — prints label=ip lines to paste
    straight into ROOST_TAPO_DEVICES."""
    found = await Discover.discover(discovery_timeout=8)
    if not found:
        return "tapo-poll: no Tapo devices answered discovery"
    lines = []
    for ip, dev in sorted(found.items()):
        dr = getattr(dev, "_discovery_info", {}) or {}
        model = dr.get("device_model") or getattr(dev, "model", "?")
        kind = dr.get("device_type") or "?"
        lines.append(f"  {ip:<15} {model:<14} {kind}")
    return "\n".join(["tapo devices on the LAN:", *lines])


def main():
    args = set(sys.argv[1:])
    if args - {"--json", "--watch", "--discover"}:
        sys.exit(__doc__)

    if "--discover" in args:
        print(asyncio.run(discover()))
        return

    cfg = config()
    creds = Credentials(cfg["email"], cfg["password"])
    handles = [[label, ip, None] for label, ip in cfg["targets"]]

    if "--json" in args:
        print(json.dumps(asyncio.run(once(handles, creds, cfg)), indent=2))
        return

    if "--watch" in args:
        asyncio.run(watch(handles, creds, cfg))
        return

    payload = asyncio.run(once(handles, creds, cfg))
    write_cache(payload)
    print(render(payload))
    print(f"  cache: {CACHE} · {post_pulse(payload, cfg['pulse'])}")


async def watch(handles, creds, cfg):
    """One event loop, one KLAP session per device, for the life of the process.
    The pulse POST goes to a thread — a slow edge must not delay the next read
    or stall the cache node-report depends on."""
    last = None
    while True:
        payload = await read_all(handles, creds, cfg)
        write_cache(payload)
        result = await asyncio.to_thread(post_pulse, payload, cfg["pulse"])
        # Log on change only: a healthy loop stays silent, but a POST that
        # starts failing lands in the journal instead of vanishing. Throwing
        # this result away is how a Cloudflare 403 survived a whole deploy
        # looking like success.
        if result != last:
            print(f"tapo-poll: {result}", file=sys.stderr, flush=True)
            last = result
        await asyncio.sleep(cfg["interval"])


if __name__ == "__main__":
    main()
