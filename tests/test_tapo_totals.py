#!/usr/bin/env python3
"""Tests for what tapo-poll counts into totalWatts.

Bulbs used to be excluded because their wattage is arithmetic rather than a
meter reading. That conflated provenance with accuracy: a PWM-dimmed LED's draw
IS its duty cycle times its rated draw, brightness being the duty cycle, and the
bulbs hang off the lighting circuit rather than any metered plug — so they are
roots with nothing to double-count. Excluding three lit bulbs understated the
house by ~26 W.

What must still hold: children stay out (their parent's meter already counted
them), fleetWatts stays measured-only (pulse advertises it as a measurement of
that box), and the measured/derived split stays legible so a consumer can ask
for one or the other.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import asyncio
import importlib.util
import os
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def load():
    spec = importlib.util.spec_from_file_location("tapo_poll", os.path.join(BIN, "tapo-poll.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Energy:
    """A plug's Energy module — the metered path."""

    def __init__(self, watts):
        self.current_consumption = watts
        self.consumption_today = None
        self.consumption_this_month = None
        self.status = {}


class Brightness:
    def __init__(self, brightness):
        self.brightness = brightness


class Dev:
    def __init__(self, modules, is_on=True, alias="dev"):
        self.modules = modules
        self.is_on = is_on
        self.alias = alias
        self.model = "test"

    async def update(self):
        return None


def cfg(parents=None, fleet="opi"):
    return {"bulbW": 8.7, "fleet": fleet, "parents": parents or {}}


class TotalsTests(unittest.TestCase):
    def setUp(self):
        self.tp = load()

    def run_all(self, handles, conf):
        return asyncio.run(self.tp.read_all(handles, None, conf))

    def plug(self, label, watts, ip="192.168.0.1"):
        return [label, ip, Dev({"Energy": Energy(watts)})]

    def bulb(self, label, brightness, ip="192.168.0.2", is_on=True):
        return [label, ip, Dev({"Brightness": Brightness(brightness)}, is_on=is_on)]

    def test_bulbs_count_toward_the_total(self):
        """The change: a lit bulb on the lighting circuit is real consumption."""
        out = self.run_all([self.plug("room", 100.0), self.bulb("closet", 100)], cfg())
        self.assertEqual(out["totalWatts"], 108.7)
        self.assertEqual(out["measuredWatts"], 100.0)
        self.assertEqual(out["derivedWatts"], 8.7)

    def test_three_bulbs_at_full(self):
        """The ~26 W that used to vanish."""
        handles = [self.bulb("ceiling-l", 100), self.bulb("ceiling-r", 100),
                   self.bulb("closet", 100)]
        out = self.run_all(handles, cfg())
        self.assertEqual(out["derivedWatts"], 26.1)
        self.assertEqual(out["totalWatts"], 26.1)
        self.assertEqual(out["measuredWatts"], 0)

    def test_dimmed_bulb_scales_with_duty_cycle(self):
        """Half duty cycle, half the draw — the whole basis for counting it."""
        out = self.run_all([self.bulb("closet", 50)], cfg())
        self.assertEqual(out["derivedWatts"], 4.35)

    def test_bulb_off_adds_nothing(self):
        out = self.run_all([self.plug("room", 100.0), self.bulb("closet", 100, is_on=False)], cfg())
        self.assertEqual(out["totalWatts"], 100.0)
        self.assertEqual(out["derivedWatts"], 0)

    def test_child_plug_still_excluded(self):
        """Unchanged and load-bearing: the parent's meter already counted it, so
        summing both bills the same watts twice — ~1500 W wrong on the kettle."""
        handles = [self.plug("room", 500.0), self.plug("fridge", 40.0)]
        out = self.run_all(handles, cfg(parents={"fridge": "room"}))
        self.assertEqual(out["totalWatts"], 500.0)
        self.assertEqual(out["measuredWatts"], 500.0)

    def test_a_parented_bulb_is_excluded_too(self):
        """If a bulb ever does sit on a metered plug, the plug wins — the same
        double-count rule has to apply to derived figures."""
        handles = [self.plug("room", 500.0), self.bulb("lamp", 100)]
        out = self.run_all(handles, cfg(parents={"lamp": "room"}))
        self.assertEqual(out["totalWatts"], 500.0)
        self.assertEqual(out["derivedWatts"], 0)

    def test_fleet_watts_never_takes_a_derived_figure(self):
        """pulse turns fleetWatts into a node's wattsW and calls it measured, so
        arithmetic must not reach it even though it now counts in the total."""
        out = self.run_all([self.bulb("opi", 100)], cfg(fleet="opi"))
        self.assertIsNone(out["fleetWatts"])
        self.assertEqual(out["derivedWatts"], 8.7)

    def test_fleet_watts_still_reads_a_metered_plug(self):
        out = self.run_all([self.plug("opi", 4.2)], cfg(fleet="opi"))
        self.assertEqual(out["fleetWatts"], 4.2)

    def test_total_is_the_sum_of_its_parts(self):
        """The split must always reconcile, or the breakdown is a lie."""
        handles = [self.plug("room", 173.46), self.bulb("ceiling-l", 100),
                   self.bulb("closet", 40)]
        out = self.run_all(handles, cfg())
        self.assertAlmostEqual(out["totalWatts"],
                               out["measuredWatts"] + out["derivedWatts"], places=2)

    def test_errored_device_contributes_nothing(self):
        """A device that failed to read must not silently count as 0 W in a way
        that hides it — it simply is not in either sum."""
        broken = ["dead", "192.168.0.9", Dev({})]
        out = self.run_all([self.plug("room", 100.0), broken], cfg())
        self.assertEqual(out["totalWatts"], 100.0)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tp = load()

    def test_header_no_longer_calls_the_total_metered(self):
        """It stopped being a metered figure the moment bulbs joined it."""
        payload = {"totalWatts": 108.7, "measuredWatts": 100.0, "derivedWatts": 8.7,
                   "fleetLabel": "opi", "fleetWatts": 4.2, "devices": []}
        head = self.tp.render(payload).splitlines()[0]
        self.assertIn("108.70 W total", head)
        self.assertIn("100.00 metered", head)
        self.assertIn("8.70 lights", head)
        self.assertNotIn("W metered", head)

    def test_header_omits_the_split_with_no_lights(self):
        payload = {"totalWatts": 100.0, "measuredWatts": 100.0, "derivedWatts": 0,
                   "fleetLabel": "opi", "fleetWatts": None, "devices": []}
        head = self.tp.render(payload).splitlines()[0]
        self.assertIn("100.00 W total", head)
        self.assertNotIn("lights", head)


if __name__ == "__main__":
    unittest.main()
