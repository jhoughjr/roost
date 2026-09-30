#!/usr/bin/env python3
"""Tests that no roost script puts a secret on curl's command line.

Every account on a host can read the argv of every process. On 2026-09-30 a ps capture on the mini caught the CI key
from ci-live-report.sh about three times a minute, because the script sent it as `-H "x-roost-ci-key: KEY"`.
Each script now sends a secret header through `roost_curl_header` in lib/roost-secret.sh, which writes it to a mode 600 file.

Each test drives the real script against a stub `curl` on PATH. The stub records its argv and the contents of each
`-H @file` it is given, so a test can assert that the key is in the file and in no argument.
All keys here are made up.

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
BIN = os.path.join(ROOT, "bin")
LIB = os.path.join(ROOT, "lib")

KEY = "made-up-key-4f2a9c7e"

# The stub curl. It writes one argument per line to argv.log and the contents of each `-H @file` to headers.log.
# It answers with $CURL_BODY, and with the status line when a -w format asks for one.
CURL_STUB = r"""
log="${CURL_LOG_DIR:?}"
prev=""
out=""
fmt=""
for arg in "$@"; do
  printf '%s\n' "$arg" >> "$log/argv.log"
  if [ "$prev" = "-H" ]; then
    case "$arg" in @*) cat "${arg#@}" >> "$log/headers.log" ;; esac
  fi
  [ "$prev" = "-o" ] && out="$arg"
  [ "$prev" = "-w" ] && fmt="$arg"
  prev="$arg"
done
if [ -n "$out" ] && [ "$out" != "/dev/null" ]; then printf '%s' "${CURL_BODY:-{\}}" > "$out"; exit 0; fi
case "$fmt" in
  *$'\n'*"%{http_code}"*) printf '%s\n200' "${CURL_BODY:-{\}}" ;;
  *"%{http_code}"*) [ "$out" = "/dev/null" ] || printf '%s' "${CURL_BODY:-}"; printf '200' ;;
  *) printf '%s' "${CURL_BODY:-{\}}" ;;
