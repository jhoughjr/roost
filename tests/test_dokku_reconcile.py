#!/usr/bin/env python3
"""Tests for bin/dokku-reconcile.sh's reading — the half that says what answers.

Drives the real script. Stub `docker`, `ssh`, `uptime` and `hostname` stand in
for the box, a stub `curl` answers the vhost probes and passes everything else
to the real curl, and a local HTTP server stands in for pulse: it serves the
declaration at /api/declared and records what is POSTed to /api/answers.

What is under test is the containers dokku does not own. They are named by
hatchery's declaration rather than by this script, so the list has three ways
to arrive — from pulse, from the cache when pulse is silent, and not at all —
and a container has three answers: running, stopped and absent. A container
that is declared and absent is the case this exists for: nothing on the box
mentions it, so a container-only inventory reports a healthy estate.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "dokku-reconcile.sh")

# The declaration as hatchery publishes it: one dokku stack and one host stack,
# and a second box whose containers are not this box's to answer for.
DECLARED = {
    "at": 1788835759931,
    "manifests": ["/infra-state/estate/hatchery.json"],
    "stacks": [
        {
            "name": "estate", "backend": "dokku", "environment": "prod",
            "host": "dokku@192.168.0.103", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "status", "kind": "status", "image": "dokku/status:latest",
                 "domains": ["status.opi"], "findings": []},
            ],
        },
        {
            "name": "box", "backend": "host", "environment": "prod",
            "host": "jimmy@192.168.0.103", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "lan-dns", "kind": "container", "image": "4km3/dnsmasq:latest",
                 "domains": [], "restart": "unless-stopped", "findings": [], "databases": []},
                {"name": "rookery-pg", "kind": "container", "image": "postgres:17-alpine",
                 "domains": [], "restart": "unless-stopped", "findings": [],
                 "databases": [
                    {"name": "rookery", "owner": "app_role"},
                    {"name": "missing_db", "owner": "app_role"},
                 ]},
                {"name": "mwserver-temporal", "kind": "container", "image": "temporalio/temporal:1.8.1",
                 "domains": [], "restart": "unless-stopped", "findings": []},
                {"name": "dokku-reconcile", "kind": "job", "image": "", "domains": [],
                 "schedule": "*:0/10", "keepAlive": False, "platform": "linux", "findings": []},
                {"name": "roost-node-report", "kind": "job", "image": "", "domains": [],
                 "schedule": "every 30s", "keepAlive": False, "platform": "linux", "findings": []},
                {"name": "phoenix-runner-watchdog", "kind": "job", "image": "", "domains": [],
                 "schedule": "every 900s", "keepAlive": False, "platform": "linux", "findings": []},
                {"name": "roost-status", "kind": "job", "image": "", "domains": [],
                 "schedule": "every 900s", "keepAlive": False, "platform": "darwin", "findings": []},
            ],
        },
        {
            "name": "pi", "backend": "host", "environment": "dev",
            "host": "jimmy@192.168.0.117", "manifest": "/infra-state/estate/hatchery.json",
            "services": [
                {"name": "elsewhere", "kind": "container", "image": "x:1", "domains": [],
                 "restart": "always", "findings": []},
            ],
        },
    ],
}

# `docker ps -a` as the box answers it: one dokku container, one container
# running, one stopped, and no row at all for mwserver-temporal.
PS_ALL = """status.web.1 running
lan-dns running
rookery-pg exited
buildx_buildkit_mwserver-builder0 running
"""


class Pulse(BaseHTTPRequestHandler):
    posts = None
    declared = None
    declared_status = 200

    def do_GET(self):
        if self.path != "/api/declared" or Pulse.declared_status != 200:
            self.send_response(Pulse.declared_status if self.path == "/api/declared" else 404)
            self.end_headers()
            return
        body = json.dumps(Pulse.declared).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        Pulse.posts.append((self.path, json.loads(body)))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def write_stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class DokkuReconcileTest(unittest.TestCase):
    def setUp(self):
        Pulse.posts = []
        Pulse.declared = DECLARED
        Pulse.declared_status = 200
        self.server = HTTPServer(("127.0.0.1", 0), Pulse)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.pulse = f"http://127.0.0.1:{self.server.server_port}"

        self.tmp = tempfile.mkdtemp(prefix="roost-reconcile-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, ".roost_node_key"), "w") as fh:
            fh.write("nodekey\n")

        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub)
        self.write_stubs()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    # ── harness ──────────────────────────────────────────────────────────

    def write_stubs(self, ps_all=PS_ALL):
        write_stub(self.stub, "docker", f"""
