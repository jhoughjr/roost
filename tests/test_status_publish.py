#!/usr/bin/env python3
"""Tests for bin/status.sh's publish step — the two-writer race (roost#27).

The collectors and renderer are stubbed out; what's under test is the git
choreography at the end of status.sh: commit → rebase on origin/main → push.
Each fixture is a site clone with bare `origin` and `dokku` remotes plus a
second writer clone that pushes concurrently, so a rebase conflict on the
generated board.json can be produced on demand.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_SH = os.path.join(ROOT, "bin", "status.sh")

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


class StatusPublishFixture(unittest.TestCase):
    """A tmp world: stub bin/, stub statusgen, site repo + remotes + writer2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-status-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

        # A fixture bin/ holding the REAL status.sh next to stubbed siblings,
        # so $BIN/fleet-board.py and $BIN/roost resolve to the stubs.
        self.bin = os.path.join(self.tmp, "fixture", "bin")
        os.makedirs(self.bin)
        shutil.copy(STATUS_SH, os.path.join(self.bin, "status.sh"))
        self.write_exec("fleet-board.py",
                        "#!/usr/bin/env python3\n"
                        "import os, sys\n"
                        "os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)\n"
                        "open(sys.argv[1], 'w').write('local\\n')\n")
        self.write_exec("roost", "#!/usr/bin/env bash\nexit 0\n")

        # Stub statusgen: collectors/renderer/validator all no-ops.
        self.sgen = os.path.join(self.tmp, "statusgen")
        os.makedirs(os.path.join(self.sgen, "bin", "collect"))
        self.write_exec("collect/history.py", "#!/usr/bin/env python3\n",
                        base=os.path.join(self.sgen, "bin"))
        self.write_exec("sync-renderer.sh", "#!/usr/bin/env bash\nexit 0\n",
                        base=os.path.join(self.sgen, "bin"))
        self.write_exec("validate-board.py", "#!/usr/bin/env python3\n",
                        base=os.path.join(self.sgen, "bin"))

        # Site repo with one board file, mirrored to bare origin + dokku.
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(os.path.join(self.site, "fleet"))
        git_init = sh(["git", "init", "-q", "-b", "main", self.site])
        assert git_init.returncode == 0, git_init.stderr
        self.board = os.path.join(self.site, "fleet", "board.json")
        with open(self.board, "w") as f:
            f.write("base\n")
        git(self.site, "add", "-A")
        git(self.site, "commit", "-q", "-m", "seed")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.dokku = os.path.join(self.tmp, "dokku.git")
        for bare in (self.origin, self.dokku):
            sh(["git", "init", "-q", "--bare", "-b", "main", bare])
        git(self.site, "remote", "add", "origin", self.origin)
        git(self.site, "remote", "add", "dokku", self.dokku)
        git(self.site, "push", "-q", "origin", "main")
        git(self.site, "push", "-q", "dokku", "main")

    def write_exec(self, name, body, base=None):
        path = os.path.join(base or self.bin, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def concurrent_writer_pushes(self):
        """Another machine lands a different board.json on origin/main."""
        w2 = os.path.join(self.tmp, "writer2")
        sh(["git", "clone", "-q", self.origin, w2])
        with open(os.path.join(w2, "fleet", "board.json"), "w") as f:
            f.write("mini\n")
        git(w2, "commit", "-qam", "status: concurrent (mini)")
        git(w2, "push", "-q", "origin", "main")

    def run_status(self, msg="hello", extra_env=None):
        env = {"HOME": self.home,
               "ROOST_STATUS_SITE": self.site,
               "ROOST_STATUSGEN": self.sgen,
               "ROOST_DOCS": os.path.join(self.tmp, "no-docs")}
        if extra_env:
            env.update(extra_env)
        return sh(["bash", os.path.join(self.bin, "status.sh"), msg], env=env)

    def origin_tip(self):
        return git(self.origin, "log", "-1", "--format=%s", "main")

    def origin_board(self):
        return git(self.origin, "show", "main:fleet/board.json")


class CleanPublishTest(StatusPublishFixture):
    def test_no_race_publishes_the_message(self):
        r = self.run_status("all quiet")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status: all quiet", self.origin_tip())
        self.assertEqual(self.origin_board(), "local")


class RebaseConflictTest(StatusPublishFixture):
    def test_conflict_retries_and_the_message_still_lands(self):
        # roost#27: this exact scenario used to print "✓ deployed" while the
        # status commit was silently reset away.
        self.concurrent_writer_pushes()
        r = self.run_status("survived the race")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("regenerating on top of it", r.stdout)
        self.assertIn("status: survived the race", self.origin_tip())
        self.assertEqual(self.origin_board(), "local")
        # Dokku (the deploy sink) got the same tree.
        self.assertEqual(git(self.dokku, "show", "main:fleet/board.json"),
                         "local")

    def test_second_conflict_fails_loudly_not_silently(self):
        self.concurrent_writer_pushes()
        r = self.run_status("doomed", extra_env={"ROOST_STATUS_RETRIED": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("status NOT posted", r.stdout + r.stderr)
        self.assertNotIn("✓ status deployed", r.stdout)
        # The concurrent writer's commit survives untouched; ours was dropped
        # but REPORTED dropped.
        self.assertIn("concurrent (mini)", self.origin_tip())


if __name__ == "__main__":
    unittest.main()