esac
exit 0
"""


def write_stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class HeaderFileCase(unittest.TestCase):
    """A throwaway HOME, a stub bin directory first on PATH, and the logs the stub curl writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-header-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "stub")
        self.logs = os.path.join(self.tmp, "logs")
        self.scratch = os.path.join(self.tmp, "scratch")
        for d in (self.home, self.stub, self.logs, self.scratch):
            os.makedirs(d)
        write_stub(self.stub, "curl", CURL_STUB)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ROOST_") and k not in ("WATCHDOG_TOKEN", "GITHUB_TOKEN")}
        env.update({
            "HOME": self.home,
            "PATH": self.stub + os.pathsep + os.environ["PATH"],
            "TMPDIR": self.scratch,
            "CURL_LOG_DIR": self.logs,
        })
        env.update(extra)
        return env

    def write_home(self, name, text):
        with open(os.path.join(self.home, name), "w") as fh:
            fh.write(text)

    def argv(self):
        path = os.path.join(self.logs, "argv.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def headers(self):
        path = os.path.join(self.logs, "headers.log")
        if not os.path.exists(path):
            return ""
        with open(path) as fh:
            return fh.read()

    def assert_key_only_in_header_file(self, header_line, result):
        detail = result.stdout + result.stderr
        self.assertTrue(self.argv(), "curl never ran: " + detail)
        for arg in self.argv():
            self.assertNotIn(KEY, arg)
        self.assertIn(header_line, self.headers().splitlines())
        # The helper removes its file once curl returns.
        self.assertEqual([n for n in os.listdir(self.scratch) if n.startswith("roost-hdr")], [])


class RoostCurlHeaderTest(HeaderFileCase):
    def call(self, *args):
        script = f'. {json.dumps(os.path.join(LIB, "roost-secret.sh"))}; roost_curl_header "$@"'
        return subprocess.run(["bash", "-c", script, "roost-test", *args], env=self.env(), capture_output=True, text=True, timeout=30)

    def test_the_value_reaches_the_file_and_no_argument(self):
        result = self.call("x-roost-ci-key", KEY, "-s", "https://example.invalid/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_key_only_in_header_file(f"x-roost-ci-key: {KEY}", result)

    def test_curl_exit_status_passes_through(self):
        write_stub(self.stub, "curl", "exit 22\n")
        result = self.call("x-roost-node-key", KEY, "-sf", "https://example.invalid/")
        self.assertEqual(result.returncode, 22)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_the_header_file_is_private_while_curl_runs(self):
        write_stub(self.stub, "curl", f"""
for arg in "$@"; do case "$arg" in @*) stat -f %Lp "${{arg#@}}" 2>/dev/null || stat -c %a "${{arg#@}}" ;; esac; done
""")
        result = self.call("Authorization", f"Bearer {KEY}", "https://example.invalid/")
        self.assertEqual(result.stdout.strip(), "600")


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class CiLiveReportTest(HeaderFileCase):
    def test_the_ci_key_travels_in_a_header_file(self):
        self.write_home(".roost_ci_key", KEY + "\n")
        self.write_home(".roostrc", "ROOST_CI_LIVE_REPOS=acme/widget:widget:30\nROOST_CI_LIVE_ENDPOINT=http://127.0.0.1:1\n")
        os.symlink(shutil.which("jq"), os.path.join(self.stub, "jq"))
        run = {"status": "in_progress", "conclusion": None, "headBranch": "dev", "event": "push",
               "createdAt": "2026-09-30T05:00:00Z", "url": "https://gh/w/1", "databaseId": 1}
        write_stub(self.stub, "gh", f"printf '%s' {json.dumps(json.dumps([run]))}\n")
        result = subprocess.run(["bash", os.path.join(BIN, "ci-live-report.sh")], env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_key_only_in_header_file(f"x-roost-ci-key: {KEY}", result)


class NodeReportTest(HeaderFileCase):
    def test_the_node_key_travels_in_a_header_file(self):
        self.write_home(".roost_node_key", KEY + "\n")
        env = self.env(ROOST_PULSE_URL="http://127.0.0.1:1", ROOST_NODE_NAME="laptop")
        result = subprocess.run(["bash", os.path.join(BIN, "node-report.sh")], env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_key_only_in_header_file(f"x-roost-node-key: {KEY}", result)
        self.assertIn("http://127.0.0.1:1/api/nodes", self.argv())
        if platform.system() == "Darwin":
            # A Mac also reads its declaration, with the same key.
            self.assertIn("http://127.0.0.1:1/api/declared", self.argv())
            self.assertEqual(self.headers().splitlines().count(f"x-roost-node-key: {KEY}"), 2)


class BackupReportTest(HeaderFileCase):
    def test_the_node_key_travels_in_a_header_file(self):
        self.write_home(".roost_node_key", KEY + "\n")
        # Given a reading, so the test never probes a real backup host
        write_stub(self.stub, "python3", "printf '{\"ok\":true}'\n")
        env = self.env(ROOST_PULSE_URL="http://127.0.0.1:1")
        result = subprocess.run(["bash", os.path.join(BIN, "backup-report.sh")], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_key_only_in_header_file(f"x-roost-node-key: {KEY}", result)


class RunnerWatchdogTest(HeaderFileCase):
    def run_watchdog(self, script):
        env = self.env(
            WATCHDOG_TOKEN=KEY,
            WATCHDOG_ENV_FILE=os.path.join(self.tmp, "no-such.env"),
            WATCHDOG_STATE_DIR=os.path.join(self.tmp, "state"),
            WATCHDOG_RUNNERS="mini",
            CURL_BODY=json.dumps({"runners": [{"name": "mini", "status": "online"}], "workflow_runs": []}),
        )
        return subprocess.run(["bash", script, "--dry-run"], env=env, capture_output=True, text=True, timeout=60)

    def test_the_github_token_travels_in_a_header_file(self):
        result = self.run_watchdog(os.path.join(BIN, "runner-watchdog.sh"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runner mini: online", result.stdout)
        self.assert_key_only_in_header_file(f"Authorization: Bearer {KEY}", result)

    def test_the_installed_copy_finds_the_lib_beside_it(self):
        # Given the layout install-runner-watchdog.sh writes: the script, and lib/ in the same directory
        dest = os.path.join(self.tmp, "opt", "phoenix-watchdog")
        os.makedirs(os.path.join(dest, "lib"))
        shutil.copy(os.path.join(BIN, "runner-watchdog.sh"), dest)
        shutil.copy(os.path.join(LIB, "roost-secret.sh"), os.path.join(dest, "lib"))
        result = self.run_watchdog(os.path.join(dest, "runner-watchdog.sh"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_key_only_in_header_file(f"Authorization: Bearer {KEY}", result)

    def test_the_installer_copies_the_lib(self):
        with open(os.path.join(BIN, "install-runner-watchdog.sh")) as fh:
            self.assertIn('"$DEST/lib/roost-secret.sh"', fh.read())


class RoostDoctorTest(HeaderFileCase):
    def test_the_cloudflare_token_travels_in_a_header_file(self):
        self.write_home(".cf_api_token", KEY + "\n")
        # Given no dokku host and no GitHub, so the doctor reaches only the stub curl
        write_stub(self.stub, "ssh", "exit 0\n")
        write_stub(self.stub, "gh", "exit 1\n")
        env = self.env(CURL_BODY='{"status":"active","name":"example.test"}', ROOST_DOMAIN="example.test")
        result = subprocess.run(["bash", os.path.join(BIN, "roost"), "doctor"], env=env, capture_output=True, text=True, timeout=120)
        self.assertIn("cloudflare token", result.stdout, result.stderr)
        self.assertIn("✓ active", result.stdout)
        self.assert_key_only_in_header_file(f"Authorization: Bearer {KEY}", result)
        self.assertEqual(self.headers().splitlines().count(f"Authorization: Bearer {KEY}"), 2)


if __name__ == "__main__":
    unittest.main()
