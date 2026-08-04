#!/usr/bin/env python3
"""ha-scoop.py — copy Home Assistant's hourly power statistics into pulse.

pulse samples every 30 s and keeps 90 days; Home Assistant keeps an hourly
mean per entity FOREVER, in a `statistics` table its recorder never purges.
Those are complementary, not redundant, and this moves the long tail across so
pulse can answer questions older than its own retention.

It also patches holes. The two systems read the same plugs independently, so
one can be blind while the other is not: on 2026-08-04 a firmware push broke
roost's poller at 02:55 while HA — holding an already-open session — kept
logging until 07:55. pulse's history has a 3h34m gap across that morning that
HA can fill exactly.

Read granularity honestly: an hourly MEAN is not a 30 s peak. A kettle that
draws 1500 W for ninety seconds shows up here as ~37 W spread over its hour.
Rows land in pulse tagged `src:"ha"` and only ever fill buckets that have no
sampled data, so a real reading always wins over an averaged one.

Statistics are not on HA's REST API — only over the WebSocket API, hence the
aiohttp dependency, which is already in the tapo venv as a python-kasa
dependency. This re-execs into that venv the same way tapo-poll.py does.

Usage:
  ha-scoop.py               scoop the last ROOST_HA_SCOOP_DAYS → POST to pulse
  ha-scoop.py --days 90     scoop a specific window (use once to backfill)
  ha-scoop.py --json        print the rows instead of POSTing
  ha-scoop.py --entities    list HA's power entities and exit (to build config)

Config, via ~/.roostrc KEY=VALUE lines:
  ROOST_HA_URL         HA base URL (default http://opi.local:8123)
  ROOST_HA_ENTITIES    comma list of `label=entity_id`, where label matches the
                       ROOST_TAPO_DEVICES label so both sources describe the
                       same device, e.g. room=sensor.room_power_current_consumption
  ROOST_HA_SCOOP_DAYS  default window (default 2 — enough to cover a missed run
                       and let HA finish computing the current hour)
  ROOST_PULSE_URL      pulse base URL (default https://pulse.jimmyhoughjr.net)

Secrets: ~/.ha_token (chmod 600) is a HA long-lived access token, made under
Profile → Security. ~/.roost_node_key is the pulse NODE_KEY; absent, the POST
is skipped and --json still works.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import asyncio
import datetime

# aiohttp rides along in the tapo venv. The re-exec fires only under __main__:
# importing this module must never replace the running process, or a test that
# imports it to check the row shaping would silently become a run of the tool.
# Same sys.prefix guard as tapo-poll.py — a venv's bin/python3 symlinks to the
# base interpreter, so comparing resolved paths makes the re-exec look
# already-done and it would loop forever.
VENV = os.path.expanduser("~/.roost-tapo-venv")
VENV_PY = os.path.join(VENV, "bin", "python3")
try:
    import aiohttp
except ImportError:
    aiohttp = None

BIN = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BIN)
import roostlib  # noqa: E402

TOKEN_FILE = os.path.expanduser("~/.ha_token")
KEY_FILE = os.path.expanduser("~/.roost_node_key")


def config():
    """Resolved config, or exit naming what is missing."""
    cfg = roostlib.read_rc()
    url = cfg.get("ROOST_HA_URL", "http://opi.local:8123").strip().rstrip("/")
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        sys.exit(f"ha-scoop: missing {TOKEN_FILE} (HA long-lived token, chmod 600)")
    if not token:
        sys.exit(f"ha-scoop: {TOKEN_FILE} is empty")
    entities = {}
    for pair in cfg.get("ROOST_HA_ENTITIES", "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        label, _, ent = pair.partition("=")
        entities[label.strip()] = ent.strip()
    days = cfg.get("ROOST_HA_SCOOP_DAYS", "2").strip()
    pulse = cfg.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").strip().rstrip("/")
    try:
        days = max(1, int(days))
    except ValueError:
        days = 2
    return url, token, entities, days, pulse


async def ws_call(url, token, message):
    """One authenticated WebSocket round-trip. Returns the `result` payload.

    HA greets, demands auth, then answers commands by id. Nothing here is
    long-lived: a scoop is a cron-shaped job, not a subscription.
    """
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, timeout=aiohttp.ClientWSTimeout(ws_close=20)) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                sys.exit("ha-scoop: HA rejected the token in ~/.ha_token")
            await ws.send_json({"id": 1, **message})
            reply = await ws.receive_json()
            if not reply.get("success"):
                sys.exit(f"ha-scoop: HA refused {message.get('type')}: {reply.get('error')}")
            return reply.get("result") or {}


async def list_power_entities(url, token):
    """Every power sensor HA knows, as `label=entity_id` config lines."""
    states = await ws_call(url, token, {"type": "get_states"})
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if s.get("attributes", {}).get("device_class") == "power" or "current_consumption" in eid:
            out.append((eid, s.get("state")))
    return sorted(out)


async def scoop(url, token, entities, days):
    """Hourly means per label for the window, oldest first."""
    if not entities:
        sys.exit("ha-scoop: set ROOST_HA_ENTITIES in ~/.roostrc (label=entity_id) "
                 "— run `ha-scoop.py --entities` to see what HA has")
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    result = await ws_call(url, token, {
        "type": "recorder/statistics_during_period",
        "start_time": start.isoformat(),
        "statistic_ids": sorted(set(entities.values())),
        "period": "hour",
        "types": ["mean"],
    })
    return rows_from_result(result, entities)


HOUR = 3600


def rows_from_result(result, entities):
    """HA's statistics reply → the rows pulse ingests. Pure; see tests.

    pulse keys on (device, hour) and REJECTS anything not landing exactly on an
    hour boundary, because an off-boundary row would create a second key for the
    same period that no later scoop could overwrite. So snap here rather than
    trust the source: HA's buckets are hour-aligned in every timezone we run in,
    but a half-hour zone (Asia/Kolkata, Australia/Adelaide) would otherwise have
    every single row silently dropped at the far end with a 200 OK.
    """
    by_entity = {ent: label for label, ent in entities.items()}
    rows = []
    for ent, points in result.items():
        label = by_entity.get(ent)
        if not label:
            continue
        for p in points:
            mean, start_ms = p.get("mean"), p.get("start")
            if mean is None or start_ms is None:
                continue
            try:
                watts = round(float(mean), 2)
            except (TypeError, ValueError):
                continue
            # Seconds, matching pulse's sample clock; HA reports epoch ms.
            t = (int(start_ms) // 1000 // HOUR) * HOUR
            rows.append({"n": label, "t": t, "w": watts})
    rows.sort(key=lambda r: (r["t"], r["n"]))
    return rows


def post_pulse(rows, pulse):
    try:
        with open(KEY_FILE) as f:
            key = f.read().strip()
    except OSError:
        return "no node key, POST skipped"
    body = json.dumps({"period": "hour", "src": "ha", "rows": rows}).encode()
    req = urllib.request.Request(f"{pulse}/api/tapo/hourly", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-roost-node-key", key)
    # Cloudflare 403s the default Python-urllib agent outright — see tapo-poll.py.
    req.add_header("user-agent", "roost-ha-scoop/1 (+https://github.com/jhoughjr/roost)")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"pulse {r.status}: {r.read().decode()[:120]}"
    except urllib.error.HTTPError as e:
        return f"pulse HTTP {e.code}"
    except Exception as e:
        return f"pulse unreachable: {type(e).__name__}"


def main():
    args = sys.argv[1:]
    if aiohttp is None:
        if os.path.exists(VENV_PY) and os.path.normpath(sys.prefix) != os.path.normpath(VENV):
            os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *args])
        sys.exit("ha-scoop: aiohttp not installed — run install-tapo-poll.sh")
    url, token, entities, days, pulse = config()

    if "--entities" in args:
        for eid, state in asyncio.run(list_power_entities(url, token)):
            print(f"{eid}\t{state}")
        return

    if "--days" in args:
        try:
            days = max(1, int(args[args.index("--days") + 1]))
        except (IndexError, ValueError):
            sys.exit("ha-scoop: --days needs a number")

    rows = asyncio.run(scoop(url, token, entities, days))
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print(f"ha-scoop: HA returned no statistics for the last {days}d", file=sys.stderr)
        return
    labels = sorted({r["n"] for r in rows})
    span = f"{rows[0]['t']}..{rows[-1]['t']}"
    print(f"ha-scoop: {len(rows)} hourly rows, {len(labels)} devices ({', '.join(labels)}), {span}")
    print(f"ha-scoop: {post_pulse(rows, pulse)}")


if __name__ == "__main__":
    main()
