#!/usr/bin/env python3
"""Tests for bin/ci-live-report.sh — the live-CI poller.

Drives the real script: a stub `gh` on PATH returns canned runs, a throwaway
HOME supplies the rc + key, and a local HTTP server stands in for ci-live and
records what was POSTed. Nothing here talks to GitHub or the network.

The aggregate feed is the part worth pinning. A board's live-console polls one
project, so a two-repo board can only show one repo's runs without it — and the
failure modes are quiet ones: a feed blanked by an outage reads exactly like
"nothing is building".

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "ci-live-report.sh")

PHOENIX = "Austin-MacWorks/Phoenix-Electron"
MWSERVER = "Austin-MacWorks/MWServer"

# One live run each, at different times, plus a completed run that must be
# filtered out — the static collector already reports finished runs.
RUNS = {
    PHOENIX: [
        {"status": "in_progress", "conclusion": None, "headBranch": "dev",
         "event": "push", "createdAt": "2026-07-26T05:00:00Z",
         "url": "https://gh/p/1", "databaseId": 1},
        {"status": "completed", "conclusion": "success", "headBranch": "dev",
         "event": "push", "createdAt": "2026-07-26T04:00:00Z",
         "url": "https://gh/p/0", "databaseId": 0},
    ],
    MWSERVER: [
        {"status": "queued", "conclusion": None, "headBranch": "dev",
         "event": "push", "createdAt": "2026-07-26T05:30:00Z",
         "url": "https://gh/m/9", "databaseId": 9},
    ],
}


class Recorder(BaseHTTPRequestHandler):
    posts = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        Recorder.posts.append(json.loads(body))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class CiLiveReportTest(unittest.TestCase):
    def setUp(self):
        Recorder.posts = []
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

        self.home = tempfile.mkdtemp()
        self.stub = tempfile.mkdtemp()
        with open(os.path.join(self.home, ".roost_ci_key"), "w") as fh:
            fh.write("devkey\n")
        # jq must stay reachable: the script only prepends the brew bins when
        # its tools are missing, and this PATH is deliberately stub-first.
        os.symlink(shutil.which("jq"), os.path.join(self.stub, "jq"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.stub, ignore_errors=True)

    # ── harness ──────────────────────────────────────────────────────────

    def write_gh(self, runs=None, fail=False):
        """A stub `gh` that answers `run list --repo X` from `runs`."""
        payload = json.dumps({k: v for k, v in (runs or {}).items()})
        body = "exit 1" if fail else f"""
