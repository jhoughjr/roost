#!/usr/bin/env python3
"""Show mains voltage against load, so a dip can be matched to what caused it.

The question this answers is whether the line sags when something heavy starts,
and whether a machine on a UPS sags with it. A machine that dips with the house
is a machine the UPS is not isolating.

Usage: volts.py [hours]        (default 2)
       ROOST_PULSE_URL         pulse base URL
"""
import json, os, sys, urllib.request, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import roostlib
    CFG = roostlib.read_rc()
except Exception:
    CFG = {}

PULSE = CFG.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net").rstrip("/")
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 2

# Eight levels is enough to see a dip and few enough to read in a terminal.
BARS = "▁▂▃▄▅▆▇█"


def spark(values, lo=None, hi=None):
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1e-9:
        return BARS[0] * len(values)
    # A gap reads as a space rather than as the lowest reading, because a missing
    # sample is not a reading of zero.
    return "".join(" " if v is None else BARS[min(7, int((v - lo) / (hi - lo) * 7.999))]
                   for v in values)


def main():
    url = f"{PULSE}/api/history?hours={HOURS:g}"
    # A named agent, because the edge refuses python-urllib's default and the
    # refusal arrives as an HTTP error with nothing in it that says why.
    req = urllib.request.Request(url, headers={"User-Agent": "roost-volts"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    pts = d.get("samples") or d.get("points") or d.get("rows") or []
    if not pts:
        sys.exit("no samples in that window")

    volts, watts, dish = {}, {}, []
    for p in pts:
        seen = set()
        for t in (p.get("tapo") or []):
            n = t.get("n")
            seen.add(n)
            volts.setdefault(n, []).append(t.get("v"))
            watts.setdefault(n, []).append(t.get("w"))
        # Keep the series aligned when a device misses a sample.
        for n in list(volts):
            if n not in seen:
                volts[n].append(None)
                watts[n].append(None)
        dish.append(p.get("dishW"))

    span = f"{HOURS:g}h, {len(pts)} samples"
    first = datetime.datetime.fromtimestamp(pts[0]["t"]).strftime("%H:%M")
    last = datetime.datetime.fromtimestamp(pts[-1]["t"]).strftime("%H:%M")
    print(f"pulse {span}  {first} -> {last}")

    measured = {n: v for n, v in volts.items() if any(x is not None for x in v)}
    if not measured:
        print("  no voltage recorded yet in this window")
    else:
        # One scale across every plug, because the comparison between them is the
        # whole point and a per-plug scale would hide which one moved further.
        allv = [x for v in measured.values() for x in v if x is not None]
        lo, hi = min(allv), max(allv)
        print(f"\n  volts   scale {lo:.1f} .. {hi:.1f} V")
        for n in sorted(measured):
            cur = next((x for x in reversed(measured[n]) if x is not None), None)
            print(f"    {n:10} {spark(measured[n], lo, hi)}  now {cur:.1f} V")

    print("\n  watts")
    for n in sorted(watts):
        if not any(x is not None for x in watts[n]):
            continue
        cur = next((x for x in reversed(watts[n]) if x is not None), None)
        print(f"    {n:10} {spark(watts[n])}  now {cur:.0f} W")
    if any(x is not None for x in dish):
        cur = next((x for x in reversed(dish) if x is not None), None)
        print(f"    {'dish':10} {spark(dish)}  now {cur:.0f} W")


if __name__ == "__main__":
    main()
