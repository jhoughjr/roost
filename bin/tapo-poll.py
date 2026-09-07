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
  tapo-poll.py --direct    one read straight from the devices, ignoring the HA
                           mapping, for a one-off at full resolution

Two ways to read a device. The direct path opens a KLAP session on the device
itself. The HA path reads Home Assistant's state for it, and HA tracks a device
by its hardware address, so a DHCP lease change never loses it, and one reader
holds the device's single session. A device named in ROOST_HA_TAPO reads
through HA, and every other device reads direct. While pulse's fast sample rate
is armed, every device reads direct, because HA polls on its own clock and a
dip that lasts seconds is averaged away before HA sees it. When the fast window
ends, the direct sessions on HA-read devices are dropped so HA gets them back.
If HA does not answer, the tick falls back to direct for its devices, and the
entry says so in `src`.

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
  ROOST_HA_TAPO        comma list of `label=ha_name` for the devices HA reads.
                       A plug's name is the base of its HA entities, so
                       room=room_power reads switch.room_power and
                       sensor.room_power_voltage. A light's name is its whole
                       entity id, e.g. ceiling-l=light.ceiling_fixture_l.
  ROOST_HA_URL         HA base URL (default http://opi.local:8123), with the
                       token in ~/.ha_token (chmod 600)
  ROOST_TAPO_PARENT    comma list of `child=parent` saying which plug feeds
                       which, e.g. fridge=room,opi=room,induction=room. A child
                       is physically downstream, so the parent's meter ALREADY
                       includes it — only roots are summed into totalWatts.
                       Bulbs have no parent (lighting circuit, not a plug), so
                       they are roots and do count toward the total.
                       Omitting this double-counts every sub-metered device.
  ROOST_TAPO_BULB_W    dimmable bulb's rated draw at 100% (default 8.7, the
                       L530/L530E figure) — bulbs have no current sensor, so
                       their wattage is brightness × this. For a PWM-dimmed LED
                       that IS the power, brightness being the duty cycle, so it
                       counts toward totalWatts; it stays flagged `derived` and
                       broken out as derivedWatts because its provenance is
                       arithmetic, not a meter. Two known biases: a saturated
                       COLOUR drives fewer emitters and draws less than this
                       predicts, and the driver's floor makes the bottom of the
                       range less than linear.
  ROOST_PULSE_URL      pulse base URL (default https://pulse.jimmyhoughjr.net)

Secrets: ~/.tapo_pass (chmod 600) holds the TP-Link account password; trailing
whitespace is stripped, so a trailing newline is fine. ~/.roost_node_key is the
pulse NODE_KEY — absent, the POST is skipped and the cache is still written.
"""
import json
import os
import sys
import urllib.request
import time
import urllib.error
import urllib.request

# python-kasa lives in its own venv; re-exec into it on first import failure.
# The "already inside it?" guard compares sys.prefix, NOT the interpreter path:
# a venv's bin/python3 is a symlink to the base interpreter, so realpath() makes
# the two look identical and the re-exec would be skipped forever.
VENV = os.path.expanduser("~/.roost-tapo-venv")
VENV_PY = os.path.join(VENV, "bin", "python3")
# Tolerate a missing python-kasa at IMPORT time and re-exec from main() instead,
# for the same reason ha-scoop.py keeps its aiohttp import soft: a test that
# imports this module to check the wattage shaping must not become a run of the
# tool. CI has no venv, so an execv/sys.exit out here would take the test
# process with it — which is why the bulb bug below shipped untested.
try:
    import kasa  # noqa: F401
    from kasa import Credentials, Discover
except ImportError:
    kasa = Credentials = Discover = None

import asyncio  # noqa: E402


def ensure_kasa():
    """Re-exec into the venv holding python-kasa, or exit saying how to get it.

    The "already inside it?" guard compares sys.prefix, NOT the interpreter
    path: a venv's bin/python3 is a symlink to the base interpreter, so
    realpath() makes the two look identical and the re-exec would be skipped
    forever.
    """
    if kasa is not None:
        return
    if os.path.exists(VENV_PY) and os.path.normpath(sys.prefix) != os.path.normpath(VENV):
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("tapo-poll: python-kasa not installed — run install-tapo-poll.sh")

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
        "ha": ha_map(cfg),
        "haUrl": cfg.get("ROOST_HA_URL", "http://opi.local:8123").rstrip("/"),
        "haToken": ha_token(cfg),
    }


HA_TOKEN_FILE = os.path.expanduser("~/.ha_token")


def ha_map(cfg):
    """label → HA name for the devices HA reads. Empty means every device reads direct."""
    out = {}
    for pair in cfg.get("ROOST_HA_TAPO", "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            sys.exit(f"tapo-poll: ROOST_HA_TAPO entry {pair!r} is not label=ha_name")
        label, name = pair.split("=", 1)
        out[label.strip()] = name.strip()
    return out


def ha_token(cfg):
    """The HA token, required only when a device reads through HA."""
    if not ha_map(cfg):
        return None
    try:
        with open(HA_TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        sys.exit(f"tapo-poll: ROOST_HA_TAPO is set and {HA_TOKEN_FILE} is missing (a HA long-lived token, chmod 600)")
    if not token:
        sys.exit(f"tapo-poll: {HA_TOKEN_FILE} is empty")
    return token


def ha_states(url, token):
    """Every HA state in one GET, keyed by entity id. Raises on any failure, and the caller decides what a tick does without HA."""
    req = urllib.request.Request(f"{url}/api/states", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return {s["entity_id"]: s for s in json.load(r)}


def read_ha(label, ip, name, states, bulb_w=8.7):
    """One device through HA, in the shape read_dev gives, so nothing downstream can tell the paths apart except by `src`."""
    entry = {"label": label, "ip": ip, "src": "ha"}

    def live(entity_id):
        s = states.get(entity_id)
        if s is None or s.get("state") in ("unavailable", "unknown"):
            return None
        return s

    if name.startswith("light."):
        s = live(name)
        if s is None:
            entry["err"] = f"ha: {name} unavailable"
            return entry
        attrs = s.get("attributes", {})
        entry["alias"] = attrs.get("friendly_name")
        entry["on"] = s["state"] == "on"
        bri = num(attrs.get("brightness"))
        # HA gives brightness 0–255. The direct path gives 0–100, and the derived wattage is duty cycle × rated draw either way.
        if bri is not None:
            entry["brightness"] = round(bri / 255 * 100)
            entry["derived"] = True
            entry["watts"] = round(bulb_w * bri / 255 if entry["on"] else 0.0, 2)
        elif not entry["on"]:
            entry["derived"] = True
            entry["watts"] = 0.0
        return entry

    sw = live(f"switch.{name}")
    if sw is None:
        entry["err"] = f"ha: switch.{name} unavailable"
        return entry
    entry["alias"] = sw.get("attributes", {}).get("friendly_name")
    entry["on"] = sw["state"] == "on"
    # HA reports W, V, A and kWh already, so the rounding is the only arithmetic.
    for key, suffix, digits in (
        ("watts", "current_consumption", 2),
        ("volts", "voltage", 1),
        ("amps", "current", 3),
        ("kwhToday", "today_s_consumption", 3),
        ("kwhMonth", "this_month_s_consumption", 3),
    ):
        s = live(f"sensor.{name}_{suffix}")
        v = num(s["state"]) if s else None
        if v is not None:
            entry[key] = round(v, digits)
    return entry


def sample_rate(pulse):
    """Pulse's sample rate, or None when pulse does not answer. A fast rate means the direct path for every device."""
    try:
        with urllib.request.urlopen(f"{pulse}/api/sample-rate", timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


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
    """Bulb brightness 0–100, or None when this tick exposes no dimmer.

    `Light.brightness` RAISES instead of returning None when the Brightness
    module is missing from the device's module list, because python-kasa's
    `Light.is_dimmable` is only `Module.Brightness in modules` — it asks the
    module list, never the hardware. So a fully dimmable multicolor bulb
    reports "Bulb is not dimmable." on any tick where that module drops out.

    getattr's default does not help: it catches AttributeError, not a property
    that raises. That exception used to escape into read_dev's handler and blank
    the WHOLE device — watts, kWh today, kWh month, all replaced by one `err`
    string — for a bulb that was lit and working. Swallow per module and fall
    through to the next candidate instead.
    """
    mods = getattr(dev, "modules", None) or {}
    for key in ("Brightness", "Light"):
        m = mods.get(key)
        if m is None:
            continue
        try:
            b = num(getattr(m, "brightness", None))
        except Exception:
            continue
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
            # Cumulative usage is an independent query, so ask for it even on a
            # tick with no readable brightness — otherwise a momentarily absent
            # Brightness module throws away the bulb's kWh as well as its watts.
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


async def once(handles, creds, cfg, direct=False):
    try:
        return await read_all(handles, creds, cfg, direct)
    finally:
        await close_all(handles)


async def read_all(handles, creds, cfg, direct=False):
    via_ha = {} if direct else cfg.get("ha", {})
    states, ha_err = None, None
    if via_ha:
        try:
            states = await asyncio.to_thread(ha_states, cfg["haUrl"], cfg["haToken"])
        except Exception as e:
            ha_err = f"{type(e).__name__}: {e}"

    async def one(handle):
        label, ip, _ = handle
        if label in via_ha and states is not None:
            return read_ha(label, ip, via_ha[label], states, cfg["bulbW"])
        entry = await read_dev(handle, creds, cfg["bulbW"])
        # A tick without HA reads direct rather than blank, and says so, because a reading that is late beats one that is missing.
        if label in via_ha:
            entry["src"] = "direct-fallback"
            entry["haErr"] = ha_err
        return entry

    devices = list(await asyncio.gather(*(one(h) for h in handles)))
    # fleetWatts becomes a node's wattsW in pulse, which is advertised as a
    # measurement of that box, so a derived figure must never reach it. This is
    # the one place the measured/derived line still bars the way.
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
    # runs.
    #
    # Bulbs ARE in this total. They are not on any metered plug — they hang off
    # the lighting circuit, so they are roots and there is nothing to
    # double-count. And a PWM-dimmed LED's draw genuinely IS duty cycle × rated
    # draw: brightness is the duty cycle, so the arithmetic describes the
    # mechanism rather than approximating it. Leaving three lit bulbs out
    # understated the house by ~26 W, a bigger error than any imprecision in
    # putting them in. The biases that remain are named on ROOST_TAPO_BULB_W.
    roots = [d for d in devices if d.get("watts") is not None and not d.get("parent")]
    measured = sum(d["watts"] for d in roots if not d.get("derived"))
    derived = sum(d["watts"] for d in roots if d.get("derived"))
    return {
        "t": int(time.time()),
        "fleetLabel": cfg["fleet"],
        "fleetWatts": fleet_w,
        # The grand total, and the split saying where it came from. A breakdown
        # rather than an exclusion, so a consumer can still ask for "measured
        # only" without the poller deciding that on its behalf.
        "totalWatts": round(measured + derived, 2),
        "measuredWatts": round(measured, 2),
        "derivedWatts": round(derived, 2),
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

    head = f"tapo — {payload['totalWatts']:.2f} W total"
    # Show the split, so "total" is never read as "all of it metered".
    if payload.get("derivedWatts"):
        head += (f" ({payload['measuredWatts']:.2f} metered"
                 f" + {payload['derivedWatts']:.2f} lights)")
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
    if args - {"--json", "--watch", "--discover", "--direct"}:
        sys.exit(__doc__)
    ensure_kasa()

    if "--discover" in args:
        print(asyncio.run(discover()))
        return

    cfg = config()
    creds = Credentials(cfg["email"], cfg["password"])
    handles = [[label, ip, None] for label, ip in cfg["targets"]]

    direct = "--direct" in args

    if "--json" in args:
        print(json.dumps(asyncio.run(once(handles, creds, cfg, direct)), indent=2))
        return

    if "--watch" in args:
        asyncio.run(watch(handles, creds, cfg))
        return

    payload = asyncio.run(once(handles, creds, cfg, direct))
    write_cache(payload)
    print(render(payload))
    print(f"  cache: {CACHE} · {post_pulse(payload, cfg['pulse'])}")


async def watch(handles, creds, cfg):
    """One event loop, one KLAP session per device, for the life of the process.
    The pulse POST goes to a thread — a slow edge must not delay the next read
    or stall the cache node-report depends on."""
    last = None
    was_fast = False
    while True:
        # The fast window: pulse says a person is watching, so every device reads direct at pulse's rate.
        rate = await asyncio.to_thread(sample_rate, cfg["pulse"]) if cfg.get("ha") else None
        fast = bool(rate and rate.get("fast") and num(rate.get("seconds")) and rate["seconds"] < cfg["interval"])
        if was_fast and not fast:
            # The window closed. Drop the direct sessions on HA-read devices, so HA gets its one session back.
            await close_all([h for h in handles if h[0] in cfg.get("ha", {})])
            print("tapo-poll: fast window closed, HA-read devices back to HA", file=sys.stderr, flush=True)
        elif fast and not was_fast:
            print(f"tapo-poll: fast window open, every device direct at {rate['seconds']}s", file=sys.stderr, flush=True)
        was_fast = fast
        payload = await read_all(handles, creds, cfg, direct=fast)
        write_cache(payload)
        result = await asyncio.to_thread(post_pulse, payload, cfg["pulse"])
        # Log on change only: a healthy loop stays silent, but a POST that
        # starts failing lands in the journal instead of vanishing. Throwing
        # this result away is how a Cloudflare 403 survived a whole deploy
        # looking like success.
        if result != last:
            print(f"tapo-poll: {result}", file=sys.stderr, flush=True)
            last = result
        await asyncio.sleep(max(2, int(rate["seconds"])) if fast else cfg["interval"])


if __name__ == "__main__":
    main()
