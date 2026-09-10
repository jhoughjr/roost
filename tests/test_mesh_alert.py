#!/usr/bin/env python3
"""Tests for bin/mesh-alert.sh, the one alert path that does not share a fate with the network.

A stand-in CLI prints what the real meshtastic CLI prints for each answer. The
real CLI exits 0 whatever the answer is, so the script has to read its words,
and a NAK must leave the quiet-hours state alone so the next pass sends again.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "mesh-alert.sh")


class MeshAlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-mesh-test-")
        self.state_home = os.path.join(self.tmp, "state")
        os.makedirs(self.state_home)
        # The script runs on the opi, where coreutils has `timeout`. A Mac has none, so the tests carry one that runs
        # the rest of its command, the way the other roost tests stand in for the box's commands.
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        shim = os.path.join(self.bin, "timeout")
        with open(shim, "w") as fh:
            fh.write('#!/usr/bin/env bash\nshift\nexec "$@"\n')
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)

    def cli(self, prints):
        """A stand-in meshtastic CLI that prints `prints` and exits 0, as the real one does."""
        path = os.path.join(self.tmp, "meshtastic")
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env bash\nprintf '%s\\n' " + repr(prints) + "\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def run_script(self, prints, message="opi: status has no image"):
        env = dict(os.environ)
        env.update({
            "MESH_CLI": self.cli(prints),
            "MESH_DEST": "!a0cce924",
            # Any readable path passes the device check. The stand-in never opens it.
            "MESH_PORT": "/dev/null",
            "XDG_STATE_HOME": self.state_home,
            "PATH": self.bin + os.pathsep + env["PATH"],
        })
        return subprocess.run(["bash", SCRIPT, message], env=env, capture_output=True, text=True, timeout=60)

    def state_written(self):
        return os.path.exists(os.path.join(self.state_home, "mesh-alert.last"))

    def test_an_ack_is_reported_as_delivered(self):
        result = self.run_script("Received an ACK.")

        self.assertEqual(result.returncode, 0)
        self.assertIn("delivered to !a0cce924, acknowledged", result.stderr)
        self.assertTrue(self.state_written())

    def test_a_nak_is_reported_and_sent_again_next_pass(self):
        # On 2026-09-10 the script said "sent" for messages the destination never received.
        result = self.run_script("Received a NAK, error reason: MAX_RETRANSMIT")

        self.assertEqual(result.returncode, 0)
        self.assertIn("did not acknowledge (MAX_RETRANSMIT)", result.stderr)
        # No state, so the quiet hours do not hold back an alert that never arrived.
        self.assertFalse(self.state_written())

    def test_a_relay_without_confirmation_says_so(self):
        result = self.run_script("Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed.")

        self.assertIn("relayed toward !a0cce924, delivery not confirmed", result.stderr)
        self.assertTrue(self.state_written())

    def test_silence_from_the_radio_is_not_called_a_send(self):
        result = self.run_script("")

        self.assertIn("the radio gave no answer", result.stderr)
        self.assertFalse(self.state_written())

    def test_an_acknowledged_message_is_held_back_when_repeated(self):
        self.run_script("Received an ACK.")

        result = self.run_script("Received an ACK.")

        self.assertIn("held back", result.stderr)

    def test_a_refused_message_is_not_held_back_when_repeated(self):
        self.run_script("Received a NAK, error reason: MAX_RETRANSMIT")

        result = self.run_script("Received a NAK, error reason: MAX_RETRANSMIT")

        self.assertNotIn("held back", result.stderr)
        self.assertIn("did not acknowledge", result.stderr)


if __name__ == "__main__":
    unittest.main()