case "$*" in
  "ps -a --format {{{{.Names}}}} {{{{.State}}}}") printf '%b' {json.dumps(ps_all)} ;;
  "ps -a --format {{{{.Names}}}}") printf '%b' {json.dumps(ps_all)} | awk '{{print $1}}' ;;
  "ps --format {{{{.Names}}}}") printf '%b' {json.dumps(ps_all)} | awk '$2 == "running" {{print $1}}' ;;
  "exec rookery-pg pg_isready -U postgres") exit 0 ;;
  "exec rookery-pg psql -U postgres -Atc select datname from pg_database") printf 'postgres\\ntemplate0\\ntemplate1\\nrookery\\n' ;;
  run*) printf 'status status.opi\\n' ;;
  *) exit 1 ;;
esac
""")
        # dokku answers as it does on the box: one app, deployed and running.
        write_stub(self.stub, "ssh", """
for arg in "$@"; do
  case "$arg" in
    apps:list) printf '=====> My Apps\\nstatus\\n'; exit 0 ;;
    proxy:build-config) printf 'Reloading nginx\\n'; exit 0 ;;
  esac
done
exit 0
""")
        # The vhost probes are answered here; everything else is the real curl.
        # Only the probes are matched: a POST to pulse also carries -H, and short
        # circuiting on that alone reported a run that never posted anything.
        real_curl = shutil.which("curl")
        write_stub(self.stub, "curl", f"""
for arg in "$@"; do
  case "$arg" in
    --resolve|"Host: "*) printf '200'; exit 0 ;;
  esac
done
exec {real_curl} "$@"
""")
        # systemd, as the opi answers about the three jobs the declaration names for this box.
        # One ran and succeeded, one ran and exited 1, and one has never exited at all.
        # Scheduled jobs are timer units; we query both the timer for the last trigger time and
        # the service for the exit status.
        write_stub(self.stub, "systemctl", """
case "$*" in
  *dokku-reconcile.timer*)
    printf 'LastTriggerUSec=Wed 2026-09-09 07:20:33 CDT\\n' ;;
  *dokku-reconcile.service*)
    printf 'ExecMainStatus=0\\nExecMainExitTimestamp=Wed 2026-09-09 07:20:33 CDT\\n' ;;
  *roost-node-report.timer*)
    printf 'LastTriggerUSec=Wed 2026-09-09 07:21:03 CDT\\n' ;;
  *roost-node-report.service*)
    printf 'ExecMainStatus=56\\nExecMainExitTimestamp=Wed 2026-09-09 07:21:03 CDT\\n' ;;
  *phoenix-runner-watchdog.timer*)
    printf 'LastTriggerUSec=\\n' ;;
  *phoenix-runner-watchdog.service*)
    printf 'ExecMainStatus=\\nExecMainExitTimestamp=\\n' ;;
  *) printf 'ExecMainStatus=\\nExecMainExitTimestamp=\\n' ;;
esac
""")
        write_stub(self.stub, "uptime", "printf '2026-09-08 09:00:00\\n'\n")
        write_stub(self.stub, "hostname", "printf '192.168.0.103 10.0.0.1\\n'\n")

    def run_script(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "PATH": self.stub + os.pathsep + env["PATH"],
            "ROOST_PULSE_URL": self.pulse,
            "DOKKU_TARGET": "dokku@localhost",
        })
        return subprocess.run(
            ["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=120)

    def reading(self):
        answers = [body for path, body in Pulse.posts if path == "/api/answers"]
        self.assertEqual(len(answers), 1, f"expected one reading, got {Pulse.posts}")
        return answers[0]

    def rows(self):
        return {app["name"]: app for app in self.reading()["apps"]}

    # ── quarantine of poisoned apps ──────────────────────────────────────

    def test_rebuild_with_poisoned_app_disables_and_retries(self):
        # A rebuild whose output names a poisoned app leads to proxy:disable and a second rebuild.
        def ssh_with_poison(stub):
            write_stub(stub, "ssh", """
