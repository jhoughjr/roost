#!/usr/bin/env python3
"""Tests for bin/fleet-board.py, which draws the fleet board from what the box reported to pulse.

It had none before it stopped collecting over ssh. The rows it draws are the
estate's front page, so the shape of one is worth pinning.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module():
    """Import the collector by path, because bin/ is not a package and the file carries a hyphen."""
    spec = importlib.util.spec_from_file_location("fleet_board", os.path.join(ROOT, "bin", "fleet-board.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FLEET = load_module()


def answered(**overrides):
    """One app as the reconcile reports it."""
    row = {
        "name": "vault", "names": ["vault.jimmyhoughjr.net"], "served": True,
        "running": True, "image": True, "memMb": 42.4, "procs": 1,
        "httpCode": "200", "createdAt": "2026-09-01",
    }
    row.update(overrides)
    return row


class WhichRowsAreApps(unittest.TestCase):
    def test_an_app_owns_names(self):
        self.assertTrue(FLEET.is_app(answered()))

    def test_a_job_and_a_database_name_their_kind(self):
        self.assertFalse(FLEET.is_app({"name": "opi-backup", "kind": "job", "names": []}))
        self.assertFalse(FLEET.is_app({"name": "rookery-pg/rookery", "kind": "database"}))

    def test_a_container_outside_dokku_owns_no_vhost(self):
        self.assertFalse(FLEET.is_app({"name": "lan-dns", "names": [], "running": True}))


class TheRowAnAppDraws(unittest.TestCase):
    def setUp(self):
        FLEET.EXPECTED = {}
        # The probe is the one thing still asked directly, so a case says what it answered.
        self._probe = FLEET.http_check
        FLEET.http_check = lambda fqdn: self.code
        self.code = "200"
        self.addCleanup(lambda: setattr(FLEET, "http_check", self._probe))

    def test_a_serving_app_reads_up_and_carries_what_the_box_measured(self):
        row, running, healthy, mb = FLEET.collect_app(answered())

        self.assertEqual(row["pill"]["text"], "up")
        self.assertTrue(running)
        self.assertTrue(healthy)
        self.assertAlmostEqual(mb, 42.4, places=1)
        self.assertIn("42 MB", row["note"])
        self.assertIn("1 proc", row["note"])
        self.assertIn("container since 2026-09-01", row["note"])

    def test_a_redirect_is_a_healthy_answer(self):
        # Every app that sends callers to https answers 301 or 302 at the box, and calling those degraded
        # made four correctly serving apps read as broken.
        self.code = "302"
        _, _, healthy, _ = FLEET.collect_app(answered())

        self.assertTrue(healthy)

    def test_a_404_is_a_fault_until_a_person_names_it(self):
        self.code = "404"
        _, _, healthy, _ = FLEET.collect_app(answered())
        self.assertFalse(healthy)

        FLEET.EXPECTED = {"vault": "404"}
        _, _, healthy, _ = FLEET.collect_app(answered())
        self.assertTrue(healthy)

    def test_an_app_that_is_not_running_reads_down(self):
        self.code = "000"
        row, running, healthy, _ = FLEET.collect_app(answered(running=False))

        self.assertEqual(row["pill"]["text"], "down")
        self.assertFalse(running)
        self.assertFalse(healthy)

    def test_a_running_app_that_answers_badly_reads_degraded(self):
        self.code = "502"
        row, _, healthy, _ = FLEET.collect_app(answered())

        self.assertEqual(row["pill"]["text"], "degraded")
        self.assertFalse(healthy)

    def test_the_public_name_is_preferred_over_the_first_one(self):
        row, _, _, _ = FLEET.collect_app(
            answered(names=["vault.opi", "vault.jimmyhoughjr.net"]))

        self.assertEqual(row["q"], "vault.jimmyhoughjr.net")

    def test_an_app_the_box_measured_nothing_for_says_nothing_about_memory(self):
        row, _, _, mb = FLEET.collect_app(answered(memMb=None))

        self.assertNotIn("MB", row["note"])
        self.assertEqual(mb, 0.0)


class TheHostFigures(unittest.TestCase):
    def test_they_come_from_the_box_that_runs_the_apps(self):
        nodes = [
            {"name": "jimmys-mac-mini", "memUsedMb": 1, "memTotalMb": 100, "diskUsedMb": 1, "diskTotalMb": 100, "load1": 9.0},
            {"name": "opi", "memUsedMb": 40, "memTotalMb": 200, "diskUsedMb": 30, "diskTotalMb": 50, "load1": 1.5},
        ]

        self.assertEqual(FLEET.host_metrics(nodes), ("20%", "60%", "1.50"))

    def test_a_node_that_has_not_reported_leaves_the_figures_unknown(self):
        self.assertEqual(FLEET.host_metrics([]), ("?", "?", "?"))

    def test_a_node_that_reported_no_storage_says_so_rather_than_zero(self):
        nodes = [{"name": "opi", "memUsedMb": 40, "memTotalMb": 200, "diskUsedMb": 0, "diskTotalMb": 0, "load1": 1.0}]

        self.assertEqual(FLEET.host_metrics(nodes)[1], "?")


if __name__ == "__main__":
    unittest.main()
