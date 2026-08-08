#!/usr/bin/env python3
"""Tests for tapo-poll's meterless-bulb path.

A lit, fully dimmable multicolor L530 showed up on pulse as
`KasaException: Bulb is not dimmable.` and lost every field it had — watts,
kWh today, kWh month — while its identical twin at the same brightness reported
fine. python-kasa's `Light.is_dimmable` only checks whether the Brightness
module is in that tick's module list, and `Light.brightness` raises when it is
not, so a dropped module libels the bulb. `getattr(m, "brightness", None)` does
not catch a raising property, so it escaped into read_dev's broad handler and
blanked the device.

These pin both halves: a raising module must not escape, and it must not cost
the bulb its cumulative energy either.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import asyncio
import importlib.util
import os
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def load():
    """Import bin/tapo-poll.py by path — the hyphen makes it un-importable.

    This only works because the module's python-kasa import is soft; while it
    re-exec'd into the venv at import time, a test like this would have replaced
    the test process instead, which is how the bug reached production untested.
    """
    spec = importlib.util.spec_from_file_location("tapo_poll", os.path.join(BIN, "tapo-poll.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RaisingLight:
    """python-kasa's Light module on a tick where Brightness dropped out."""

    @property
    def brightness(self):
        raise RuntimeError("Bulb is not dimmable.")


class Brightness:
    def __init__(self, brightness):
        self.brightness = brightness


class Dev:
    """The parts of a python-kasa device that read_dev actually touches."""

    def __init__(self, modules, is_on=True):
        self.modules = modules
        self.is_on = is_on
        self.alias = "Ceiling Fixture -r"
        self.model = "L530"

    async def update(self):
        return None


class BrightnessOfTests(unittest.TestCase):
    def setUp(self):
        self.tp = load()

    def test_raising_light_module_is_swallowed(self):
        """The exact production shape: Light.brightness raises, and that must
        come back as "no brightness" rather than tearing out of the function."""
        self.assertIsNone(self.tp.brightness_of(Dev({"Light": RaisingLight()})))

    def test_brightness_module_preferred_over_raising_light(self):
        """With both present, read the module that works instead of tripping."""
        dev = Dev({"Brightness": Brightness(100), "Light": RaisingLight()})
        self.assertEqual(self.tp.brightness_of(dev), 100.0)

    def test_full_brightness_is_100_not_none(self):
        """Both bulbs sit at full; 100 must not be confused with "unknown"."""
        self.assertEqual(self.tp.brightness_of(Dev({"Brightness": Brightness(100)})), 100.0)

    def test_zero_brightness_is_zero_not_none(self):
        """0 is a reading, None is the absence of one — they differ downstream."""
        self.assertEqual(self.tp.brightness_of(Dev({"Brightness": Brightness(0)})), 0.0)

    def test_no_modules_yields_none(self):
        self.assertIsNone(self.tp.brightness_of(Dev({})))


class ReadDevBulbTests(unittest.TestCase):
    def setUp(self):
        self.tp = load()

    def read(self, dev, label="ceiling-r"):
        return asyncio.run(self.tp.read_dev([label, "192.168.0.16", dev], None, 8.7))

    def test_raising_brightness_does_not_blank_the_device(self):
        """Regression: the bulb kept its identity and on-state on pulse, and
        must no longer be reduced to a single `err` string."""
        entry = self.read(Dev({"Light": RaisingLight()}))
        self.assertNotIn("err", entry)
        self.assertEqual(entry["model"], "L530")
        self.assertEqual(entry["alias"], "Ceiling Fixture -r")
        self.assertTrue(entry["on"])

    def test_dimmable_bulb_reports_derived_watts(self):
        """A working bulb still gets brightness × rated draw, flagged derived so
        no total labelled "measured" can absorb it."""
        entry = self.read(Dev({"Brightness": Brightness(100)}))
        self.assertEqual(entry["watts"], 8.7)
        self.assertEqual(entry["brightness"], 100)
        self.assertTrue(entry["derived"])

    def test_bulb_off_is_zero_watts(self):
        entry = self.read(Dev({"Brightness": Brightness(100)}, is_on=False))
        self.assertEqual(entry["watts"], 0.0)

    def test_half_brightness_halves_the_draw(self):
        entry = self.read(Dev({"Brightness": Brightness(50)}))
        self.assertEqual(entry["watts"], 4.35)

    def test_usage_is_fetched_even_when_brightness_is_unreadable(self):
        """kWh comes from a separate query, so a missing Brightness module must
        not cost the bulb its cumulative energy too."""
        asked = []

        class UsageDev(Dev):
            async def _query_helper(self, method, params):
                asked.append(method)
                return {"get_device_usage": {"power_usage": {"today": 36, "past30": 1100}}}

        entry = self.read(UsageDev({"Light": RaisingLight()}))
        self.assertIn("get_device_usage", asked)
        self.assertEqual(entry["kwhToday"], 0.036)
        self.assertEqual(entry["kwhMonth"], 1.1)


if __name__ == "__main__":
    unittest.main()
