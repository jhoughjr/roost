#!/usr/bin/env python3
"""Tests for the job rows bin/node-report.sh adds on a Mac.

Drives the real script on a Mac. A stub `launchctl` stands in for the supervisor and a stub `curl`
stands in for both pulse routes: it serves the declaration from a file and records the reading that
is POSTed. Every other reading the script takes is the real machine's, read only.

The reconcile answers for the opi's jobs and runs on the opi. Nothing runs on a Mac to answer for a
Mac's jobs, so this report carries them, and that is what is under test: a job that ran and
succeeded, one that exited non-zero, one launchd is not holding, and a job declared for another host.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "node-report.sh")

# The declaration as hatchery publishes it: four jobs on this Mac and one on the opi.
DECLARED = {
    "at": 1788835759931,
    "manifests": ["/infra-state/estate/hatchery.json"],
    "stacks": [
        {
            "name": "laptop-jobs", "backend": "host", "environment": "prod",
            "host": "jimmy@127.0.0.1", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "roost-node-report", "kind": "job", "image": "", "domains": [],
                 "schedule": "every 30s", "keepAlive": False, "platform": "darwin",
                 "log": "/tmp/roost-node-report.log", "findings": []},
                {"name": "roost-ha-scoop", "kind": "job", "image": "", "domains": [],
                 "schedule": "*-*-* *:10:00", "keepAlive": False, "platform": "darwin",
                 "findings": []},
                {"name": "hatchery-serve", "kind": "job", "image": "", "domains": [],
                 "keepAlive": True, "platform": "darwin", "findings": []},
                {"name": "lan-dns", "kind": "container", "image": "4km3/dnsmasq:latest",
                 "domains": [], "restart": "unless-stopped", "findings": []},
            ],
        },
        {
            "name": "opi-jobs", "backend": "host", "environment": "prod",
            "host": "jimmy@192.168.0.103", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "dokku-reconcile", "kind": "job", "image": "", "domains": [],
                 "schedule": "*:0/10", "keepAlive": False, "platform": "linux", "findings": []},
            ],
        },
        {
            "name": "air", "backend": "host", "environment": "prod",
            "host": "jimmy@local", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "battery-alarm", "kind": "job", "image": "", "domains": [],
                 "schedule": "*:*:0,30", "keepAlive": False, "platform": "darwin",
                 "label": "com.jimmy.battery-alarm", "findings": []},
                {"name": "roms-catalog", "kind": "job", "image": "", "domains": [],
                 "schedule": "0 *", "keepAlive": False, "platform": "darwin",
                 "findings": []},
            ],
        },
    ],
}


def write_stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


@unittest.skipUnless(platform.system() == "Darwin", "the job rows are the Mac half of the report")
class NodeReportJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-node-report-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, ".roost_node_key"), "w") as fh:
            fh.write("nodekey\n")

        self.declared_path = os.path.join(self.tmp, "declared.json")
        with open(self.declared_path, "w") as fh:
            json.dump(DECLARED, fh)
        self.posted = os.path.join(self.tmp, "posted.json")

        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub)
        self.write_stubs()

    # ── harness ──────────────────────────────────────────────────────────

    def write_stubs(self):
        # Both pulse routes. A GET of the declaration copies the file, and a POST records the body.
        write_stub(self.stub, "curl", f"""
declared={json.dumps(self.declared_path)}
posted={json.dumps(self.posted)}
out=""
data=""
want_out=0
want_data=0
for arg in "$@"; do
  if [ "$want_out" = 1 ]; then out="$arg"; want_out=0; continue; fi
  if [ "$want_data" = 1 ]; then data="$arg"; want_data=0; continue; fi
  case "$arg" in
    -o) want_out=1 ;;
    -d) want_data=1 ;;
  esac
done
if [ -n "$out" ]; then cp "$declared" "$out"; exit 0; fi
if [ -n "$data" ]; then printf '%s' "$data" > "$posted"; exit 0; fi
exit 0
""")
        # launchd, as this Mac answers about the three jobs the declaration names for it.
        write_stub(self.stub, "launchctl", """
case "$*" in
  *roost-node-report*)
    printf '\\tstate = not running\\n\\tlast exit code = 0\\n' ;;
  *roost-ha-scoop*)
    printf '\\tstate = not running\\n\\tlast exit code = 1\\n' ;;
  *hatchery-serve*)
    printf '\\tstate = running\\n' ;;
  *) exit 113 ;;
