#!/usr/bin/env python3
"""Tests for lib/roost-secret.sh and lib/roost_secret.py - the secret reader with vault in front.

Both languages are driven for real: the shell helper is sourced under `set -euo pipefail` with a
stub `curl` on PATH and a throwaway HOME, and the Python module talks to a local HTTP server that
stands in for vault. Nothing here reaches the network or a host.

The three arms matter because the cutover is one host at a time: a host with vault keys must read
vault, a host without them must keep reading its legacy file, and a host with neither must say so
rather than send an empty key. The stub curl records its argv, because a key on a command line is
readable by every account on the box, which is the thing this change exists to end.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "lib", "roost-secret.sh")
sys.path.insert(0, os.path.join(ROOT, "lib"))

APP = "roost-test-node"
APP_KEY = "app-key-2b1f9c"
VAULT_NODE_KEY = "node-key-from-vault"
FILE_NODE_KEY = "node-key-from-file"
FILE_CI_KEY = "ci-key-from-file"


class SecretFixture(unittest.TestCase):
    """A throwaway HOME and TMPDIR, plus a stub curl that answers as vault."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-secret-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.tmpdir = os.path.join(self.tmp, "tmp")
        self.stub = os.path.join(self.tmp, "stub")
        for d in (self.home, self.tmpdir, self.stub):
            os.makedirs(d)
        self.argv_log = os.path.join(self.tmp, "curl-argv")
        self.call_log = os.path.join(self.tmp, "curl-calls")

    # -- fixture writers -------------------------------------------------

    def write_rc(self, **keys):
        with open(os.path.join(self.home, ".roostrc"), "w") as fh:
            for name, value in keys.items():
                fh.write(f"{name}={value}\n")

    def write_legacy(self, name, value):
        path = os.path.join(self.home, {"NODE_KEY": ".roost_node_key",
                                        "CI_KEY": ".roost_ci_key"}[name])
        with open(path, "w") as fh:
            fh.write(value + "\n")
        os.chmod(path, 0o600)

    def write_curl(self, document=None, fail=False):
        """A stub curl that logs its argv, then answers with `document` or fails as curl -sf does."""
        body = json.dumps(document if document is not None else {})
        script = f"""#!/bin/bash
printf '%s\\n' "$*" >> {json.dumps(self.argv_log)}
echo call >> {json.dumps(self.call_log)}
"""
        if fail:
            # 22 is what curl -sf exits on an HTTP error.
            script += "exit 22\n"
        else:
            script += f"printf '%s' {json.dumps(body)}\n"
        path = os.path.join(self.stub, "curl")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)

    # -- readers ---------------------------------------------------------

    def curl_calls(self):
        try:
            with open(self.call_log) as fh:
                return len(fh.read().split())
        except OSError:
            return 0

    def curl_argv(self):
        try:
            with open(self.argv_log) as fh:
                return fh.read()
        except OSError:
            return ""

    def env(self, extra=None):
        e = dict(os.environ, HOME=self.home, TMPDIR=self.tmpdir,
                 PATH=self.stub + os.pathsep + os.environ["PATH"])
        for name in ("ROOST_VAULT_URL", "ROOST_VAULT_APP", "ROOST_VAULT_APP_KEY"):
            e.pop(name, None)
        if extra:
            e.update(extra)
        return e

    def sh(self, script, extra=None):
        """Source the helper under the same options the reporters run with, then run `script`."""
        return subprocess.run(["bash", "-euo", "pipefail", "-c",
                               f'. "{HELPER}"\n{script}\n'],
                              env=self.env(extra), capture_output=True, text=True, timeout=60)

    def cache_file(self, app=APP):
        return os.path.join(self.tmpdir, f"roost-secrets-{os.getuid()}-{app}.json")


