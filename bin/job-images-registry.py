#!/usr/bin/env python3
"""job-images-registry.py - push a forge job image to the forge's registry, and restore a lost one from it.

A job image is built on the opi. Until it has a package, a lost image is a build of 2 to 6 GB from Docker Hub and the apt mirrors.
The image store was lost on 2026-09-21 and the weekly prune took two images on 2026-10-04 (jimmy/house#98).
With a package, a lost image is a pull from the forge on the same box (jimmy/house#101).

  job-images-registry.py push IMAGE [--dry-run]      tag IMAGE for the registry, push it, and record the package on this box
  job-images-registry.py restore IMAGE [--dry-run]   pull the package and tag it back to the local name

The local name stays. `roost-ci:arm64` goes to `forgejo.jimmyhoughjr.net/jimmy/roost-ci:arm64`, and a restore tags it back,
so no workflow and no runner label changes and the runner needs no registry login.

Both verbs are a person's step and no timer runs either. `--dry-run` prints each docker command and the bytes it writes, and runs none.
A push writes the compressed layers to the forge's package store, which is under /var/lib/dokku on the root disk.
A restore writes the layers and their unpacked copy to docker's store on the NVMe, and a sustained write there has reset the opi.
Run one image at a time, at a time when a reset costs little, with `box-watch` beside it.

The push signs in with a token of its own, with `write:package`, read from the mode 600 file FORGE_JOB_IMAGE_TOKEN_FILE names
(default ~/.forge_job_image_token) or from this host's vault document under FORGE_JOB_IMAGE_TOKEN.
The sign-in goes into a throwaway docker config directory, so the box's own read-only login is left as it was, and the token
goes to docker on stdin and never on a command line. The restore uses the box's existing login.

Environment: FORGE_REGISTRY (forgejo.jimmyhoughjr.net), FORGE_REGISTRY_OWNER (jimmy), FORGE_REGISTRY_USER (the owner),
FORGE_JOB_IMAGE_TOKEN_FILE, ROOST_JOB_IMAGES_PUSHED (~/.roost-job-images.pushed.json).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

HOME = os.path.expanduser("~")
REGISTRY = os.environ.get("FORGE_REGISTRY", "forgejo.jimmyhoughjr.net")
OWNER = os.environ.get("FORGE_REGISTRY_OWNER", "jimmy")
USER = os.environ.get("FORGE_REGISTRY_USER", OWNER)
TOKEN_NAME = "FORGE_JOB_IMAGE_TOKEN"
TOKEN_FILE = os.environ.get("FORGE_JOB_IMAGE_TOKEN_FILE", os.path.join(HOME, ".forge_job_image_token"))
PUSHED_FILE = os.environ.get("ROOST_JOB_IMAGES_PUSHED", os.path.join(HOME, ".roost-job-images.pushed.json"))
# Only an image built on the box goes to the registry by this door. Any other name already has a registry of its own.
LOCAL_PREFIX = "roost-"
IMAGE_NAME = re.compile(r"^roost-[a-z0-9._-]+:[A-Za-z0-9._-]+$")
SIZE = re.compile(r"^([0-9.]+)\s*([kMGT]?B)$")
WARNING = "A sustained write to the NVMe has reset the opi. Run one image at a time, with box-watch beside it."
# A push reads the NVMe and writes the root disk, so it is the lighter of the two, and it is still gigabytes on a box that has reset under load.
PUSH_WARNING = "The push writes the root disk and not the NVMe. It is still a long write on a box that has reset under load: run one image at a time, with box-watch beside it."


def docker(*args, config=None, stdin=None, quiet=False):
    """Run one docker call and return its exit code and output. A call that is not `quiet` prints as it runs, because a push takes minutes."""
    command = ["docker"] + (["--config", config] if config else []) + list(args)
    try:
        if quiet or stdin is not None:
            done = subprocess.run(command, input=stdin, capture_output=True, text=True)
            return done.returncode, done.stdout + done.stderr
        return subprocess.run(command).returncode, ""
    except OSError as error:
        return 127, str(error)


def package(image):
    return "%s/%s/%s" % (REGISTRY, OWNER, image)


def gigabytes(count):
    return "%.2f GB" % (count / 1e9) if count else "an unknown size"


def read_pushed():
    try:
        with open(PUSHED_FILE) as fh:
            found = json.load(fh)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def held(image):
    """The compressed bytes of an image the box holds, and the bytes its row in `docker images` reports, or None when it holds none."""
    code, out = docker("image", "inspect", "--format", "{{.Size}}", image, quiet=True)
    if code != 0 or not out.strip().isdigit():
        return None
    store = 0
    code, listing = docker("image", "ls", "--format", "{{json .}}", quiet=True)
    for line in listing.splitlines() if code == 0 else []:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        match = SIZE.match(entry.get("Size") or "")
        if match and "%s:%s" % (entry.get("Repository"), entry.get("Tag")) == image:
            store = int(float(match.group(1)) * {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[match.group(2)])
    return int(out.strip()), store


def read_token():
    """The push token, from its own file first and this host's vault document second. It is returned and never printed."""
    try:
        with open(TOKEN_FILE) as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    try:
        from roost_secret import roost_secret
        return roost_secret(TOKEN_NAME)
    except Exception:
        return ""


