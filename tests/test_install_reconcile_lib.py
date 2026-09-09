#!/usr/bin/env python3
"""Tests that the installed dokku-reconcile carries the secret reader it sources.

install-dokku-reconcile.sh copies the script out of the repo into its own directory, and the
script began sourcing lib/roost-secret.sh for NODE_KEY. A copy with no reader beside it sources
a file that is not there, so the pass reports nothing and says so only in the journal, every ten
minutes. The installer copies the reader now, and the script looks in its own directory first.

The installer is driven for real into a throwaway HOME with a stub systemctl first on PATH.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReconcileInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-reconcile-install-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        os.makedirs(self.home)
        os.makedirs(self.stub)
        # The real service manager must never be reached from a test.
        for name in ("systemctl", "loginctl"):
            path = os.path.join(self.stub, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/bash\nexit 0\n")
            os.chmod(path, 0o755)
        self.dest = os.path.join(self.home, "opt", "dokku-reconcile")
        env = dict(os.environ, HOME=self.home, DEST=self.dest,
                   PATH=self.stub + os.pathsep + os.environ["PATH"])
        self.run = subprocess.run(
            ["bash", os.path.join(ROOT, "bin", "install-dokku-reconcile.sh")],
            env=env, capture_output=True, text=True, timeout=60)

    def test_the_installer_lands_the_reader_beside_the_script(self):
        self.assertEqual(self.run.returncode, 0, self.run.stdout + self.run.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "dokku-reconcile.sh")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "lib", "roost-secret.sh")),
                        "the installed copy has no reader to source")

    def test_the_installed_copy_resolves_its_reader(self):
        """Source the installed script's own resolution and ask for the function it needs."""
        script = os.path.join(self.dest, "dokku-reconcile.sh")
        head = []
        for line in open(script):
            head.append(line)
            if line.startswith('. "$LIB/roost-secret.sh"'):
                break
        probe = os.path.join(self.tmp, "probe.sh")
        with open(probe, "w") as fh:
            fh.write("".join(head).replace("${BASH_SOURCE[0]}", script))
            fh.write('\ntype -t roost_secret\n')
        r = subprocess.run(["bash", probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "function")


    def test_the_failure_alert_is_wired_where_systemd_reads_it(self):
        """OnFailure in [Service] is ignored with one journal line, so the alert never fires."""
        unit = os.path.join(self.home, ".config", "systemd", "user", "dokku-reconcile.service")
        section = None
        placed = {}
        for line in open(unit):
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line
            elif line.startswith("OnFailure="):
                placed[section] = line
        self.assertIn("[Unit]", placed, "OnFailure is not in the section systemd reads")
        self.assertNotIn("[Service]", placed)
        self.assertIn("dokku-reconcile-alert.service", placed["[Unit]"])


if __name__ == "__main__":
    unittest.main()
