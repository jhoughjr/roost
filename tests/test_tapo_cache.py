#!/usr/bin/env python3
"""Tests for the tapo cache contract between tapo-poll.py and node-report.sh.

These two talk through ~/.roost-tapo.json and nothing type-checks the seam:
node-report.sh greps the file with a plain ERE because the opi has no jq. The
first deploy shipped broken because json.dump's default separators write
`"fleetWatts": 4.2` (space after the colon) while the pattern demanded
`"fleetWatts":4.2` — so every reading was silently dropped and the opi kept
showing the estimate. These lock both halves down.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_REPORT = os.path.join(ROOT, "bin", "node-report.sh")
TAPO_POLL = os.path.join(ROOT, "bin", "tapo-poll.py")


def patterns():
    """The two grep -oE patterns node-report.sh uses to read the cache."""
    with open(NODE_REPORT) as f:
        src = f.read()
    found = dict(re.findall(r"""grep -oE '"(\w+)": \*(\[[^']+\]\+)'""", src))
    return found


def extract(pattern_body, key, text):
    """Reproduce the shell pipeline: grep -oE … | head -1 | tr -dc …"""
    m = re.search(f'"{key}": *{pattern_body}', text)
    if not m:
        return ""
    keep = "0123456789." if "." in pattern_body else "0123456789"
    return "".join(c for c in m.group(0) if c in keep)


class TapoCacheContract(unittest.TestCase):
    def setUp(self):
        self.pats = patterns()

    def test_node_report_reads_both_keys(self):
        """It must pull the timestamp and the fleet watts, or freshness and the
        reading itself can't both be checked."""
        self.assertIn("t", self.pats, "node-report.sh lost its cache `t` grep")
        self.assertIn("fleetWatts", self.pats,
                      "node-report.sh lost its cache `fleetWatts` grep")

    def test_matches_compact_and_spaced_json(self):
        """Compact is what tapo-poll writes; spaced is what a human or any
        default json.dump produces. Both must parse — the bug was rejecting
        spaced, and hard-coding compact alone would just re-arm it."""
        payload = {"t": 1785718657, "fleetLabel": "opi", "fleetWatts": 4.2}
        for label, text in (
            ("compact", json.dumps(payload, separators=(",", ":"))),
            ("spaced", json.dumps(payload)),
            ("indented", json.dumps(payload, indent=2)),
        ):
            with self.subTest(style=label):
                self.assertEqual(extract(self.pats["t"], "t", text), "1785718657")
                self.assertEqual(
                    extract(self.pats["fleetWatts"], "fleetWatts", text), "4.2")

    def test_null_fleet_watts_yields_nothing(self):
        """A plug that didn't report must fall through to the idleW/maxW
        estimate, not smuggle a bare `null` into the POST body."""
        text = json.dumps({"t": 1785718657, "fleetWatts": None},
                          separators=(",", ":"))
        self.assertEqual(extract(self.pats["fleetWatts"], "fleetWatts", text), "")
        self.assertEqual(extract(self.pats["t"], "t", text), "1785718657")

    def test_tapo_poll_writes_compact(self):
        """Belt to node-report's braces: keep the writer compact so the file
        stays small on a 30 s cadence and the grep has the easiest job."""
        with open(TAPO_POLL) as f:
            src = f.read()
        self.assertIn('separators=(",", ":")', src,
                      "tapo-poll.py must json.dump the cache compactly")

    def test_scripts_are_syntactically_valid(self):
        subprocess.run(["bash", "-n", NODE_REPORT], check=True)
        subprocess.run(["python3", "-m", "py_compile", TAPO_POLL], check=True)


if __name__ == "__main__":
    unittest.main()
