#!/usr/bin/env python3
"""Tests for bin/job-images.py, the reading of the forge job images a build box holds.

Drives the real script in a throwaway HOME. A stub `docker` on PATH answers from tests/fake_docker_images.py, so no
daemon is asked and no image is touched. The box in these tests is the opi as it stood on 2026-10-05: the weekly prune
had removed roost-ci:arm64 and roost-swift-ci:6.3.2-noble the day before, and roost-swift-ci:6.1-noble was left.

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
SCRIPT = os.path.join(ROOT, "bin", "job-images.py")
FAKE = os.path.join(ROOT, "tests", "fake_docker_images.py")

# The runner's config as the container holds it: the labels, a host label, and the cache secret beside them.
CONFIG = """runner:
  capacity: 2
  labels:
    - "self-hosted:docker://roost-ci:arm64"
    - "linux:docker://roost-ci:arm64"
    - "ubuntu-latest:docker://roost-ci:arm64"
    - "mini:host"
cache:
  secret: "made-up-cache-secret"
"""

SWIFT_61 = {"created": "2026-08-26T23:03:00-05:00", "size": "4.53GB", "id": "1a01cea09aac7193"}
SWIFT_632 = {"created": "2026-10-05T11:00:00-05:00", "size": "4.8GB", "labels": {"house.job-image": "1", "house.swift": "6.3.2"}}
ROOST_CI = {"created": "2026-10-05T10:30:00-05:00", "size": "1.4GB", "labels": {"house.job-image": "1"}}
DOKKU_APP = {"created": "2026-10-05T09:48:46-05:00", "size": "94.1MB"}


class JobImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roost-job-images-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stub = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.stub)
        path = os.path.join(self.stub, "docker")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.box(images={"roost-swift-ci:6.1-noble": SWIFT_61, "dokku/status:latest": DOKKU_APP})

    # ── harness ──────────────────────────────────────────────────────────

    def box(self, images, config=CONFIG):
        with open(os.path.join(self.tmp, "box.json"), "w") as fh:
            json.dump({"config": config, "images": images}, fh)

    def workflow(self, repo, text, directory=".forgejo"):
        where = os.path.join(self.home, repo, directory, "workflows")
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "ci.yml"), "w") as fh:
            fh.write(text)

    def host_runner(self, swift=True):
        """A runner in host mode as the mini holds one: a config with host labels and the cache secret, a `.runner` file with its
        name and its token, and a Swift and an Xcode on the PATH."""
        where = os.path.join(self.home, "forgejo-runner")
        os.makedirs(where)
        with open(os.path.join(where, "config.yaml"), "w") as fh:
            fh.write('runner:\n  labels:\n    - "self-hosted:host"\n    - "macos:host"\n    - "mini:host"\ncache:\n  secret: "made-up-cache-secret"\n')
        with open(os.path.join(where, ".runner"), "w") as fh:
            json.dump({"name": "mini-forge", "token": "made-up-runner-token", "labels": ["self-hosted:host"]}, fh)
        tools = {"xcodebuild": "printf 'Xcode 27.0\\nBuild version 27A266a\\n'\n"}
        if swift:
            tools["swift"] = ("printf 'swift-driver version: 1.168.6 Apple Swift version 6.4 (swiftlang-6.4.0.34.1 clang-2100.3.34.1)\\n"
                              "Target: arm64-apple-macosx26.0\\n'\n")
        else:
            tools["swift"] = "exit 127\n"
        for name, body in tools.items():
            path = os.path.join(self.stub, name)
            with open(path, "w") as fh:
                fh.write("#!/usr/bin/env bash\necho run >> %s\n%s" % (os.path.join(self.tmp, name + ".calls"), body))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        # pgrep finds no daemon in a test, so the config is found at its usual path under HOME.
        path = os.path.join(self.stub, "pgrep")
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env bash\nexit 1\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def toolchain(self):
        result = self.run_script("--toolchain")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def run_script(self, *args):
        env = dict(os.environ)
        env.update({"HOME": self.home, "PATH": self.stub + os.pathsep + env["PATH"],
                    "FAKE_DOCKER_IMAGES": os.path.join(self.tmp, "box.json"), "ROOST_WORKFLOW_ROOTS": self.home})
        for name in ("ROOST_DECLARED_FILE", "ROOST_JOB_IMAGES_SEEN", "ROOST_BOX_NAMES", "ROOST_JOB_IMAGES_PUSHED",
                     "FORGE_HOST_RUNNER_CONFIG", "ROOST_JOB_TOOLCHAIN_CACHE"):
            env.pop(name, None)
        return subprocess.run([sys.executable, SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60)

    def rows(self):
        result = self.run_script("--rows")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["name"]: row for row in json.loads(result.stdout)}

    # ── what the box holds ───────────────────────────────────────────────

    def test_a_held_image_is_a_row_with_its_swift_its_date_and_its_size(self):
        self.box(images={"roost-swift-ci:6.3.2-noble": SWIFT_632, "roost-ci:arm64": ROOST_CI})

        row = self.rows()["roost-swift-ci:6.3.2-noble"]

        self.assertEqual(row["kind"], "job-image")
        self.assertEqual(row["state"], "held")
        self.assertEqual((row["repository"], row["tag"]), ("roost-swift-ci", "6.3.2-noble"))
        self.assertEqual((row["swift"], row["swiftSource"]), ("6.3.2", "label"))
        self.assertEqual(row["created"], "2026-10-05T11:00:00-05:00")
        self.assertEqual(row["sizeMb"], 4800)
        self.assertTrue(row["kept"])
        self.assertEqual(row["recipe"], "ci/images/roost-swift-ci-6.3.2-noble")
        self.assertTrue(row["image"] and row["served"] and row["running"])

    def test_an_image_with_no_swift_label_reads_the_version_from_its_tag_and_says_so(self):
        # The image the prune left was built before the recipes carried a label.
        row = self.rows()["roost-swift-ci:6.1-noble"]

        self.assertEqual((row["swift"], row["swiftSource"]), ("6.1", "tag"))
        self.assertFalse(row["kept"])
        self.assertEqual(row["id"], "1a01cea09aac")

    def test_an_image_with_no_swift_in_it_claims_none(self):
        self.box(images={"roost-ci:arm64": ROOST_CI})

        row = self.rows()["roost-ci:arm64"]

        self.assertIsNone(row["swift"])
        self.assertIsNone(row["swiftSource"])

    def test_an_app_image_is_not_a_job_image(self):
        self.assertNotIn("dokku/status:latest", self.rows())

    # ── what a job wants and the box does not hold ───────────────────────

    def test_a_runner_label_whose_image_is_gone_is_a_missing_row(self):
        row = self.rows()["roost-ci:arm64"]

        self.assertEqual(row["state"], "missing")
        self.assertEqual(row["labels"], ["self-hosted", "linux", "ubuntu-latest"])
        self.assertTrue(row["wanted"])
        self.assertFalse(row["image"])
        self.assertIsNone(row["sizeMb"])
        self.assertEqual(row["recipe"], "ci/images/roost-ci")

    def test_a_workflow_that_names_a_local_image_wants_it(self):
        # hatchery names its image in the job, so no runner label mentions it.
        self.workflow("hatchery", "jobs:\n  test:\n    runs-on: [self-hosted, linux, arm64]\n"
                                  "    container: roost-swift-ci:6.3.2-noble\n    steps:\n      - run: swift test\n")

        row = self.rows()["roost-swift-ci:6.3.2-noble"]

        self.assertEqual(row["state"], "missing")
        self.assertEqual(row["workflows"], ["hatchery ci.yml test"])

    def test_a_workflow_that_names_no_container_wants_the_image_its_label_maps_to(self):
        # jimmy/atlas on 2026-10-05: no container, and the label sent it to an image the box no longer held.
        self.workflow("atlas", "jobs:\n  test:\n    runs-on:\n      - self-hosted\n      - linux\n    steps:\n      - run: swift test\n")

        self.assertEqual(self.rows()["roost-ci:arm64"]["workflows"], ["atlas ci.yml test"])

    def test_a_container_written_as_a_block_is_read_by_its_image(self):
        self.workflow("vault-hb", "jobs:\n  test:\n    runs-on: ubuntu-latest\n    container:\n      image: roost-swift-ci:6.1-noble\n")

        self.assertEqual(self.rows()["roost-swift-ci:6.1-noble"]["workflows"], ["vault-hb ci.yml test"])

    def test_a_registry_image_a_workflow_names_is_not_missing(self):
        # docker pulls node:22-alpine on demand, so its absence is no fault.
        self.workflow("coop", "jobs:\n  check:\n    runs-on: ubuntu-latest\n    container: node:22-alpine\n")

        self.assertNotIn("node:22-alpine", self.rows())

    def test_the_forgejo_directory_hides_the_github_one(self):
        self.workflow("rookery", "jobs:\n  test:\n    runs-on: ubuntu-latest\n    container: roost-swift-ci:6.1-noble\n")
        self.workflow("rookery", "jobs:\n  test:\n    runs-on: ubuntu-latest\n    container: roost-swift-ci:9.9\n", directory=".github")

        self.assertNotIn("roost-swift-ci:9.9", self.rows())

    def test_an_image_the_box_held_once_is_missing_when_it_goes(self):
        # Nothing on the opi names roost-swift-ci:6.3.2-noble, so the box's own memory of holding it is what notices the loss.
        self.box(images={"roost-swift-ci:6.3.2-noble": SWIFT_632, "roost-swift-ci:6.1-noble": SWIFT_61})
        self.rows()
        self.box(images={"roost-swift-ci:6.1-noble": SWIFT_61})

        row = self.rows()["roost-swift-ci:6.3.2-noble"]

        self.assertEqual(row["state"], "missing")
        self.assertTrue(row["heldBefore"])
        self.assertEqual(row["lastSeen"], "2026-10-05T11:00:00-05:00")

    def test_forget_stops_the_question_about_a_removed_image(self):
        self.box(images={"roost-swift-ci:6.3.2-noble": SWIFT_632, "roost-swift-ci:6.1-noble": SWIFT_61})
        self.rows()
        self.box(images={"roost-swift-ci:6.1-noble": SWIFT_61})

        result = self.run_script("--forget", "roost-swift-ci:6.3.2-noble")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("roost-swift-ci:6.3.2-noble", self.rows())

    def test_the_declaration_names_an_image_when_the_runner_block_carries_one(self):
        declared = {"stacks": [{"name": "box", "backend": "host", "host": "jimmy@opi.jimmyhoughjr.net", "services": [
            {"name": "act_runner", "kind": "act-runner",
             "runner": {"registration": "opi-forge", "images": [{"image": "roost-swift-ci:6.3.2-noble", "recipe": "ci/images/roost-swift-ci-6.3.2-noble"}]}}]}]}
        with open(os.path.join(self.home, ".roost-reconcile-declared.json"), "w") as fh:
            json.dump(declared, fh)

        row = self.rows()["roost-swift-ci:6.3.2-noble"]

        self.assertEqual(row["state"], "missing")
        self.assertTrue(row["declared"])

    def test_a_declared_image_with_a_registry_restores_and_does_not_build(self):
        # jimmy/hatchery#4 gives the runner's block the field. Until it does, the push's own record names the package.
        declared = {"stacks": [{"name": "box", "backend": "host", "host": "jimmy@opi.jimmyhoughjr.net", "services": [
            {"name": "act_runner", "kind": "act-runner", "runner": {"registration": "opi-forge", "images": [
                {"image": "roost-swift-ci:6.3.2-noble", "recipe": "ci/images/roost-swift-ci-6.3.2-noble",
                 "registry": "forgejo.jimmyhoughjr.net/jimmy/roost-swift-ci:6.3.2-noble"}]}}]}]}
        with open(os.path.join(self.home, ".roost-reconcile-declared.json"), "w") as fh:
            json.dump(declared, fh)

        self.assertEqual(self.rows()["roost-swift-ci:6.3.2-noble"]["registry"], "forgejo.jimmyhoughjr.net/jimmy/roost-swift-ci:6.3.2-noble")
        result = self.run_script("check")

        self.assertIn("Restore: ~/roost/bin/job-images-registry.py restore roost-swift-ci:6.3.2-noble", result.stdout)
        self.assertNotIn("docker build -t roost-swift-ci:6.3.2-noble", result.stdout)

    def test_an_image_with_no_package_has_no_registry_and_builds(self):
        self.assertIsNone(self.rows()["roost-ci:arm64"]["registry"])

    # ── a runner that runs its jobs on the host ──────────────────────────

    def test_a_host_runner_is_one_row_with_the_swift_and_the_xcode_of_the_host(self):
        # The mini on 2026-10-05: its runner has no image, so the toolchain a job tests is the host's.
        self.host_runner()

        row = self.toolchain()

        self.assertEqual((row["name"], row["kind"], row["state"]), ("mini-forge toolchain", "job-toolchain", "held"))
        self.assertEqual((row["swift"], row["swiftSource"]), ("6.4", "host"))
        self.assertEqual((row["xcode"], row["xcodeBuild"]), ("27.0", "27A266a"))
        self.assertEqual(row["runner"], "mini-forge")
        self.assertEqual(row["labels"], ["self-hosted", "macos", "mini"])
        self.assertEqual(row["names"], [])

    def test_a_host_with_no_runner_answers_nothing(self):
        # The air has no forge runner, and the opi's runs in a container whose config does not open on the host.
        result = self.run_script("--toolchain")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_a_host_runner_with_no_swift_is_a_missing_row(self):
        self.host_runner(swift=False)

        row = self.toolchain()

        self.assertEqual(row["state"], "missing")
        self.assertIsNone(row["swift"])
        self.assertFalse(row["served"])

    def test_the_toolchain_is_asked_once_an_hour_and_not_once_a_report(self):
        # The node report runs every 30 seconds.
        self.host_runner()
        self.toolchain()

        row = self.toolchain()

        self.assertEqual(row["swift"], "6.4")
        with open(os.path.join(self.tmp, "swift.calls")) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

    def test_the_toolchain_row_holds_no_secret_of_the_runner(self):
        self.host_runner()

        result = self.run_script("--toolchain")

        self.assertNotIn("made-up-cache-secret", result.stdout + result.stderr)
        self.assertNotIn("made-up-runner-token", result.stdout + result.stderr)

    def test_the_toolchain_row_fits_the_node_report(self):
        # pulse refuses a node report over 4096 bytes, and the mini's carries about 1.7 KB before this row.
        self.host_runner()

        self.assertLess(len(self.run_script("--toolchain").stdout), 400)

    # ── the check a person or a timer runs ───────────────────────────────

    def test_check_names_each_missing_image_with_its_build_line_and_exits_1(self):
        result = self.run_script("check")

        self.assertEqual(result.returncode, 1)
        self.assertIn("job image roost-ci:arm64: the box does not hold it, and the runner labels self-hosted linux ubuntu-latest map to it",
                      result.stdout)
        self.assertIn("Build: docker build -t roost-ci:arm64 ~/roost/ci/images/roost-ci", result.stdout)

    def test_check_is_quiet_and_exits_0_when_the_box_holds_what_is_wanted(self):
        self.box(images={"roost-ci:arm64": ROOST_CI, "roost-swift-ci:6.1-noble": SWIFT_61})

        result = self.run_script("check")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("does not hold", result.stdout)

    def test_the_table_marks_a_version_taken_from_the_tag(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("6.1*", result.stdout)
        self.assertIn("read from the tag", result.stdout)

    def test_the_cache_secret_in_the_runner_config_reaches_no_output(self):
        for args in ((), ("--rows",), ("check",)):
            result = self.run_script(*args)
            self.assertNotIn("made-up-cache-secret", result.stdout + result.stderr)

    def test_a_host_with_no_docker_reports_nothing_and_no_alarm(self):
        # A Mac runs the same script. With no image store there, a wanted image is not missing, it is somewhere else.
        self.box(images={}, config=None)
        self.workflow("hatchery", "jobs:\n  test:\n    runs-on: ubuntu-latest\n    container: roost-swift-ci:6.3.2-noble\n")

        self.assertEqual(self.rows(), {})
        self.assertEqual(self.run_script("check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
