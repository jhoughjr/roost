#!/usr/bin/env python3
"""job-images.py - the forge job images a build box holds, the Swift in each, and the ones a job wants that are gone.

A forge job runs in a container, and its image is built on the box and lives in no registry.
On 2026-10-04 the weekly prune removed roost-ci:arm64 and roost-swift-ci:6.3.2-noble, and the first sign was a failed job a day later.
This reads the box and builds nothing. A rebuild is a person's step, because a multi-GB write to the NVMe has reset the opi.

  job-images.py                  one line per job image: state, Swift, created, size, and what wants it
  job-images.py check            one line per wanted image the box does not hold, and exit 1 when there is one
  job-images.py --rows           the same reading as a JSON list, which the reconcile posts to pulse
  job-images.py --forget IMAGE   stop asking for an image this box held once and a person removed on purpose

Four things can want an image, and a row names each one that does:
  - a label of the forge runner on this box, read from the running container's config
  - the `images` list of the runner's block in hatchery's declaration, when the published copy carries one
  - a workflow in a checkout on this host, by its `container:` or by a `runs-on` label that maps to the image
  - this box itself, because it held the image on an earlier pass

Environment: FORGE_RUNNER_CONTAINER (act_runner), FORGE_RUNNER_CONFIG (/data/config.yaml), ROOST_WORKFLOW_ROOTS ($HOME:$HOME/repos),
ROOST_JOB_IMAGES_SEEN (~/.roost-job-images.seen.json), ROOST_DECLARED_FILE, ROOST_BOX_NAMES.
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECIPES = os.path.join(os.path.dirname(HERE), "ci", "images")
HOME = os.path.expanduser("~")
RUNNER = os.environ.get("FORGE_RUNNER_CONTAINER", "act_runner")
RUNNER_CONFIG = os.environ.get("FORGE_RUNNER_CONFIG", "/data/config.yaml")
SEEN_FILE = os.environ.get("ROOST_JOB_IMAGES_SEEN", os.path.join(HOME, ".roost-job-images.seen.json"))
# The label every recipe under ci/images carries, which is also what the weekly prune keeps.
KEEP_LABEL = "house.job-image"
SWIFT_LABEL = "house.swift"
# No registry serves a name with this prefix, so the box holds it or a job fails at the pull.
LOCAL_PREFIX = "roost-"

LABEL_LINE = re.compile(r'^\s*-\s*"?([^":\s]+):docker://([^"\s]+)"?\s*$')
SIZE = re.compile(r"^([0-9.]+)\s*([kMGT]?B)$")
SWIFT_TAG = re.compile(r"^(\d+\.\d+(?:\.\d+)?)")


def docker(*args):
    """The output of one docker call, or an empty string when docker or the object is not there."""
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def runner_labels():
    """Each `label, image` pair of the runner on this box, in the order its config writes them.

    The config also holds the cache secret, so only a line that matches a label leaves this function.
    """
    pairs = []
    for line in docker("exec", RUNNER, "cat", RUNNER_CONFIG).splitlines():
        match = LABEL_LINE.match(line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def box_names():
    names = set(os.environ.get("ROOST_BOX_NAMES", "").split())
    names.update(["127.0.0.1", "localhost", socket.gethostname(), os.environ.get("ROOST_BOX_NAME", "opi.jimmyhoughjr.net")])
    return names


def declared_images():
    """The images the runner's block declares for this box, as `image -> recipe`.

    Hatchery publishes the runner's label names today and no image, so this is empty until the block carries `images`.
    """
    wanted = {}
    files = [os.environ.get("ROOST_DECLARED_FILE", ""), os.path.join(HOME, ".roost-reconcile-declared.json"),
             os.path.join(HOME, ".roost-node-declared.json")]
    here = box_names()
    for path in files:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                declared = json.load(fh)
        except (OSError, ValueError):
            continue
        for stack in declared.get("stacks", []):
            if (stack.get("host") or "").split("@")[-1] not in here:
                continue
            for service in stack.get("services", []):
                for entry in (service.get("runner") or {}).get("images") or []:
                    if isinstance(entry, str):
                        wanted[entry] = None
                    elif isinstance(entry, dict) and entry.get("image"):
                        wanted[entry["image"]] = entry.get("recipe")
        break
    return wanted


def workflow_jobs():
    """Each job of each workflow this host can see, as `repo, file, job, runs-on labels, container image`.

    A repository with `.forgejo/workflows` is read from there alone, because the forge stops reading `.github/workflows` once it exists.
    The parse reads indentation and three keys. It is not a YAML reader, and a workflow that builds a name from an expression reads as that text.
    """
    jobs = []
    roots = os.environ.get("ROOST_WORKFLOW_ROOTS", HOME + os.pathsep + os.path.join(HOME, "repos")).split(os.pathsep)
    for root in roots:
        for repo in sorted(glob.glob(os.path.join(root, "*"))):
            files = sorted(glob.glob(os.path.join(repo, ".forgejo", "workflows", "*.y*ml")))
            files = files or sorted(glob.glob(os.path.join(repo, ".github", "workflows", "*.y*ml")))
            for path in files:
                try:
                    with open(path) as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                jobs.extend((os.path.basename(repo), os.path.basename(path)) + job for job in parse_jobs(lines))
    return jobs


def parse_jobs(lines):
    jobs, name, runs_on, image, in_jobs, in_container, in_runs_on = [], None, [], None, False, False, False

    def close():
        if name:
            jobs.append((name, runs_on, image))

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        text = raw.strip()
        if indent == 0:
            in_jobs = text.startswith("jobs:")
            continue
        if not in_jobs:
            continue
        if indent == 2 and text.endswith(":"):
            close()
            name, runs_on, image, in_container, in_runs_on = text[:-1], [], None, False, False
            continue
        if indent != 4:
            if in_container and text.startswith("image:"):
                image = text.split(":", 1)[1].strip().strip("\"'")
            elif in_runs_on and text.startswith("- "):
                runs_on.append(text[2:].strip().strip("\"'"))
            continue
        in_container = in_runs_on = False
        key, _, value = text.partition(":")
        value = value.split(" #")[0].strip()
        if key == "runs-on":
            if value.startswith("["):
                runs_on = [part.strip().strip("\"'") for part in value.strip("[]").split(",") if part.strip()]
            elif value:
                runs_on = [value.strip("\"'")]
            else:
                in_runs_on = True
        elif key == "container":
            if value:
                image = value.strip("\"'")
            else:
                in_container = True
    close()
    return jobs


def megabytes(text):
    match = SIZE.match(text or "")
    if not match:
        return None
    scale = {"B": 1 / 1e6, "kB": 1 / 1e3, "MB": 1.0, "GB": 1e3, "TB": 1e6}[match.group(2)]
    return round(float(match.group(1)) * scale)


def recipe_for(image):
    """The directory under ci/images that builds this image, by the two names the recipes use."""
    repository, _, tag = image.partition(":")
    for name in (repository + "-" + tag, repository):
        if os.path.isfile(os.path.join(RECIPES, name, "Dockerfile")):
            return "ci/images/" + name
    return None


def read_seen():
    try:
        with open(SEEN_FILE) as fh:
            seen = json.load(fh)
        return seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        return {}


def write_seen(seen):
    try:
        with open(SEEN_FILE + ".new", "w") as fh:
            json.dump(seen, fh, indent=1, sort_keys=True)
        os.replace(SEEN_FILE + ".new", SEEN_FILE)
    except OSError:
        pass


def reading():
    """One row per job image: each one the box holds, and each one something wants that the box does not hold."""
    held = {}
    for line in docker("image", "ls", "--format", "{{json .}}").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("Repository") and entry.get("Tag") not in (None, "", "<none>"):
            held[entry["Repository"] + ":" + entry["Tag"]] = entry
    # A box with no docker has no job image to hold, and saying every wanted image is missing there would be a false alarm.
    if not held:
        return []

    labels = runner_labels()
    by_label = dict(labels)
    declared = declared_images()
    seen = read_seen()
    workflows = {}
    for repo, name, job, runs_on, container in workflow_jobs():
        if container:
            images = [container]
        else:
            # A job that names no container starts in the image its `runs-on` labels map to on this runner.
            images = sorted(set(by_label[label] for label in runs_on if label in by_label))
        for image in images:
            workflows.setdefault(image, []).append("%s %s %s" % (repo, name, job))

    def local(image):
        return image.startswith(LOCAL_PREFIX)

    candidates = [image for image in held if local(image)]
    candidates += [image for _, image in labels] + list(declared) + list(seen)
    candidates += [image for image in workflows if image in held or local(image)]
    names = sorted(set(candidates))

    facts = {}
    present = [image for image in names if image in held]
    if present:
        out = docker("image", "inspect", "--format", "{{json .RepoTags}}|{{.Created}}|{{json .Config.Labels}}", *present)
        for line in out.splitlines():
            tags, _, rest = line.partition("|")
            created, _, image_labels = rest.partition("|")
            try:
                parsed = json.loads(image_labels) or {}
                tags = json.loads(tags) or []
            except ValueError:
                continue
            for tag in tags:
                facts[tag] = (created, parsed)

    rows = []
    for image in names:
        repository, _, tag = image.partition(":")
        created, image_labels = facts.get(image, (None, {}))
        entry = held.get(image)
        kept = KEEP_LABEL in image_labels
        if entry and (kept or local(image)):
            seen[image] = created or seen.get(image) or ""
        wanted_labels = [label for label, mapped in labels if mapped == image]
        held_before = image in seen and not entry
        wanted = bool(wanted_labels or image in declared or workflows.get(image) or held_before)
        swift, source = image_labels.get(SWIFT_LABEL), "label"
        if not swift:
            match = SWIFT_TAG.match(tag) if "swift" in repository else None
            swift, source = (match.group(1), "tag") if match else (None, None)
        rows.append({
            "name": image, "names": [], "kind": "job-image",
            "state": "held" if entry else "missing",
            "repository": repository, "tag": tag,
            "swift": swift, "swiftSource": source,
            "created": created, "sizeMb": megabytes(entry.get("Size")) if entry else None,
            "id": (entry.get("ID") or "").replace("sha256:", "")[:12] if entry else None,
            "kept": kept if entry else None,
            "wanted": wanted, "declared": image in declared, "labels": wanted_labels,
            "workflows": workflows.get(image, []), "heldBefore": held_before,
            "lastSeen": seen.get(image) or None if held_before else None,
            "recipe": declared.get(image) or recipe_for(image),
            "served": bool(entry), "running": bool(entry), "image": bool(entry),
        })
    write_seen(seen)
    return rows


def why(row):
    parts = []
    if row["labels"]:
        parts.append("the runner labels %s map to it" % " ".join(row["labels"]))
    if row["declared"]:
        parts.append("the runner's block declares it")
    if row["workflows"]:
        parts.append("%s name it" % ", ".join(row["workflows"]))
    if row["heldBefore"]:
        parts.append("this box held it%s" % (" until after " + row["lastSeen"][:10] if row["lastSeen"] else " before"))
    return "; ".join(parts) or "nothing this host can read names it"


def missing_line(row):
    build = "docker build -t %s ~/roost/%s" % (row["name"], row["recipe"]) if row["recipe"] else "no recipe under ci/images builds it"
    return "job image %s: the box does not hold it, and %s. A job that lands on it fails at the pull. Build: %s" % (row["name"], why(row), build)


def main(argv):
    if argv[:1] == ["--forget"] and len(argv) == 2:
        seen = read_seen()
        if seen.pop(argv[1], None) is None:
            print("job-images: %s was not on the list of images this box held" % argv[1])
            return 1
        write_seen(seen)
        print("job-images: %s is forgotten, and a pass that finds it gone says nothing" % argv[1])
        return 0
    rows = reading()
    if argv[:1] == ["--rows"]:
        print(json.dumps(rows))
        return 0
    if argv[:1] == ["check"]:
        missing = [row for row in rows if row["state"] == "missing"]
        for row in missing:
            print(missing_line(row))
        if not missing:
            print("job-images: the box holds every job image something wants (%d)" % len([row for row in rows if row["wanted"]]))
        return 1 if missing else 0
    if argv:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    if not rows:
        print("job-images: docker answers with no image here, so this host holds no job image")
        return 0
    print("%-30s %-8s %-7s %-11s %-8s %-5s %s" % ("image", "state", "swift", "created", "size", "kept", "wanted by"))
    for row in rows:
        swift = (row["swift"] or "-") + ("*" if row["swiftSource"] == "tag" else "")
        size = "%.2fGB" % (row["sizeMb"] / 1000.0) if row["sizeMb"] else "-"
        kept = "-" if row["kept"] is None else "yes" if row["kept"] else "no"
        print("%-30s %-8s %-7s %-11s %-8s %-5s %s" % (row["name"], row["state"], swift, (row["created"] or "-")[:10], size, kept, why(row)))
    if any(row["swiftSource"] == "tag" for row in rows):
        print("* the Swift version is read from the tag, because the image carries no %s label" % SWIFT_LABEL)
    if any(row["kept"] is False for row in rows):
        print("kept no: the image carries no %s label, so the weekly prune can remove it once it is a week old" % KEEP_LABEL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