def push(image, dry_run):
    sizes = held(image)
    if sizes is None:
        print("job-images-registry: the box does not hold %s, so there is nothing to push" % image)
        return 1
    compressed, store = sizes
    target = package(image)
    steps = [
        "docker tag %s %s" % (image, target),
        "docker --config <a throwaway directory> login %s --username %s --password-stdin" % (REGISTRY, USER),
        "docker --config <a throwaway directory> push %s" % target,
        "docker rmi %s" % target,
    ]
    writes = ["nothing, a tag is a name", "nothing on the box",
              "up to %s of compressed layers to the forge's package store on the root disk; a layer the forge already holds is not sent again" % gigabytes(compressed),
              "nothing, it removes the registry name and %s stays" % image]
    if dry_run:
        print("push %s to %s - dry run, nothing runs" % (image, target))
        for step, write in zip(steps, writes):
            print("  %s\n      writes %s" % (step, write))
        print("  then records the package in %s" % PUSHED_FILE)
        print(PUSH_WARNING)
        return 0
    token = read_token()
    if not token:
        print("job-images-registry: no push token. %s is absent and this host's vault document holds no %s. "
              "The key is declared on the forge kind, and house-rotate places it." % (TOKEN_FILE, TOKEN_NAME))
        return 1
    print(PUSH_WARNING)
    config = tempfile.mkdtemp(prefix="job-images-registry-")
    os.chmod(config, 0o700)
    try:
        code, out = docker("tag", image, target, quiet=True)
        if code != 0:
            print("job-images-registry: docker tag failed: %s" % out.strip())
            return 1
        code, out = docker("login", REGISTRY, "--username", USER, "--password-stdin", config=config, stdin=token)
        if code != 0:
            # docker names the registry and the reason and never the password, and the token is cut out all the same.
            print("job-images-registry: the forge refused the sign-in: %s" % out.replace(token, "<token>").strip()[-300:])
            return 1
        code, _ = docker("push", target, config=config)
        if code != 0:
            print("job-images-registry: the push of %s failed, and %s is unchanged" % (target, image))
            return 1
    finally:
        shutil.rmtree(config, ignore_errors=True)
        docker("rmi", target, quiet=True)
    pushed = read_pushed()
    pushed[image] = {"registry": target, "pushedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "compressedBytes": compressed, "storeBytes": store}
    with open(PUSHED_FILE + ".new", "w") as fh:
        json.dump(pushed, fh, indent=1, sort_keys=True)
    os.replace(PUSHED_FILE + ".new", PUSHED_FILE)
    print("job-images-registry: %s is at %s, and a lost copy restores with: job-images-registry.py restore %s" % (image, target, image))
    return 0


def restore(image, dry_run):
    target = package(image)
    record = read_pushed().get(image) or {}
    sizes = held(image)
    if sizes is not None and not dry_run:
        print("job-images-registry: the box holds %s already, so nothing is restored" % image)
        return 0
    if sizes is not None and not record:
        # A dry run on a box that still holds the image reads the sizes from the image itself.
        record = {"compressedBytes": sizes[0], "storeBytes": sizes[1], "unpushed": True}
    steps = ["docker pull %s" % target, "docker tag %s %s" % (target, image), "docker rmi %s" % target]
    if record.get("storeBytes"):
        pull = "about %s to docker's store on the NVMe: %s of layers and their unpacked copy" % (
            gigabytes(record["storeBytes"]), gigabytes(record.get("compressedBytes")))
    else:
        pull = "an unknown size to docker's store on the NVMe, because this box has no record of the push; ci/README.md lists each image"
    writes = [pull, "nothing, a tag is a name", "nothing, it removes the registry name and %s stays" % image]
    if dry_run:
        print("restore %s from %s - dry run, nothing runs" % (image, target))
        for step, write in zip(steps, writes):
            print("  %s\n      writes %s" % (step, write))
        if not record or record.get("unpushed"):
            print("  this box has no record of a push of %s, so the pull fails if the forge holds no such package" % image)
        if sizes is not None:
            print("  the box holds %s now, so a run without --dry-run does nothing" % image)
        print(WARNING)
        return 0
    print(WARNING)
    code, _ = docker("pull", target)
    if code != 0:
        print("job-images-registry: the pull of %s failed. If the forge holds no such package, build the image: job-images.py check prints the line" % target)
        return 1
    code, out = docker("tag", target, image, quiet=True)
    if code != 0:
        print("job-images-registry: docker tag failed: %s" % out.strip())
        return 1
    docker("rmi", target, quiet=True)
    print("job-images-registry: %s is on the box again" % image)
    return 0


def main(argv):
    dry_run = "--dry-run" in argv
    words = [word for word in argv if word != "--dry-run"]
    if len(words) != 2 or words[0] not in ("push", "restore"):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    verb, image = words
    if not IMAGE_NAME.match(image):
        print("job-images-registry: %s is not a job image. A job image is named %s<name>:<tag>" % (image, LOCAL_PREFIX), file=sys.stderr)
        return 2
    return push(image, dry_run) if verb == "push" else restore(image, dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
