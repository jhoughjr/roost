#!/usr/bin/env python3
"""Tests for bin/gen-narrative.py — the board-narrative generator.

Covers the pure composition (PR list → one-line narrative) and title cleanup;
the gh call is not exercised (network).
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "gen_narrative", os.path.join(ROOT, "bin", "gen-narrative.py"))
gn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gn)


class ComposeTest(unittest.TestCase):
    def test_empty_prs_say_nothing(self):
        self.assertEqual(gn.compose_narrative([], "2026-07-16", "dev"), "")

    def test_orders_by_merge_number_not_input_order(self):
        prs = [{"number": 76, "title": "feat: c"}, {"number": 73, "title": "feat: a"},
               {"number": 75, "title": "feat: b"}]
        out = gn.compose_narrative(prs, "2026-07-16", "dev")
        self.assertEqual(out.index("#73"), out.index("#73"))
        self.assertLess(out.index("#73"), out.index("#75"))
        self.assertLess(out.index("#75"), out.index("#76"))

    def test_shape_and_count(self):
        prs = [{"number": 73, "title": "feat: composed list status"}]
        out = gn.compose_narrative(prs, "2026-07-16", "dev", label="Phoenix")
        self.assertEqual(out, "2026-07-16: Phoenix merged 1 PR to dev — #73 composed list status.")

    def test_pluralizes(self):
        prs = [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}]
        self.assertIn("merged 2 PRs to dev", gn.compose_narrative(prs, "2026-07-16", "dev"))

    def test_strips_conventional_prefix_and_trailing_pr_number(self):
        self.assertEqual(gn._short_title("feat: per-line landing (#76)"), "per-line landing")
        self.assertEqual(gn._short_title("fix: don't refresh (#74)"), "don't refresh")
        self.assertEqual(gn._short_title("plain title"), "plain title")

    # The whole point: a real merge set composes a current narrative, so the
    # banner never has to be hand-written to stay fresh.
    def test_composes_a_real_session_merge_set(self):
        prs = [
            {"number": 73, "title": "feat: composed list status + dual identity (#73)"},
            {"number": 75, "title": "feat: core polish — Print verb + PO vendor language (#75)"},
            {"number": 76, "title": "feat: per-line landing (#76)"},
        ]
        out = gn.compose_narrative(prs, "2026-07-16", "dev", label="Phoenix")
        self.assertTrue(out.startswith("2026-07-16: Phoenix merged 3 PRs to dev — "))
        self.assertIn("#73 composed list status + dual identity;", out)
        self.assertIn("#76 per-line landing.", out)
        self.assertNotIn("(#73)", out)  # trailing PR number trimmed


if __name__ == "__main__":
    unittest.main()
