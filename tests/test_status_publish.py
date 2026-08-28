#!/usr/bin/env python3
"""Tests for bin/status.sh's two-writer choreography, start to finish: the
site clone's start-of-run pull from origin, the commit -> rebase-on-origin
-> push at the end (the roost#27 race), and the fetch-failure-before-push
guard (the mini's scheduled refresh steamrolling a hand lede, bit twice:
2026-07-26, 2026-07-29).

The collectors and renderer are stubbed out; what's under test is the git
choreography around status.sh's own commit. Each fixture is a site clone
with bare `origin` and `dokku` remotes plus a second writer clone that
pushes concurrently — either before the run starts or mid-run, during
collection — so both a start-of-run pull and a rebase conflict at push time
can be produced on demand.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_SH = os.path.join(ROOT, "bin", "status.sh")
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
        # status.sh sources this sibling for the rc load + defaults.
        shutil.copy(ENV_SH, os.path.join(self.bin, "roost-env.sh"))
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
        """Another machine lands a different board.json on origin/main
        BEFORE our run starts — the start-of-run origin pull should pick
        this up before collecting, with no conflict at push time."""
        w2 = os.path.join(self.tmp, "writer2")
        sh(["git", "clone", "-q", self.origin, w2])
        with open(os.path.join(w2, "fleet", "board.json"), "w") as f:
            f.write("mini\n")
        git(w2, "commit", "-qam", "status: concurrent (mini)")
        git(w2, "push", "-q", "origin", "main")

    def make_fleet_board_land_a_mid_run_write(self):
        """Replace the fleet-board.py stub so that, WHILE our own run is
        collecting (i.e. after our start-of-run origin pull already ran),
        another writer's commit lands on origin/main — a genuine mid-run
        race the start-of-run pull can't see coming, unlike
        concurrent_writer_pushes() which lands before we even start."""
        w2 = os.path.join(self.tmp, "writer2")
        script = (
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)\n"
            "open(sys.argv[1], 'w').write('local\\n')\n"
            f"subprocess.run(['git', 'clone', '-q', {self.origin!r}, {w2!r}], check=True)\n"
            f"open(os.path.join({w2!r}, 'fleet', 'board.json'), 'w').write('mini\\n')\n"
            f"subprocess.run(['git', '-C', {w2!r}, 'commit', '-qam', "
            "'status: concurrent (mini)'], check=True)\n"
            f"subprocess.run(['git', '-C', {w2!r}, 'push', '-q', 'origin', 'main'], check=True)\n"
        )
        self.write_exec("fleet-board.py", script)

    def run_status(self, msg="hello", extra_env=None):
        # ROOST_LOOPBACK stops the Local Network Privacy hop in status.sh.
        # The hop re-execs the script through `ssh localhost`, and the new login shell
        # drops every variable below and then reads the real ~/.roostrc.
        # The run then works on the operator's own status-site instead of this fixture.
        # A host where `ssh localhost` succeeds shows the failure, and a host where it
        # fails does not, so the suite passes on the opi and fails on the mini.
        env = {"HOME": self.home,
               "ROOST_LOOPBACK": "1",
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


class StartOfRunPullTest(StatusPublishFixture):
    """Coverage for the start-of-run site pull (the bug bit the mini's
    scheduled refresh twice: 2026-07-26, 2026-07-29 — it regenerated from a
    local checkout that never pulled the canonical mirror, then force-pushed
    dokku over a hand lede another writer had just deployed)."""

    def test_a_push_that_landed_before_we_started_needs_no_retry(self):
        # Another writer's commit is already on origin by the time we start.
        # The start-of-run origin pull should pick it up while collecting,
        # so there's nothing left to reconcile at push time — no conflict,
        # no retry, single clean pass.
        self.concurrent_writer_pushes()
        r = self.run_status("survived the race")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✓ site: fresh (origin/main)", r.stdout)
        self.assertNotIn("regenerating on top of it", r.stdout)
        self.assertIn("status: survived the race", self.origin_tip())
        self.assertEqual(self.origin_board(), "local")
        # Dokku (the deploy sink) got the same tree.
        self.assertEqual(git(self.dokku, "show", "main:fleet/board.json"),
                         "local")

    def test_origin_unreachable_at_start_falls_back_to_dokku_not_fatal(self):
        # A WAN blip at the top of the run shouldn't stall the whole
        # pipeline — dokku (LAN) is an acceptable fallback source there.
        # Origin comes back (as it typically would within a run) before the
        # final pre-push fetch, so the publish itself still succeeds.
        git(self.site, "remote", "set-url", "origin", "/no/such/path.git")
        self.write_exec(
            "sync-renderer.sh",
            "#!/usr/bin/env bash\n"
            f"git -C {self.site!r} remote set-url origin {self.origin!r}\n"
            "exit 0\n",
            base=os.path.join(self.sgen, "bin"),
        )
        r = self.run_status("origin down at start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("fresh via dokku/main instead", r.stdout)


class RebaseConflictTest(StatusPublishFixture):
    """A genuine mid-run race: another writer's commit lands on origin
    AFTER our start-of-run pull already ran (during collection), so it's
    invisible until the final pre-push fetch — the retry path this covers."""

    def test_conflict_retries_and_the_message_still_lands(self):
        # roost#27: this exact scenario used to print "✓ deployed" while the
        # status commit was silently reset away.
        self.make_fleet_board_land_a_mid_run_write()
        r = self.run_status("survived the race")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("regenerating on top of it", r.stdout)
        self.assertIn("status: survived the race", self.origin_tip())
        self.assertEqual(self.origin_board(), "local")
        # Dokku (the deploy sink) got the same tree.
        self.assertEqual(git(self.dokku, "show", "main:fleet/board.json"),
                         "local")

    def test_second_conflict_fails_loudly_not_silently(self):
        self.make_fleet_board_land_a_mid_run_write()
        r = self.run_status("doomed", extra_env={"ROOST_STATUS_RETRIED": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("status NOT posted", r.stdout + r.stderr)
        self.assertNotIn("✓ status deployed", r.stdout)
        # The concurrent writer's commit survives untouched; ours was dropped
        # but REPORTED dropped.
        self.assertIn("concurrent (mini)", self.origin_tip())


class MirrorUnreachableAtPublishTest(StatusPublishFixture):
    """The other half of the same bug: even with a fresh start-of-run pull,
    the mirror can go unreachable between then and the final push. That used
    to fall through to `git push --force dokku main` with whatever this
    clone had locally — silently steamrolling any writer whose push we
    never saw. Now it refuses to publish over an unverified base."""

    def test_fetch_failure_before_push_aborts_instead_of_pushing_stale_state(self):
        before = git(self.dokku, "log", "-1", "--format=%s", "main")
        # Origin is reachable at start (so the start-of-run pull succeeds),
        # then goes away before the pre-push fetch. sync-renderer.sh runs
        # after collection, right before the publish step — a convenient
        # late hook to sever origin mid-run.
        self.write_exec(
            "sync-renderer.sh",
            "#!/usr/bin/env bash\n"
            f"git -C {self.site!r} remote set-url origin /no/such/path.git\n"
            "exit 0\n",
            base=os.path.join(self.sgen, "bin"),
        )
        r = self.run_status("unreachable mid-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("status NOT posted", r.stdout + r.stderr)
        self.assertIn("mirror fetch failed", r.stdout + r.stderr)
        self.assertNotIn("✓ status deployed", r.stdout)
        # Dokku (the live deploy target) was never touched.
        self.assertEqual(git(self.dokku, "log", "-1", "--format=%s", "main"),
                          before)


if __name__ == "__main__":
    unittest.main()