esac
""")

    def run_script(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "PATH": self.stub + os.pathsep + env["PATH"],
            "ROOST_PULSE_URL": "http://127.0.0.1:1",
            "ROOST_NODE_NAME": "laptop",
        })
        return subprocess.run(
            ["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=180)

    def reading(self):
        with open(self.posted) as fh:
            return json.load(fh)

    def jobs(self):
        return {row["name"]: row for row in self.reading().get("jobs", [])}

    # ── the rows ─────────────────────────────────────────────────────────

    def test_a_job_that_last_exited_zero_answers_ok(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = self.jobs()["roost-node-report"]
        self.assertEqual(row["kind"], "job")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["exit"], 0)

    def test_a_job_that_last_exited_non_zero_answers_failed_with_the_code(self):
        self.run_script()
        row = self.jobs()["roost-ha-scoop"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit"], 1)

    def test_a_kept_alive_job_that_is_running_answers_running(self):
        self.run_script()
        row = self.jobs()["hatchery-serve"]
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["exit"])
        self.assertTrue(row["running"])

    def test_a_kept_alive_job_that_crashed_with_exit_code_answers_failed(self):
        # Override the launchctl stub to show hatchery-serve as stopped with a non-zero exit code.
        write_stub(self.stub, "launchctl", """
case "$*" in
  *roost-node-report*)
    printf '\\tstate = not running\\n\\tlast exit code = 0\\n' ;;
  *roost-ha-scoop*)
    printf '\\tstate = not running\\n\\tlast exit code = 1\\n' ;;
  *hatchery-serve*)
    printf '\\tstate = not running\\n\\tlast exit code = 1\\n' ;;
  *) exit 113 ;;
esac
""")
        self.run_script()
        row = self.jobs()["hatchery-serve"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit"], 1)

    def test_launchd_keeps_no_time_of_the_last_run_and_the_row_says_so(self):
        self.run_script()
        self.assertEqual(self.jobs()["roost-node-report"]["at"], "")

    def test_a_job_declared_for_another_host_is_not_this_Mac_to_answer_for(self):
        self.run_script()
        self.assertNotIn("dokku-reconcile", self.jobs())

    def test_a_container_is_not_a_job(self):
        self.run_script()
        self.assertNotIn("lan-dns", self.jobs())

    def test_a_declaration_that_never_arrives_costs_the_report_nothing(self):
        write_stub(self.stub, "curl", f"""
posted={json.dumps(self.posted)}
data=""
want_data=0
for arg in "$@"; do
  if [ "$want_data" = 1 ]; then data="$arg"; want_data=0; continue; fi
  case "$arg" in -d) want_data=1 ;; esac
done
if [ -n "$data" ]; then printf '%s' "$data" > "$posted"; exit 0; fi
exit 22
""")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("jobs", self.reading())
        self.assertIn("name", self.reading())

    def test_roost_stack_matches_jobs_by_stack_name(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "PATH": self.stub + os.pathsep + env["PATH"],
            "ROOST_PULSE_URL": "http://127.0.0.1:1",
            "ROOST_NODE_NAME": "laptop",
            "ROOST_STACK": "air",
        })
        result = subprocess.run(
            ["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        jobs = {row["name"]: row for row in self.reading().get("jobs", [])}
        self.assertIn("battery-alarm", jobs)
        self.assertIn("roms-catalog", jobs)

    def test_declared_label_overrides_built_label(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "PATH": self.stub + os.pathsep + env["PATH"],
            "ROOST_PULSE_URL": "http://127.0.0.1:1",
            "ROOST_NODE_NAME": "laptop",
            "ROOST_STACK": "air",
        })
        # Stub launchctl to answer for the air jobs (both scheduled).
        write_stub(self.stub, "launchctl", """
case "$*" in
  *battery-alarm*)
    printf '\\tstate = not running\\n\\tlast exit code = 0\\n' ;;
  *roms-catalog*)
    printf '\\tstate = not running\\n\\tlast exit code = 0\\n' ;;
  *) exit 113 ;;
esac
""")
        result = subprocess.run(
            ["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        jobs = {row["name"]: row for row in self.reading().get("jobs", [])}
        # The battery-alarm job has a declared label, so it should be matched with that.
        self.assertEqual(jobs["battery-alarm"]["state"], "ok")
        # The roms-catalog job has no declared label, so it should use the built one.
        self.assertEqual(jobs["roms-catalog"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