for arg in "$@"; do
  case "$arg" in
    apps:list) printf '=====> My Apps\\nstatus\\nbuggy\\n'; exit 0 ;;
    "proxy:disable buggy") printf 'Disabled\\n'; exit 0 ;;
    proxy:build-config)
      if [ "$1" = "buggy" ]; then
        printf 'Reloading nginx\\n'
      else
        printf 'host not found in upstream "invalid:5000" in /home/dokku/buggy/nginx.conf:80\\n'
      fi
      exit 0
      ;;
  esac
done
exit 0
""")

        self.write_stubs()
        ssh_with_poison(self.stub)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The script should report that the app was quarantined.
        self.assertIn("proxy: quarantined buggy", result.stdout)

    def test_quarantined_app_with_running_container_is_enabled(self):
        # A quarantined app whose container is running leads to proxy:enable and per-app rebuild.
        def ssh_with_enable(stub):
            write_stub(stub, "ssh", """
for arg in "$@"; do
  case "$arg" in
    apps:list) printf '=====> My Apps\\nstatus\\nrecovered\\n'; exit 0 ;;
    "proxy:enable recovered") printf 'Enabled\\n'; exit 0 ;;
    "proxy:build-config recovered") printf 'Reloading nginx\\n'; exit 0 ;;
    proxy:build-config) printf 'Reloading nginx\\n'; exit 0 ;;
  esac
done
exit 0
""")

        def docker_with_recovered(stub):
            write_stub(stub, "docker", f"""
case "$*" in
  "ps --format {{{{.Names}}}}") printf 'status.web.1\\nrecovered.web.1\\n' ;;
  "ps -a --format {{{{.Names}}}}") printf 'status.web.1 running\\nrecovered.web.1 running\\n' ;;
  "ps -a --format {{{{.Names}}}} {{{{.State}}}}") printf 'status.web.1 running\\nrecovered.web.1 running\\n' ;;
  run*) printf 'status status.opi\\n' ;;
  *) exit 1 ;;
