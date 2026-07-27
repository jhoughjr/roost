#!/usr/bin/env python3
"""Tests for bin/roost's plugin dispatch and bin/roost-env.sh's defaults.

Dispatch (roost#14): an unrecognized command becomes `roost-<cmd>`, looked
up on PATH first and then in $ROOST_STATUSGEN/bin — so statusgen can ship
board-shaped commands without roost knowing its internals.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOST = os.path.join(ROOT, "bin", "roost")
ENV_SH = os.path.join(ROOT, "bin", "roost-env.sh")


class DispatchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-dispatch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        # An empty statusgen tree; individual tests add roost-* entrypoints.
        self.sgen_bin = os.path.join(self.tmp, "statusgen", "bin")
        os.makedirs(self.sgen_bin)
        self.path_dir = os.path.join(self.tmp, "pathbin")
        os.makedirs(self.path_dir)

    def plugin(self, directory, name, body="#!/usr/bin/env bash\necho PLUGIN \"$@\"\n"):
        p = os.path.join(directory, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p

    def roost(self, *args, env=None):
        e = dict(os.environ)
        e.update({"HOME": self.home,
                  "ROOST_STATUSGEN": os.path.join(self.tmp, "statusgen"),
                  "PATH": self.path_dir + os.pathsep + os.environ["PATH"]})
        if env:
            e.update(env)
        return subprocess.run(["bash", ROOST, *args], env=e,
                              capture_output=True, text=True)


class PluginDispatchTest(DispatchFixture):
    def test_unknown_command_runs_a_path_plugin(self):
        self.plugin(self.path_dir, "roost-frobnicate")
        r = self.roost("frobnicate", "--flag", "arg")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "PLUGIN --flag arg")

    def test_statusgen_bin_is_searched_when_path_misses(self):
        self.plugin(self.sgen_bin, "roost-board",
                    "#!/usr/bin/env bash\necho FROM-STATUSGEN\n")
        r = self.roost("board")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "FROM-STATUSGEN")

    def test_path_wins_over_statusgen(self):
        self.plugin(self.path_dir, "roost-dup",
                    "#!/usr/bin/env bash\necho FROM-PATH\n")
        self.plugin(self.sgen_bin, "roost-dup",
                    "#!/usr/bin/env bash\necho FROM-STATUSGEN\n")
        r = self.roost("dup")
        self.assertEqual(r.stdout.strip(), "FROM-PATH")

    def test_plugin_exit_code_propagates(self):
        self.plugin(self.path_dir, "roost-fails",
                    "#!/usr/bin/env bash\nexit 3\n")
        self.assertEqual(self.roost("fails").returncode, 3)

    def test_unknown_with_no_plugin_is_a_clear_error(self):
        r = self.roost("definitely-not-a-command")
        # 2 = usage error, distinct from a plugin's own failure.
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown command", r.stderr)
        self.assertIn("roost-definitely-not-a-command", r.stderr)

    def test_builtin_still_beats_a_shadowing_plugin(self):
        # A stray roost-status on PATH must not hijack the real orchestrator.
        self.plugin(self.path_dir, "roost-status",
                    "#!/usr/bin/env bash\necho HIJACKED\n")
        r = self.roost("status")
        self.assertNotIn("HIJACKED", r.stdout)

    def test_bare_roost_still_prints_help(self):
        r = self.roost()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("one command for the whole platform", r.stdout)
        self.assertIn("roost rollout", r.stdout)
        self.assertIn("roost-<cmd>", r.stdout)


class EnvDefaultsTest(DispatchFixture):
    def source_env(self, rc_text=None, preset=None):
        """Source roost-env.sh with a fake HOME and print the resolved values."""
        if rc_text is not None:
            with open(os.path.join(self.home, ".roostrc"), "w") as f:
                f.write(rc_text)
        e = dict(os.environ, HOME=self.home)
        if preset:
            e.update(preset)
        script = f'. "{ENV_SH}"; echo "$DOKKU|$DOMAIN|$ROOST_STATUS_RUNNER"'
        r = subprocess.run(["bash", "-c", script], env=e,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().split("|")

    def test_defaults_apply_with_no_rc(self):
        dokku, domain, runner = self.source_env()
        self.assertEqual(dokku, "dokku@192.168.0.103")
        self.assertEqual(domain, "jimmyhoughjr.net")
        self.assertTrue(runner)

    def test_rc_overrides_defaults(self):
        dokku, domain, _ = self.source_env(
            "ROOST_DOKKU_HOST=dokku@10.0.0.9\nROOST_DOMAIN=example.test\n")
        self.assertEqual(dokku, "dokku@10.0.0.9")
        self.assertEqual(domain, "example.test")

    def test_precedence_is_rc_then_environment_then_default(self):
        # Sourcing ~/.roostrc does plain assignments, so a key PRESENT in the
        # rc overrides an exported value — long-standing behavior, preserved
        # here deliberately. The := only fills gaps, so a key ABSENT from the
        # rc falls through to the environment before the built-in default.
        _, domain, _ = self.source_env("ROOST_DOMAIN=from-rc.test\n",
                                       preset={"ROOST_DOMAIN": "from-env.test"})
        self.assertEqual(domain, "from-rc.test", "rc must win over the env")

        dokku, _, _ = self.source_env("ROOST_DOMAIN=from-rc.test\n",
                                      preset={"ROOST_DOKKU_HOST": "dokku@env"})
        self.assertEqual(dokku, "dokku@env",
                         "env must win over the built-in default")


if __name__ == "__main__":
    unittest.main()