class ShellArmsTest(SecretFixture):
    def test_vault_answers_and_the_file_is_not_consulted(self):
        self.write_rc(ROOST_VAULT_URL="https://vault.test",
                      ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY, "CI_KEY": "ci-key-from-vault"})
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)

        r = self.sh("roost_secret NODE_KEY")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, VAULT_NODE_KEY)
        self.assertEqual(self.sh("roost_secret_source NODE_KEY").stdout.strip(), "vault")

    def test_the_value_is_the_whole_of_stdout(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.assertEqual(self.sh("roost_secret NODE_KEY").stdout, VAULT_NODE_KEY)

    def test_no_vault_configured_falls_back_to_the_file(self):
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)

        r = self.sh("roost_secret NODE_KEY")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, FILE_NODE_KEY)
        self.assertEqual(self.sh("roost_secret_source NODE_KEY").stdout.strip(), "file")
        self.assertEqual(self.curl_calls(), 0, "an unconfigured host must not call vault")

    def test_vault_that_does_not_answer_falls_back_to_the_file(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl(fail=True)
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)

        r = self.sh("roost_secret NODE_KEY")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, FILE_NODE_KEY)
        self.assertEqual(self.sh("roost_secret_source NODE_KEY").stdout.strip(), "file")

    def test_a_document_without_the_name_falls_back_to_the_file(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"CI_KEY": "ci-key-from-vault"})
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)

        self.assertEqual(self.sh("roost_secret NODE_KEY").stdout, FILE_NODE_KEY)

    def test_neither_arm_answers(self):
        self.write_curl(fail=True)
        r = self.sh("roost_secret NODE_KEY")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.sh("roost_secret_source NODE_KEY").stdout.strip(), "none")
        self.assertEqual(self.sh("roost_secret_source CI_KEY").stdout.strip(), "none")

    def test_an_empty_legacy_file_is_no_answer(self):
        self.write_curl(fail=True)
        self.write_legacy("NODE_KEY", "")
        self.assertEqual(self.sh("roost_secret_source NODE_KEY").stdout.strip(), "none")

    def test_an_unknown_name_has_no_legacy_file(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.assertEqual(self.sh("roost_secret_source HA_TOKEN").stdout.strip(), "none")

    def test_the_environment_is_read_when_the_rc_is_absent(self):
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        r = self.sh("roost_secret NODE_KEY",
                    extra={"ROOST_VAULT_APP": APP, "ROOST_VAULT_APP_KEY": APP_KEY})
        self.assertEqual(r.stdout, VAULT_NODE_KEY)


class ShellCustodyTest(SecretFixture):
    def test_the_app_key_never_reaches_the_curl_argv(self):
        self.write_rc(ROOST_VAULT_URL="https://vault.test",
                      ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})

        self.assertEqual(self.sh("roost_secret NODE_KEY").stdout, VAULT_NODE_KEY)
        argv = self.curl_argv()
        self.assertNotIn(APP_KEY, argv, "the app key must travel in a header file, never in argv")
        self.assertIn("-H @", argv, "curl must be given a header file")
        self.assertIn(f"https://vault.test/api/apps/{APP}/secrets", argv)

    def test_the_header_file_is_removed_after_the_fetch(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.sh("roost_secret NODE_KEY")
        left = [n for n in os.listdir(self.tmpdir) if n.startswith("roost-secret-hdr")]
        self.assertEqual(left, [], "the header file must not outlive the fetch")

    def test_the_cache_is_private_to_this_account(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.sh("roost_secret NODE_KEY")
        self.assertEqual(oct(os.stat(self.cache_file()).st_mode)[-3:], "600")

    def test_an_app_name_that_is_not_a_filename_stays_in_tmpdir(self):
        self.write_rc(ROOST_VAULT_APP="../../etc/roost", ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.assertEqual(self.sh("roost_secret NODE_KEY").stdout, VAULT_NODE_KEY)
        written = [n for n in os.listdir(self.tmpdir) if n.startswith("roost-secrets-")]
        self.assertEqual(len(written), 1, written)


class ShellCacheTest(SecretFixture):
    def setUp(self):
        super().setUp()
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY, "CI_KEY": "ci-key-from-vault"})

    def test_a_second_read_inside_the_window_calls_vault_once(self):
        self.sh("roost_secret NODE_KEY")
        self.assertEqual(self.curl_calls(), 1)
        self.sh("roost_secret NODE_KEY")
        self.sh("roost_secret CI_KEY")
        self.assertEqual(self.curl_calls(), 1, "the 300 s cache must serve every read in the window")

    def test_a_read_past_the_window_refreshes(self):
        self.sh("roost_secret NODE_KEY")
        cache = self.cache_file()
        stale = os.stat(cache).st_mtime - 301
        os.utime(cache, (stale, stale))
        self.sh("roost_secret NODE_KEY")
        self.assertEqual(self.curl_calls(), 2, "a document older than 300 s must be fetched again")

    def test_a_cache_just_inside_the_window_is_still_used(self):
        self.sh("roost_secret NODE_KEY")
        cache = self.cache_file()
        fresh = os.stat(cache).st_mtime - 290
        os.utime(cache, (fresh, fresh))
        self.sh("roost_secret NODE_KEY")
        self.assertEqual(self.curl_calls(), 1)


class VaultDoor(BaseHTTPRequestHandler):
    """Stands in for vault: one sealed document, and it checks the bearer token."""

    document = {}
    app_key = APP_KEY
    requests = None

    def do_GET(self):
        VaultDoor.requests.append((self.path, self.headers.get("authorization"),
                                   self.headers.get("user-agent")))
        if self.headers.get("authorization") != f"Bearer {VaultDoor.app_key}":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(VaultDoor.document).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PythonMirrorTest(SecretFixture):
    """The Python module against a real HTTP door, in a subprocess so the environment is clean."""

    def setUp(self):
        super().setUp()
        VaultDoor.requests = []
        VaultDoor.document = {"NODE_KEY": VAULT_NODE_KEY}
        self.server = HTTPServer(("127.0.0.1", 0), VaultDoor)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def py(self, expression, extra=None):
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from roost_secret import roost_secret, roost_secret_source\n"
            "sys.stdout.write(str(%s))\n" % (os.path.join(ROOT, "lib"), expression)
        )
        r = subprocess.run([sys.executable, "-c", script], env=self.env(extra),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_vault_answers(self):
        self.write_rc(ROOST_VAULT_URL=self.url, ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)
        self.assertEqual(self.py('roost_secret("NODE_KEY")'), VAULT_NODE_KEY)
        self.assertEqual(self.py('roost_secret_source("NODE_KEY")'), "vault")
        path, auth, agent = VaultDoor.requests[0]
        self.assertEqual(path, f"/api/apps/{APP}/secrets")
        self.assertEqual(auth, f"Bearer {APP_KEY}")
        # Cloudflare 403s the default Python-urllib agent - see tapo-poll.py.
        self.assertTrue(agent.startswith("roost-secret/"), agent)

    def test_a_wrong_app_key_falls_back_to_the_file(self):
        self.write_rc(ROOST_VAULT_URL=self.url, ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY="wrong")
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)
        self.assertEqual(self.py('roost_secret("NODE_KEY")'), FILE_NODE_KEY)
        self.assertEqual(self.py('roost_secret_source("NODE_KEY")'), "file")

    def test_no_vault_and_no_file(self):
        self.assertEqual(self.py('roost_secret("NODE_KEY")'), "")
        self.assertEqual(self.py('roost_secret_source("NODE_KEY")'), "none")
        self.assertEqual(self.py('roost_secret_source("CI_KEY")'), "none")

    def test_the_cache_serves_a_second_read(self):
        self.write_rc(ROOST_VAULT_URL=self.url, ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.py('roost_secret("NODE_KEY")')
        self.py('roost_secret("NODE_KEY")')
        self.assertEqual(len(VaultDoor.requests), 1)
        self.assertEqual(oct(os.stat(self.cache_file()).st_mode)[-3:], "600")

    def test_a_cache_past_the_window_refreshes(self):
        self.write_rc(ROOST_VAULT_URL=self.url, ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.py('roost_secret("NODE_KEY")')
        cache = self.cache_file()
        stale = os.stat(cache).st_mtime - 301
        os.utime(cache, (stale, stale))
        self.py('roost_secret("NODE_KEY")')
        self.assertEqual(len(VaultDoor.requests), 2)

    def test_both_languages_share_one_cache(self):
        """The shell writes the document and Python reads it, so a host fetches it once for every tool."""
        self.write_rc(ROOST_VAULT_URL="https://vault.test",
                      ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.assertEqual(self.sh("roost_secret NODE_KEY").stdout, VAULT_NODE_KEY)
        # The Python side points at the live door, which would answer with a different document.
        VaultDoor.document = {"NODE_KEY": "a-document-python-should-not-need"}
        self.assertEqual(self.py('roost_secret("NODE_KEY")',
                                 extra={"ROOST_VAULT_URL": self.url}), VAULT_NODE_KEY)
        self.assertEqual(VaultDoor.requests, [])


class SecretsVerbTest(SecretFixture):
    """`roost secrets` - the check a person runs after editing the rc, and after deleting a file."""

    def roost(self, *args, extra=None):
        return subprocess.run(["bash", os.path.join(ROOT, "bin", "roost"), *args],
                              env=self.env(extra), capture_output=True, text=True, timeout=60)

    def test_a_host_with_neither_arm_says_none(self):
        r = self.roost("secrets")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "NODE_KEY none\nCI_KEY none\n")

    def test_a_host_still_on_its_files_says_file(self):
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)
        self.write_legacy("CI_KEY", FILE_CI_KEY)
        r = self.roost("secrets")
        self.assertEqual(r.stdout, "NODE_KEY file\nCI_KEY file\n")

    def test_a_host_on_vault_says_vault(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY, "CI_KEY": "ci-key-from-vault"})
        r = self.roost("secrets")
        self.assertEqual(r.stdout, "NODE_KEY vault\nCI_KEY vault\n")

    def test_a_host_mid_cutover_says_both(self):
        """One name moved and the other did not, which is what a cutover looks like from the box."""
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY})
        self.write_legacy("CI_KEY", FILE_CI_KEY)
        self.assertEqual(self.roost("secrets").stdout, "NODE_KEY vault\nCI_KEY file\n")

    def test_no_value_ever_reaches_the_output(self):
        self.write_rc(ROOST_VAULT_APP=APP, ROOST_VAULT_APP_KEY=APP_KEY)
        self.write_curl({"NODE_KEY": VAULT_NODE_KEY, "CI_KEY": "ci-key-from-vault"})
        self.write_legacy("NODE_KEY", FILE_NODE_KEY)
        r = self.roost("secrets")
        for value in (VAULT_NODE_KEY, FILE_NODE_KEY, "ci-key-from-vault", APP_KEY):
            self.assertNotIn(value, r.stdout + r.stderr)

    def test_the_help_names_the_verb(self):
        self.assertIn("roost secrets", self.roost("help").stdout)


if __name__ == "__main__":
    unittest.main()
