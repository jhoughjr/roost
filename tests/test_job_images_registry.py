#!/usr/bin/env python3
"""Tests for bin/job-images-registry.py, the push of a forge job image to the forge's registry and the restore of a lost one.

Drives the real script in a throwaway HOME. A stub `docker` on PATH answers from tests/fake_docker_images.py, which keeps
the box's images and the forge's packages in one JSON file and logs each call. No daemon is asked, nothing is pushed or
pulled, and the token is made up.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "job-images-registry.py")
READER = os.path.join(ROOT, "bin", "job-images.py")
FAKE = os.path.join(ROOT, "tests", "fake_docker_images.py")

TOKEN = "made-up-push-token-7c1e"
IMAGE = "roost-swift-ci:6.3.2-noble"
PACKAGE = "forgejo.jimmyhoughjr.net/jimmy/roost-swift-ci:6.3.2-noble"
# The image as the opi held it on 2026-10-05: 1.38 GB of layers, and 5.8 GB with their unpacked copy.
SWIFT_632 = {"created": "2026-10-05T10:10:54-05:00", "size": "5.8GB", "bytes": 1375744359,
             "labels": {"house.job-image": "1", "house.swift": "6.3.2"}}


class JobImagesRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-job-images-registry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.stub)
        path = os.path.join(self.stub, "docker")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "docker.log")
        self.box(images={IMAGE: SWIFT_632})
        with open(os.path.join(self.home, ".forge_job_image_token"), "w") as fh:
            fh.write(TOKEN + "\n")

    # ── harness ──────────────────────────────────────────────────────────

    def box(self, images, registry=None, **more):
        with open(os.path.join(self.tmp, "box.json"), "w") as fh:
            json.dump(dict({"config": None, "images": images, "registry": registry or {}}, **more), fh)

    def state(self):
        with open(os.path.join(self.tmp, "box.json")) as fh:
            return json.load(fh)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def changes(self):
        """The calls that change the box or the forge, which a dry run must never make."""
        return [call["args"][0] for call in self.calls() if call["args"][0] in ("tag", "login", "push", "pull", "rmi", "build")]

    def run_script(self, *args, script=SCRIPT):
        env = dict(os.environ)
        env.update({"HOME": self.home, "PATH": self.stub + os.pathsep + env["PATH"], "FAKE_DOCKER_LOG": self.log,
                    "FAKE_DOCKER_IMAGES": os.path.join(self.tmp, "box.json"), "ROOST_WORKFLOW_ROOTS": self.home})
        for name in ("ROOST_JOB_IMAGES_PUSHED", "ROOST_JOB_IMAGES_SEEN", "FORGE_JOB_IMAGE_TOKEN_FILE", "FORGE_REGISTRY",
                     "FORGE_REGISTRY_OWNER", "FORGE_REGISTRY_USER", "ROOST_DECLARED_FILE", "ROOST_VAULT_APP_KEY"):
            env.pop(name, None)
        return subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True, timeout=60)

    def pushed(self):
        with open(os.path.join(self.home, ".roost-job-images.pushed.json")) as fh:
            return json.load(fh)

    # ── the push ─────────────────────────────────────────────────────────

    def test_a_push_tags_signs_in_pushes_and_leaves_the_local_name_alone(self):
        result = self.run_script("push", IMAGE)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["tag", "login", "push", "rmi"])
        state = self.state()
        self.assertIn(PACKAGE, state["registry"])
        # The registry name is gone from the box again, and the name a workflow uses is still there.
        self.assertEqual(list(state["images"]), [IMAGE])
        self.assertIn("job-images-registry.py restore " + IMAGE, result.stdout)

    def test_a_push_records_the_package_and_the_sizes_a_restore_will_write(self):
        self.run_script("push", IMAGE)

        record = self.pushed()[IMAGE]
        self.assertEqual(record["registry"], PACKAGE)
        self.assertEqual(record["compressedBytes"], 1375744359)
        self.assertEqual(record["storeBytes"], 5800000000)

    def test_the_token_goes_to_docker_on_stdin_and_reaches_no_argument_and_no_output(self):
        result = self.run_script("push", IMAGE)

        login = [call for call in self.calls() if call["args"][0] == "login"][0]
        self.assertEqual(login["stdin"], TOKEN)
        self.assertNotIn(TOKEN, json.dumps([call["args"] for call in self.calls()]))
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_the_sign_in_goes_to_a_throwaway_directory_that_is_removed(self):
        # The opi's own docker login is read:package and a deploy pulls with it, so the push must not write over it.
        self.run_script("push", IMAGE)

        login = [call for call in self.calls() if call["args"][0] == "login"][0]
        self.assertTrue(login["config"])
        self.assertFalse(os.path.exists(login["config"]))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".docker", "config.json")))

    def test_a_refused_sign_in_pushes_nothing_and_records_nothing(self):
        self.box(images={IMAGE: SWIFT_632}, refuse_login=True)

        result = self.run_script("push", IMAGE)

        self.assertEqual(result.returncode, 1)
        self.assertIn("the forge refused the sign-in", result.stdout)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertEqual(self.state()["registry"], {})
        self.assertFalse(os.path.exists(os.path.join(self.home, ".roost-job-images.pushed.json")))
        self.assertEqual(list(self.state()["images"]), [IMAGE])

    def test_a_push_with_no_token_says_where_the_token_comes_from_and_changes_nothing(self):
        os.remove(os.path.join(self.home, ".forge_job_image_token"))

        result = self.run_script("push", IMAGE)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no push token", result.stdout)
        self.assertEqual(self.changes(), [])

    def test_a_push_of_an_image_the_box_does_not_hold_is_refused(self):
        self.box(images={})

        result = self.run_script("push", IMAGE)

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not hold", result.stdout)
        self.assertEqual(self.changes(), [])

    def test_a_push_dry_run_prints_each_command_and_its_bytes_and_runs_none(self):
        result = self.run_script("push", IMAGE, "--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("docker tag %s %s" % (IMAGE, PACKAGE), result.stdout)
        self.assertIn("login forgejo.jimmyhoughjr.net --username jimmy --password-stdin", result.stdout)
        self.assertIn("push " + PACKAGE, result.stdout)
        self.assertIn("up to 1.38 GB of compressed layers", result.stdout)
        self.assertIn("has reset under load", result.stdout)

    # ── the restore ──────────────────────────────────────────────────────

    def test_a_restore_pulls_the_package_and_tags_it_back_to_the_local_name(self):
        self.box(images={}, registry={PACKAGE: SWIFT_632})

        result = self.run_script("restore", IMAGE)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["pull", "tag", "rmi"])
        self.assertEqual(list(self.state()["images"]), [IMAGE])
        # The restore uses the box's own login, so it signs in nowhere and reads no token.
        self.assertEqual([call for call in self.calls() if call["config"]], [])

    def test_a_restore_of_an_image_the_box_holds_does_nothing(self):
        result = self.run_script("restore", IMAGE)

        self.assertEqual(result.returncode, 0)
        self.assertIn("holds %s already" % IMAGE, result.stdout)
        self.assertEqual(self.changes(), [])

    def test_a_restore_with_no_package_on_the_forge_fails_and_points_at_the_build(self):
        self.box(images={})

        result = self.run_script("restore", IMAGE)

        self.assertEqual(result.returncode, 1)
        self.assertIn("job-images.py check prints the line", result.stdout)
        self.assertEqual(self.state()["images"], {})

    def test_a_restore_dry_run_names_the_bytes_the_push_recorded(self):
        self.run_script("push", IMAGE)
        self.box(images={}, registry={PACKAGE: SWIFT_632})
        os.remove(self.log)

        result = self.run_script("restore", IMAGE, "--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("docker pull " + PACKAGE, result.stdout)
        self.assertIn("docker tag %s %s" % (PACKAGE, IMAGE), result.stdout)
        self.assertIn("about 5.80 GB to docker's store on the NVMe: 1.38 GB of layers", result.stdout)
        self.assertIn("has reset the opi", result.stdout)

    def test_a_restore_dry_run_of_a_held_image_plans_from_the_image_and_says_a_real_run_does_nothing(self):
        result = self.run_script("restore", IMAGE, "--dry-run")

        self.assertEqual(self.changes(), [])
        self.assertIn("about 5.80 GB to docker's store on the NVMe", result.stdout)
        self.assertIn("a run without --dry-run does nothing", result.stdout)

    def test_a_restore_dry_run_on_a_box_with_no_record_says_the_size_is_unknown(self):
        self.box(images={})

        result = self.run_script("restore", IMAGE, "--dry-run")

        self.assertIn("an unknown size", result.stdout)
        self.assertIn("no record of a push", result.stdout)

    # ── what is refused before docker is asked ───────────────────────────

    def test_a_name_that_is_not_a_job_image_is_refused(self):
        for name in ("dokku/vault:latest", "swift:6.1-noble", "roost-ci", "forgejo.jimmyhoughjr.net/jimmy/roost-ci:arm64"):
            result = self.run_script("push", name)
            self.assertEqual(result.returncode, 2, name)
        self.assertEqual(self.calls(), [])

    # ── the reader names the restore once a package exists ───────────────

    def test_check_names_the_restore_for_an_image_that_was_pushed_and_then_lost(self):
        self.run_script("--rows", script=READER)
        self.run_script("push", IMAGE)
        self.box(images={"roost-ci:arm64": {"size": "1.98GB"}}, registry={PACKAGE: SWIFT_632})

        result = self.run_script("check", script=READER)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Restore: ~/roost/bin/job-images-registry.py restore " + IMAGE, result.stdout)
        self.assertNotIn("docker build -t " + IMAGE, result.stdout)

    def test_the_row_of_a_pushed_image_carries_its_package(self):
        self.run_script("push", IMAGE)

        rows = {row["name"]: row for row in json.loads(self.run_script("--rows", script=READER).stdout)}

        self.assertEqual(rows[IMAGE]["registry"], PACKAGE)


if __name__ == "__main__":
    unittest.main()