esac
""")

        self.write_stubs()
        ssh_with_enable(self.stub)
        docker_with_recovered(self.stub)

        # Set up state file with quarantined app.
        state_dir = os.path.join(self.home, ".local", "state")
        os.makedirs(state_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "dokku-reconcile.last")
        with open(state_file, "w") as fh:
            fh.write("started=0 imageless= unserved= quarantined=recovered")

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The script should report that the app is back.
        self.assertIn("proxy: recovered is back, its vhost restored", result.stdout)

    # ── the containers dokku does not own ────────────────────────────────

    def test_declared_containers_answer_beside_dokku_apps(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()

        # The dokku app is reported exactly as before.
        self.assertEqual(rows["status"]["names"], ["status.opi"])
        self.assertTrue(rows["status"]["served"])
        self.assertNotIn("state", rows["status"])

        # A running container answers, and is reached through itself.
        self.assertEqual(rows["lan-dns"]["state"], "running")
        self.assertTrue(rows["lan-dns"]["running"])
        self.assertTrue(rows["lan-dns"]["served"])
        self.assertTrue(rows["lan-dns"]["image"])
        self.assertEqual(rows["lan-dns"]["names"], [])

        # A container that exists and stopped ran and gave up.
        self.assertEqual(rows["rookery-pg"]["state"], "exited")
        self.assertFalse(rows["rookery-pg"]["running"])
        self.assertFalse(rows["rookery-pg"]["served"])
        self.assertTrue(rows["rookery-pg"]["image"])

        # A declared container with no row at all was never made.
        self.assertEqual(rows["mwserver-temporal"]["state"], "absent")
        self.assertFalse(rows["mwserver-temporal"]["running"])
        self.assertFalse(rows["mwserver-temporal"]["image"])

        # Another box's containers are not this box's to answer for.
        self.assertNotIn("elsewhere", rows)
        # A dokku stack's services are already reported through dokku's own half.
        self.assertEqual(len([a for a in self.reading()["apps"] if a["name"] == "status"]), 1)

    def test_the_summary_names_what_is_not_running(self):
        result = self.run_script()
        self.assertIn("declared containers: 1 running", result.stdout)
        self.assertIn("rookery-pg(exited)", result.stdout)
        self.assertIn("mwserver-temporal(absent)", result.stdout)

    def test_a_declared_container_is_never_started(self):
        result = self.run_script()
        # ps:start is dokku's verb, and a container dokku did not make has no dokku name.
        self.assertNotIn("rookery-pg: was down, started", result.stdout)
        self.assertNotIn("lan-dns", result.stdout.split("declared containers")[0])

    # ── where the list comes from ────────────────────────────────────────

    def test_a_silent_pulse_falls_back_to_the_previous_run(self):
        self.run_script()
        cache = os.path.join(self.home, ".roost-reconcile-declared.json")
        self.assertTrue(os.path.exists(cache))

        Pulse.declared_status = 503
        Pulse.posts = []
        result = self.run_script()
        self.assertIn("the previous run's list is used", result.stdout)
        # The reading is still posted, because the POST route is a different one.
        self.assertEqual(self.rows()["lan-dns"]["state"], "running")

    def test_no_list_at_all_reports_dokku_alone_and_says_so(self):
        Pulse.declared_status = 503
        result = self.run_script()
        self.assertIn("only dokku's containers are reported", result.stdout)
        self.assertNotIn("lan-dns", self.rows())
        self.assertIn("status", self.rows())

    def test_a_cache_that_is_not_json_is_no_worse_than_no_cache(self):
        cache = os.path.join(self.home, ".roost-reconcile-declared.json")
        with open(cache, "w") as fh:
            fh.write("<html>a proxy answered instead</html>")
        Pulse.declared_status = 503

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("lan-dns", self.rows())
        self.assertIn("status", self.rows())

    # ── databases in declared containers ─────────────────────────────────

    def test_declared_database_present(self):
        # Set up stubs with rookery-pg running so we can query its databases.
        def docker_with_running_pg(stub):
            write_stub(stub, "docker", f"""
case "$*" in
  "ps -a --format {{{{.Names}}}} {{{{.State}}}}") printf 'status.web.1 running\\nlan-dns running\\nrookery-pg running\\nbuildx_buildkit_mwserver-builder0 running\\n' ;;
  "ps -a --format {{{{.Names}}}}") printf 'status.web.1\\nlan-dns\\nrookery-pg\\nbuildx_buildkit_mwserver-builder0\\n' ;;
  "ps --format {{{{.Names}}}}") printf 'status.web.1\\nlan-dns\\nrookery-pg\\nbuildx_buildkit_mwserver-builder0\\n' ;;
  "exec rookery-pg pg_isready -U postgres") exit 0 ;;
  "exec rookery-pg psql -U postgres -Atc select datname from pg_database") printf 'postgres\\ntemplate0\\ntemplate1\\nrookery\\n' ;;
  run*) printf 'status status.opi\\n' ;;
  *) exit 1 ;;
esac
""")

        self.write_stubs()
        docker_with_running_pg(self.stub)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()

        # A database that exists in the cluster is present.
        self.assertIn("rookery-pg/rookery", rows)
        self.assertEqual(rows["rookery-pg/rookery"]["kind"], "database")
        self.assertEqual(rows["rookery-pg/rookery"]["state"], "present")
        self.assertTrue(rows["rookery-pg/rookery"]["running"])
        self.assertTrue(rows["rookery-pg/rookery"]["served"])

    def test_declared_database_missing(self):
        # Set up stubs with rookery-pg running but missing_db not in the database list.
        def docker_with_running_pg_no_missing(stub):
            write_stub(stub, "docker", f"""
