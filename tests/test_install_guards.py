#!/usr/bin/env python3
"""Tests for the key guard in bin/install-node-report.sh, install-backup-report.sh and install-ci-live-report.sh.

Each installer used to demand the legacy key file before it would write a timer. A host that has
moved to vault deletes that file, which is exactly what the README tells a person to do, so the
guard would then refuse to reinstall the service on the hosts furthest along the cutover. The guard
now takes either door.

Each installer is driven for real in a throwaway HOME, with a stub launchctl and a stub systemctl
first on PATH. No agent on this machine is loaded, read or written: the plist and the unit files
land under the temp HOME, and the real launchctl is never reached.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# name, script, the legacy file it accepts
INSTALLERS = [
    ("node-report", "install-node-report.sh", ".roost_node_key", "NODE_KEY"),
    ("backup-report", "install-backup-report.sh", ".roost_node_key", "NODE_KEY"),
    ("ci-live-report", "install-ci-live-report.sh", ".roost_ci_key", "CI_KEY"),
]

VAULT_RC = ("ROOST_VAULT_URL=https://vault.test\n"
            "ROOST_VAULT_APP=roost-test-node\n"
            "ROOST_VAULT_APP_KEY=app-key-2b1f9c\n")


class InstallGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-install-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        os.makedirs(self.home)
        os.makedirs(self.stub)
        # The real service managers must never be reached from a test.
        for name in ("launchctl", "systemctl"):
            path = os.path.join(self.stub, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/bash\nexit 0\n")
            os.chmod(path, 0o755)

    def write_rc(self, text):
        with open(os.path.join(self.home, ".roostrc"), "w") as fh:
            fh.write(text)

    def write_legacy(self, name):
        path = os.path.join(self.home, name)
        with open(path, "w") as fh:
            fh.write("a-key\n")
        os.chmod(path, 0o600)

    def install(self, script):
        env = dict(os.environ, HOME=self.home,
                   PATH=self.stub + os.pathsep + os.environ["PATH"])
        for name in ("ROOST_VAULT_URL", "ROOST_VAULT_APP", "ROOST_VAULT_APP_KEY"):
            env.pop(name, None)
        return subprocess.run(["bash", os.path.join(ROOT, "bin", script)],
                              env=env, capture_output=True, text=True, timeout=60)

    # -- the three arms --------------------------------------------------

    def installed_under_home(self):
        """Every unit this run wrote, relative to the throwaway HOME."""
        written = []
        for base, _, files in os.walk(self.home):
            for name in files:
                if name.endswith((".plist", ".service", ".timer")):
                    written.append(os.path.relpath(os.path.join(base, name), self.home))
        return written

    def test_the_legacy_file_still_installs(self):
        """The arm every host is on today, and it must keep working through the whole cutover."""
        for label, script, legacy, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.write_legacy(legacy)
                r = self.install(script)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue(self.installed_under_home(), "the installer wrote no unit")

    def test_the_vault_keys_install_with_no_legacy_file(self):
        """The arm a cut-over host is on, where the file the old guard demanded is gone."""
        for label, script, legacy, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.write_rc(VAULT_RC)
                self.assertFalse(os.path.exists(os.path.join(self.home, legacy)))
                r = self.install(script)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue(self.installed_under_home(), "the installer wrote no unit")

    def test_a_refusal_writes_nothing(self):
        for label, script, _, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.assertEqual(self.install(script).returncode, 1)
                self.assertEqual(self.installed_under_home(), [])

    def test_neither_door_refuses_and_names_both(self):
        for label, script, legacy, key in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                r = self.install(script)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(key, r.stderr)
                self.assertIn("ROOST_VAULT_", r.stderr)
                self.assertIn(legacy, r.stderr)

    # -- the guard is not fooled -----------------------------------------

    def test_a_partial_vault_config_refuses(self):
        """Two keys out of three is a half-edited rc, and installing on it would report with no key."""
        for label, script, _, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.write_rc("ROOST_VAULT_URL=https://vault.test\nROOST_VAULT_APP=roost-test-node\n")
                self.assertEqual(self.install(script).returncode, 1)

    def test_an_empty_vault_value_refuses(self):
        """A key present with no value is the shape a half-finished paste leaves behind."""
        for label, script, _, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.write_rc("ROOST_VAULT_URL=https://vault.test\n"
                              "ROOST_VAULT_APP=roost-test-node\n"
                              "ROOST_VAULT_APP_KEY=\n")
                self.assertEqual(self.install(script).returncode, 1)

    def test_a_commented_vault_key_refuses(self):
        """roostrc.example ships these three commented out, and a copied example configures nothing."""
        for label, script, _, _ in INSTALLERS:
            with self.subTest(label):
                self.setUp()
                self.write_rc("# ROOST_VAULT_URL=https://vault.test\n"
                              "# ROOST_VAULT_APP=roost-test-node\n"
                              "# ROOST_VAULT_APP_KEY=app-key-2b1f9c\n")
                self.assertEqual(self.install(script).returncode, 1)

    def test_no_real_service_manager_was_reached(self):
        """The stubs are first on PATH, so a passing arm never touches this machine's agents."""
        self.write_legacy(".roost_node_key")
        env = dict(os.environ, HOME=self.home,
                   PATH=self.stub + os.pathsep + os.environ["PATH"])
        which = subprocess.run(["bash", "-c", "command -v launchctl; command -v systemctl"],
                               env=env, capture_output=True, text=True)
        for line in which.stdout.split():
            self.assertTrue(line.startswith(self.stub), line)


if __name__ == "__main__":
    unittest.main()
