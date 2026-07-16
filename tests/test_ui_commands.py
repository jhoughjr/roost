#!/usr/bin/env python3
"""Tests for bin/roost-ui.py — the console's pure logic.

The drawing needs a real terminal, so these cover what can be checked without
one: the slash menu's filtering and acceptance, the ⏺/⎿ transcript shape, and
the palette lookups. UI instances are built with __new__ to skip the curses
handle and the app-listing thread that __init__ would start.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "roost_ui", os.path.join(ROOT, "bin", "roost-ui.py"))
ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui)


def mkui():
    """A UI with just the fields the pure paths touch."""
    u = ui.UI.__new__(ui.UI)
    u.h, u.w = 24, 80
    u.transcript = []
    u.buf = ""
    u.cur = 0
    u.menu_sel = 0
    u.result_open = False
    return u


class MenuTest(unittest.TestCase):
    def test_every_command_has_a_blurb(self):
        # The menu renders a description per row; a missing one ships blank.
        self.assertEqual(sorted(ui.CMD_DESC), sorted(ui.ALL_CMDS))

    def test_menu_filters_on_the_fragment(self):
        u = mkui()
        u.buf = "/st"
        self.assertEqual([c for c, _ in u.menu_items()],
                         ["start", "stats", "status"])

    def test_menu_closed_without_a_slash_or_once_an_arg_starts(self):
        u = mkui()
        for buf in ("", "st", "apps", "/logs pulse"):
            u.buf = buf
            self.assertEqual(u.menu_items(), [], f"menu open for {buf!r}")

    def test_bare_slash_lists_everything(self):
        u = mkui()
        u.buf = "/"
        self.assertEqual(len(u.menu_items()), len(ui.ALL_CMDS))

    def test_accept_fills_the_buffer(self):
        u = mkui()
        u.buf = "/stat"
        u.menu_sel = 1                            # start,stats,status -> stats
        self.assertEqual([c for c, _ in u.menu_items()], ["stats", "status"])
        self.assertTrue(u.menu_accept())
        self.assertEqual(u.buf, "/status")
        self.assertEqual(u.cur, len(u.buf))

    def test_accept_leaves_room_for_an_app_argument(self):
        u = mkui()
        u.buf = "/logs"
        self.assertTrue(u.menu_accept())
        self.assertEqual(u.buf, "/logs ")         # trailing space: wants an app

    def test_accept_is_a_noop_with_no_matches(self):
        u = mkui()
        u.buf = "/zzz"
        self.assertFalse(u.menu_accept())
        self.assertEqual(u.buf, "/zzz")


class TranscriptTest(unittest.TestCase):
    def test_command_echoes_as_a_bullet(self):
        u = mkui()
        u.say_cmd("apps")
        self.assertEqual(u.transcript, [("o", "⏺ apps")])
        self.assertTrue(u.result_open)

    def test_output_opens_a_gutter_then_indents(self):
        u = mkui()
        u.say_cmd("apps")
        u.say_out("pulse")
        u.say_out("vault")
        self.assertEqual([t for _, t in u.transcript[1:]],
                         ["  ⎿ pulse", "    vault"])

    def test_long_output_wraps_under_the_gutter(self):
        u = mkui()
        u.w = 30
        u.say_cmd("logs")
        u.say_out("x" * 40)
        rows = [t for _, t in u.transcript[1:]]
        self.assertTrue(rows[0].startswith("  ⎿ "))
        self.assertTrue(rows[1].startswith("      "))   # continuation indent
        self.assertEqual("".join(r.lstrip(" ⎿") for r in rows), "x" * 40)

    def test_bullet_recolours_on_exit(self):
        u = mkui()
        u.say_cmd("apps")
        u.say_out("pulse")
        u.mark_bullet("g")
        self.assertEqual(u.transcript[0], ("g", "⏺ apps"))

    def test_mark_bullet_targets_the_latest_command(self):
        u = mkui()
        u.say_cmd("apps")
        u.say_cmd("fleet")
        u.mark_bullet("r")
        self.assertEqual(u.transcript[0][0], "o")     # earlier one untouched
        self.assertEqual(u.transcript[1], ("r", "⏺ fleet"))

    def test_mark_bullet_survives_an_empty_transcript(self):
        mkui().mark_bullet("g")                       # must not raise


class PaletteTest(unittest.TestCase):
    def test_load_attr_thresholds(self):
        self.assertEqual(ui.load_attr(0.1), "g")
        self.assertEqual(ui.load_attr(0.5), "y")
        self.assertEqual(ui.load_attr(1.0), "r")
        self.assertEqual(ui.load_attr(4.0), "r")

    def test_palettes_cover_the_same_keys(self):
        self.assertEqual(sorted(ui.PALETTE_256), sorted(ui.PALETTE_8))

    def test_work_word_is_stable_per_label(self):
        self.assertEqual(ui.work_word("status"), ui.work_word("status"))
        self.assertIn(ui.work_word("status"), ui.WORK_WORDS)
        self.assertIn(ui.work_word(""), ui.WORK_WORDS)


if __name__ == "__main__":
    unittest.main()
