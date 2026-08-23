#!/usr/bin/env python3
"""The config verb: reads stay, writes hand off to hatchery.

roost#15 recorded the disagreement: a value set through dokku directly is invisible
to the declaration, and tofu plan starts describing something that is no longer
true. So `roost config <app> K=V` refuses and names the hatchery command, and
`--force` keeps the old direct write for emergencies, with the audit note after.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOST = os.path.join(ROOT, "bin", "roost")


class ConfigVerbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-config-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path_dir = os.path.join(self.tmp, "pathbin")
        os.makedirs(self.path_dir)
        # A fake ssh, so the test sees exactly what would reach the box.
        fake = os.path.join(self.path_dir, "ssh")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\necho SSH \"$@\"\n")
        os.chmod(fake, 0o755)

    def roost(self, *args):
        env = dict(os.environ)
        env["PATH"] = self.path_dir + os.pathsep + os.environ["PATH"]
        env["ROOST_DOKKU_HOST"] = "dokku@box.test"
        return subprocess.run(["bash", ROOST, *args], env=env,
                              capture_output=True, text=True)

    def test_a_read_still_goes_to_the_box(self):
        r = self.roost("config", "blog")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("config:show blog", r.stdout)

    def test_a_write_refuses_and_names_the_hatchery_command(self):
        r = self.roost("config", "blog", "LOG_LEVEL=debug")
        self.assertEqual(r.returncode, 64)
        self.assertIn("hatchery config set <stack> blog KEY=VALUE", r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertNotIn("SSH", r.stdout)

    def test_force_runs_the_old_write_and_says_to_audit(self):
        r = self.roost("config", "blog", "--force", "LOG_LEVEL=debug")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("config:set blog LOG_LEVEL=debug", r.stdout)
        self.assertIn("hatchery config audit", r.stderr)

    def test_force_with_no_values_is_a_usage_error(self):
        r = self.roost("config", "blog", "--force")
        self.assertEqual(r.returncode, 64)
        self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
