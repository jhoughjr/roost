#!/usr/bin/env python3
"""Tests for bin/box-watch.py: the vote each box works out itself, and two watchers that see each other.

The vote is pure functions, so the rules are tested with no network. Two watchers are then started on loopback,
each naming the other as its peer, and one is stopped, to prove a probe, an answer and the turn to unreachable.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "box-watch.py")
spec = importlib.util.spec_from_file_location("box_watch", SCRIPT)
box_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box_watch)


class VoteTest(unittest.TestCase):
    def test_one_dropped_probe_is_stale_and_three_intervals_is_unreachable(self):
        self.assertEqual(box_watch.peer_state(True, 100.0, 100.0, 30), "up")
        self.assertEqual(box_watch.peer_state(False, 100.0, 150.0, 30), "stale")
        self.assertEqual(box_watch.peer_state(False, 100.0, 191.0, 30), "unreachable")
        self.assertEqual(box_watch.peer_state(False, None, 100.0, 30), "unreachable")

    def test_votes_take_my_word_and_each_peer_document_and_never_the_box_itself(self):
        mine = {"mini": {"state": "unreachable"}, "milo": {"state": "up"}}
        documents = {"milo": {"peers": {"mini": {"state": "unreachable"}, "opi": {"state": "up"}}}}
        self.assertEqual(box_watch.votes_on("mini", "opi", mine, documents), {"opi": "unreachable", "milo": "unreachable"})
        self.assertEqual(box_watch.votes_on("milo", "opi", mine, documents), {"opi": "up"})

    def test_a_majority_of_counted_voters_decides_and_a_stale_vote_does_not_count(self):
        self.assertEqual(box_watch.agreed({"a": "unreachable", "b": "unreachable"}), "down")
        self.assertEqual(box_watch.agreed({"a": "up", "b": "unreachable"}), "split")
        self.assertEqual(box_watch.agreed({"a": "up", "b": "stale"}), "up")
        self.assertEqual(box_watch.agreed({"a": "stale"}), "split")
        self.assertEqual(box_watch.agreed({"a": "unreachable"}), "down")

    def test_the_lowest_named_voter_that_sees_the_fault_speaks(self):
        self.assertEqual(box_watch.alerter({"opi": "unreachable", "mini": "unreachable"}), "mini")
        self.assertEqual(box_watch.alerter({"opi": "unreachable", "mini": "up"}), "opi")
        self.assertIsNone(box_watch.alerter({"opi": "up"}))

    def test_peers_parse_from_the_declaration_line(self):
        self.assertEqual(box_watch.parse_peers("mini=mini.example:9211, opi=opi.example:9211"),
                         {"mini": "mini.example:9211", "opi": "opi.example:9211"})


class TwoWatchersTest(unittest.TestCase):
    def start(self, name, port, peer, peer_port, home):
        env = dict(os.environ, HOME=home, BOX_WATCH_NAME=name, BOX_WATCH_PORT=str(port), BOX_WATCH_INTERVAL="1",
                   BOX_WATCH_PEERS=f"{peer}=127.0.0.1:{peer_port}", BOX_WATCH_PULSE="http://127.0.0.1:9", BOX_WATCH_STATE=os.path.join(home, "state"))
        return subprocess.Popen([sys.executable, SCRIPT], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def view(self, port, key):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/view", headers={"x-roost-node-key": key})
        return json.loads(urllib.request.urlopen(request, timeout=5).read())

    def test_two_watchers_see_each_other_then_one_stops_and_the_other_says_unreachable(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, ".roost_node_key"), "w") as file:
                file.write("k")
            a = self.start("a", 19311, "b", 19312, home)
            b = self.start("b", 19312, "a", 19311, home)
            try:
                deadline = time.time() + 15
                while time.time() < deadline:
                    try:
                        if self.view(19311, "k")["peers"].get("b", {}).get("state") == "up":
                            break
                    except OSError:
                        pass
                    time.sleep(0.5)
                seen = self.view(19311, "k")
                self.assertEqual(seen["voter"], "a")
                self.assertEqual(seen["intervalS"], 1)
                self.assertEqual(seen["peers"]["b"]["state"], "up")
                # A probe with the wrong key is refused.
                with self.assertRaises(urllib.error.HTTPError):
                    self.view(19311, "wrong")
                b.terminate(); b.wait(timeout=5)
                deadline = time.time() + 20
                state = ""
                while time.time() < deadline:
                    state = self.view(19311, "k")["peers"]["b"]["state"]
                    if state == "unreachable":
                        break
                    time.sleep(0.5)
                self.assertEqual(state, "unreachable")
                said = json.load(open(os.path.join(home, "state", "said.json")))
                self.assertEqual(said.get("b"), "down")
                self.assertTrue(said.get("_alone"))
            finally:
                for process in (a, b):
                    if process.poll() is None:
                        process.terminate()


if __name__ == "__main__":
    unittest.main()
