#!/usr/bin/env python3
"""Tests for bin/roost-prune.py — the artifact pruner.

Because this tool DELETES, the tests pin the safety-critical behavior: dry-run
removes nothing, --yes removes only the artifact dir (never source or .git),
node_modules needs --deep, and a project filter scopes the work. All against a
throwaway ROOST_PROJECTS_DIR; global caches are never exercised with --yes.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "roost-prune.py")


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prune-test-")
        # two fake repos; only dirs with a .git count as projects
        self._mk("alpha/.git/HEAD", "ref")
        self._mk("alpha/src/main.js", "source — must survive")
        self._mk("alpha/out/bundle.js", "x" * 4096)
        self._mk("alpha/node_modules/dep/i.js", "y" * 4096)
        self._mk("beta/.git/HEAD", "ref")
        self._mk("beta/dist/app.css", "z" * 2048)
        self._mk("loose/out/thing", "not a repo — no .git, must be ignored")

    def _mk(self, rel, text):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def run_prune(self, *args):
        env = {**os.environ, "ROOST_PROJECTS_DIR": self.dir, "HOME": self.dir}
        return subprocess.run([sys.executable, SCRIPT, *args], env=env,
                              capture_output=True, text=True)

    def exists(self, rel):
        return os.path.exists(os.path.join(self.dir, rel))

    def test_dryrun_lists_but_deletes_nothing(self):
        r = self.run_prune()
        self.assertIn("alpha/out", r.stdout)
        self.assertIn("beta/dist", r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertTrue(self.exists("alpha/out/bundle.js"))  # untouched
        self.assertTrue(self.exists("beta/dist/app.css"))

    def test_non_repo_dir_ignored(self):
        r = self.run_prune()
        self.assertNotIn("loose/out", r.stdout)

    def test_node_modules_only_with_deep(self):
        self.assertNotIn("node_modules", self.run_prune().stdout)
        self.assertIn("node_modules", self.run_prune("--deep").stdout)

    def test_apply_removes_artifacts_but_not_source(self):
        self.run_prune("--yes")
        self.assertFalse(self.exists("alpha/out"))     # artifact gone
        self.assertFalse(self.exists("beta/dist"))
        self.assertTrue(self.exists("alpha/src/main.js"))   # source safe
        self.assertTrue(self.exists("alpha/.git/HEAD"))     # git safe
        self.assertTrue(self.exists("alpha/node_modules")) # not deep -> safe

    def test_project_filter_scopes_deletion(self):
        self.run_prune("--yes", "alpha")
        self.assertFalse(self.exists("alpha/out"))
        self.assertTrue(self.exists("beta/dist"))  # beta untouched

    def test_exit_zero(self):
        self.assertEqual(self.run_prune().returncode, 0)


if __name__ == "__main__":
    unittest.main()