case "$*" in
  "ps -a --format {{{{.Names}}}} {{{{.State}}}}") printf 'status.web.1 running\\nlan-dns running\\nrookery-pg running\\nbuildx_buildkit_mwserver-builder0 running\\n' ;;
  "ps -a --format {{{{.Names}}}}") printf 'status.web.1\\nlan-dns\\nrookery-pg\\nbuildx_buildkit_mwserver-builder0\\n' ;;
  "ps --format {{{{.Names}}}}") printf 'status.web.1\\nlan-dns\\nrookery-pg\\nbuildx_buildkit_mwserver-builder0\\n' ;;
  "exec rookery-pg pg_isready -U postgres") exit 0 ;;
  "exec rookery-pg psql -U postgres -Atc select datname from pg_database") printf 'postgres\\ntemplate0\\ntemplate1\\nrookery\\n' ;;
  run*) printf 'status status.opi\\n' ;;
  *) exit 1 ;;
esac
""")

        self.write_stubs()
        docker_with_running_pg_no_missing(self.stub)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()

        # A database that is declared but not in the cluster is missing.
        self.assertIn("rookery-pg/missing_db", rows)
        self.assertEqual(rows["rookery-pg/missing_db"]["kind"], "database")
        self.assertEqual(rows["rookery-pg/missing_db"]["state"], "missing")
        self.assertFalse(rows["rookery-pg/missing_db"]["running"])
        self.assertFalse(rows["rookery-pg/missing_db"]["served"])

    def test_declared_database_unreachable_when_container_stopped(self):
        # Default stubs have rookery-pg stopped, so databases are unreachable.
        self.write_stubs()

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()

        # The rookery-pg container is stopped, so its databases are unreachable.
        self.assertIn("rookery-pg/rookery", rows)
        self.assertIn("rookery-pg/missing_db", rows)
        self.assertEqual(rows["rookery-pg/rookery"]["state"], "unreachable")
        self.assertEqual(rows["rookery-pg/missing_db"]["state"], "unreachable")

    # ── the declared jobs ────────────────────────────────────────────────

    def test_a_job_that_ran_and_succeeded_answers_ok(self):
        self.run_script()
        row = self.rows()["dokku-reconcile"]
        self.assertEqual(row["kind"], "job")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["exit"], 0)
        self.assertEqual(row["at"], "Wed 2026-09-09 07:20:33 CDT")

    def test_a_job_that_exited_non_zero_answers_failed_with_the_code(self):
        self.run_script()
        row = self.rows()["roost-node-report"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit"], 56)
        self.assertFalse(row["running"])

    def test_a_job_that_has_never_exited_answers_never_ran(self):
        self.run_script()
        row = self.rows()["phoenix-runner-watchdog"]
        self.assertEqual(row["state"], "never-ran")
        self.assertEqual(row["at"], "")

    def test_a_job_on_a_mac_is_not_this_box_to_answer_for(self):
        # roost-status is declared on the mini. The reconcile runs on the opi and answers for Linux.
        self.run_script()
        self.assertNotIn("roost-status", self.rows())

    def test_a_job_is_not_reported_as_a_container_the_box_does_not_hold(self):
        # docker knows nothing about a job, so asking the daemon would report every one as absent.
        self.run_script()
        self.assertNotEqual(self.rows()["dokku-reconcile"].get("state"), "absent")

    def test_the_summary_names_the_jobs_that_are_not_ok(self):
        result = self.run_script()
        self.assertIn("declared jobs: 1 ok", result.stdout)
        self.assertIn("roost-node-report(exit 56)", result.stdout)
        self.assertIn("phoenix-runner-watchdog(never-ran)", result.stdout)

    def test_no_declaration_means_no_job_rows_rather_than_an_error(self):
        Pulse.declared_status = 503
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("dokku-reconcile", self.rows())


if __name__ == "__main__":
    unittest.main()
