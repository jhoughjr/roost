#!/usr/bin/env python3
"""Tests for lib/roost-collector.sh — the half that says which collectors ran.

Every collector is non-fatal by contract, so a broken one used to look exactly
like one with nothing to say. These drive the real shell helper and assert on
the record it writes.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib", "roost-collector.sh")


class CollectorRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-collector-test-")
        self.log = os.path.join(self.tmp, "collectors.json")

    def run_script(self, body):
        script = f'set -euo pipefail\nexport ROOST_COLLECTOR_LOG="{self.log}"\n. "{LIB}"\n{body}\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    def record(self):
        with open(self.log) as fh:
            return json.load(fh)

    def runs(self):
        return {r["name"]: r for r in self.record()["runs"]}

    # ── what a pass records ──────────────────────────────────────────────

    def test_a_collector_that_worked_is_recorded_as_ok(self):
        result = self.run_script("collector good true\ncollector_report")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        good = self.runs()["good"]
        self.assertTrue(good["ok"])
        self.assertNotIn("exit", good)
        self.assertGreaterEqual(good["ms"], 0)

    def test_a_failure_carries_its_exit_code_and_still_says_the_note(self):
        result = self.run_script("collector bad sh -c 'exit 3'\ncollector_report")

        # Non-fatal by contract: one broken collector must not stop the nineteen behind it.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note: bad failed (non-fatal)", result.stdout)
        bad = self.runs()["bad"]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["exit"], 3)

    def test_a_second_pass_merges_rather_than_replacing(self):
        # `roost stats` and `status.sh` each run their own collectors, and neither is the whole list.
        self.run_script("collector first true\ncollector_report")
        self.run_script("collector second true\ncollector_report")

        self.assertEqual(sorted(self.runs()), ["first", "second"])

    def test_a_later_run_of_the_same_collector_replaces_its_row(self):
        self.run_script("collector same sh -c 'exit 1'\ncollector_report")
        self.run_script("collector same true\ncollector_report")

        same = self.runs()["same"]
        self.assertTrue(same["ok"])
        self.assertNotIn("exit", same)

    def test_a_pass_that_ran_nothing_writes_nothing(self):
        result = self.run_script("collector_report")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_a_torn_record_is_replaced_rather_than_fatal(self):
        with open(self.log, "w") as fh:
            fh.write("{not json")

        result = self.run_script("collector good true\ncollector_report")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(self.runs()), ["good"])


if __name__ == "__main__":
    unittest.main()
