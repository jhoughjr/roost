#!/usr/bin/env python3
"""Tests for bin/rollout.sh — the local pull leg and the failure accounting.

The ssh leg is exercised only through an unreachable target (BatchMode +
a .invalid hostname fails fast); real remote pulls run the same PULL script
the local leg runs, which is what these tests cover.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLLOUT_SH = os.path.join(ROOT, "bin", "rollout.sh")
ENV_SH = os.path.join(ROOT, "bin", "roost-env.sh")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test",
}


def sh(args, cwd=None, env=None):
    e = dict(os.environ, **GIT_ENV)
    if env:
        e.update(env)
    return subprocess.run(args, cwd=cwd, env=e, capture_output=True, text=True)


def git(repo, *args):
    r = sh(["git", "-C", repo, *args])
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


class RolloutFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-rollout-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

        # rollout.sh copied into a bin/ whose parent is NOT a git repo, so the
        # "local roost clone" leg reports "no clone here" instead of touching
        # this working copy.
        fixture_bin = os.path.join(self.tmp, "fixture", "bin")
        os.makedirs(fixture_bin)
        self.rollout = os.path.join(fixture_bin, "rollout.sh")
        shutil.copy(ROLLOUT_SH, self.rollout)
        # rollout.sh sources this sibling for the rc load + defaults.
        shutil.copy(ENV_SH, os.path.join(fixture_bin, "roost-env.sh"))

        # A "statusgen clone" one commit behind its origin: upstream work1
        # pushes c1 and c2; the machine's clone (work2) is cloned at c1.
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.origin])
        work1 = os.path.join(self.tmp, "work1")
        sh(["git", "clone", "-q", self.origin, work1])
        with open(os.path.join(work1, "f"), "w") as f:
            f.write("one\n")
        git(work1, "add", "-A")
        git(work1, "commit", "-qm", "c1")
        git(work1, "push", "-q", "origin", "main")
        self.sgen = os.path.join(self.tmp, "sgen-clone")
        sh(["git", "clone", "-q", self.origin, self.sgen])
        with open(os.path.join(work1, "f"), "w") as f:
            f.write("two\n")
        git(work1, "commit", "-qam", "c2")
        git(work1, "push", "-q", "origin", "main")
        self.work1 = work1

    def run_rollout(self, *args, writers="", extra_env=None):
        env = {"HOME": self.home,
               "ROOST_STATUSGEN": self.sgen,
               "ROOST_WRITERS": writers}
        if extra_env:
            env.update(extra_env)
        return sh(["bash", self.rollout, *args], env=env)

    def test_behind_clone_fast_forwards(self):
        r = self.run_rollout()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("→", r.stdout)              # sgen moved c1 → c2
        self.assertIn("no clone here", r.stdout)  # fixture roost isn't a repo
        self.assertIn("rollout complete", r.stdout)
        self.assertEqual(git(self.sgen, "log", "-1", "--format=%s"), "c2")

    def test_diverged_clone_is_left_alone_and_reported(self):
        with open(os.path.join(self.sgen, "f"), "w") as f:
            f.write("mine\n")
        git(self.sgen, "commit", "-qam", "local divergence")
        r = self.run_rollout()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pull failed (dirty or diverged)", r.stdout)
        self.assertIn("rollout incomplete", r.stdout + r.stderr)
        # The divergent commit is untouched.
        self.assertEqual(git(self.sgen, "log", "-1", "--format=%s"),
                         "local divergence")

    def test_unreachable_writer_fails_the_run(self):
        r = self.run_rollout(writers="nobody@rollout-test.invalid")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unreachable", r.stdout)

    def test_unknown_flag_is_an_error(self):
        r = self.run_rollout("--frobnicate")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