repo=""
while [ $# -gt 0 ]; do case "$1" in --repo) repo="$2"; shift 2;; *) shift;; esac; done
python3 -c '
import json, sys
print(json.dumps(json.loads(sys.argv[1]).get(sys.argv[2], [])))
' {json.dumps(payload)} "$repo"
"""
        path = os.path.join(self.stub, "gh")
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body)
        os.chmod(path, 0o755)

    def write_rc(self, repos, aggregate=None):
        lines = [f"ROOST_CI_LIVE_REPOS={repos}",
                 f"ROOST_CI_LIVE_ENDPOINT={self.endpoint}"]
        if aggregate:
            lines.append(f"ROOST_CI_LIVE_AGGREGATE={aggregate}")
        with open(os.path.join(self.home, ".roostrc"), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def run_poller(self):
        env = dict(os.environ, HOME=self.home,
                   PATH=self.stub + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", SCRIPT], env=env, capture_output=True,
                              text=True, timeout=60)

    def posted(self, project):
        return next((p for p in Recorder.posts if p["project"] == project), None)

    # ── per-repo behaviour (unchanged contract) ──────────────────────────

    def test_each_repo_gets_its_own_project_feed(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30")
        self.run_poller()
        self.assertIsNotNone(self.posted("phoenix"))
        self.assertIsNotNone(self.posted("mwserver"))

    def test_finished_runs_are_not_live_runs(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30")
        self.run_poller()
        lines = self.posted("phoenix")["lines"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], "in progress")
        self.assertEqual(lines[0]["tone"], "wip")

    # ── the label field ──────────────────────────────────────────────────

    def test_label_defaults_to_the_repo_name(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30")
        self.run_poller()
        self.assertTrue(
            self.posted("phoenix")["lines"][0]["text"].startswith("Phoenix-Electron ·"))

    # Without this the same repo is called two different things depending on
    # whether its run happens to be finished — "Phoenix" in the static console,
    # "Phoenix-Electron" in the live one directly above it.
    def test_label_can_match_the_static_consoles_name(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30:Phoenix")
        self.run_poller()
        self.assertEqual(self.posted("phoenix")["lines"][0]["text"], "Phoenix · dev")

    def test_interval_is_still_read_when_a_label_follows_it(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:45:Phoenix")
        self.run_poller()
        self.assertEqual(self.posted("phoenix")["intervalMs"], 45000)

    # ── the aggregate feed ───────────────────────────────────────────────

    def test_aggregate_merges_every_repo_newest_first(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30:Phoenix,{MWSERVER}:mwserver:30:MWServer",
                      aggregate="all")
        self.run_poller()
        lines = self.posted("all")["lines"]
        self.assertEqual([l["text"] for l in lines],
                         ["MWServer · dev", "Phoenix · dev"])

    def test_aggregate_keeps_each_runs_own_watch_command(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30",
                      aggregate="all")
        self.run_poller()
        cmds = [l["cmd"] for l in self.posted("all")["lines"]]
        self.assertIn(f"gh run watch 9 -R {MWSERVER}", cmds)
        self.assertIn(f"gh run watch 1 -R {PHOENIX}", cmds)

    def test_aggregate_refreshes_as_often_as_its_fastest_member(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:60,{MWSERVER}:mwserver:20",
                      aggregate="all")
        self.run_poller()
        self.assertEqual(self.posted("all")["intervalMs"], 20000)

    def test_per_repo_feeds_survive_alongside_the_aggregate(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30",
                      aggregate="all")
        self.run_poller()
        self.assertEqual(len(Recorder.posts), 3)

    def test_no_aggregate_configured_means_no_aggregate_post(self):
        self.write_gh(RUNS)
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30")
        self.run_poller()
        self.assertEqual({p["project"] for p in Recorder.posts},
                         {"phoenix", "mwserver"})

    # An outage must not read as "nothing is building": with no repo answering
    # there is nothing to say, so the last good feed stands.
    def test_a_total_gh_failure_posts_nothing_at_all(self):
        self.write_gh(fail=True)
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30",
                      aggregate="all")
        self.run_poller()
        self.assertEqual(Recorder.posts, [])

    # …but a genuinely idle pipeline must clear, or finished runs linger as
    # "running" until something else starts.
    def test_repos_that_answer_with_nothing_live_clear_the_feed(self):
        self.write_gh({PHOENIX: [RUNS[PHOENIX][1]], MWSERVER: []})
        self.write_rc(f"{PHOENIX}:phoenix:30,{MWSERVER}:mwserver:30",
                      aggregate="all")
        self.run_poller()
        self.assertEqual(self.posted("all")["lines"], [])

    def test_one_repo_down_still_reports_the_other(self):
        self.write_gh({PHOENIX: RUNS[PHOENIX]})   # MWServer answers []
        self.write_rc(f"{PHOENIX}:phoenix:30:Phoenix,{MWSERVER}:mwserver:30",
                      aggregate="all")
        self.run_poller()
        self.assertEqual([l["text"] for l in self.posted("all")["lines"]],
                         ["Phoenix · dev"])

    def test_unconfigured_repos_is_a_clean_exit(self):
        self.write_gh(RUNS)
        self.write_rc("")
        r = self.run_poller()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(Recorder.posts, [])


if __name__ == "__main__":
    unittest.main()
