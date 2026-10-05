#!/usr/bin/env python3
"""A stand-in for the docker calls bin/job-images.py and bin/job-images-registry.py make, answered from the JSON file $FAKE_DOCKER_IMAGES names.

The file holds `config`, the runner's config text or null for a box with no runner, `images`, a map of `name:tag` to
`created`, `size`, `bytes`, `id` and `labels`, and `registry`, the packages the forge holds under their full names.
A tag, a push, a pull and an rmi change the file, so a test reads the box after a run as it would read the real one.
When $FAKE_DOCKER_LOG names a file, each call is written to it as one JSON line: the arguments, and what arrived on stdin for a login.
A test stub `docker` hands its arguments to this file.
"""
import json
import os
import sys

path = os.environ["FAKE_DOCKER_IMAGES"]
with open(path) as fh:
    box = json.load(fh)
box.setdefault("registry", {})
args = sys.argv[1:]
config_dir = None
if args[:1] == ["--config"]:
    config_dir, args = args[1], args[2:]


def save():
    with open(path, "w") as out:
        json.dump(box, out)


log = os.environ.get("FAKE_DOCKER_LOG")
entry = {"args": args, "config": config_dir}
if args[:1] == ["login"]:
    entry["stdin"] = sys.stdin.read()
if log:
    with open(log, "a") as out:
        out.write(json.dumps(entry) + "\n")

if args[:1] == ["exec"] and "cat" in args:
    if box.get("config") is None:
        sys.exit(1)
    sys.stdout.write(box["config"])
elif args[:2] == ["image", "ls"]:
    for name, image in box["images"].items():
        repository, _, tag = name.rpartition(":")
        print(json.dumps({"Repository": repository, "Tag": tag, "ID": image.get("id", "0123456789abcdef"),
                          "CreatedAt": image.get("created", ""), "Size": image.get("size", "1GB")}))
elif args[:2] == ["image", "inspect"]:
    for name in args[4:]:
        image = box["images"].get(name)
        if image is None:
            sys.exit(1)
        if args[3] == "{{.Size}}":
            print(image.get("bytes", 1000000000))
        else:
            print("%s|%s|%s" % (json.dumps([name]), image.get("created", ""), json.dumps(image.get("labels") or {})))
elif args[:1] == ["tag"]:
    if args[1] not in box["images"]:
        sys.exit(1)
    box["images"][args[2]] = box["images"][args[1]]
    save()
elif args[:1] == ["login"]:
    # The forge refuses a sign-in the test marks as refused, the way it refuses a token with no write:package.
    if box.get("refuse_login"):
        sys.stderr.write("Error response from daemon: login attempt failed with status: 401 Unauthorized\n")
        sys.exit(1)
    with open(os.path.join(config_dir, "config.json"), "w") as out:
        out.write("{}")
elif args[:1] == ["push"]:
    # A push with no sign-in in its own config directory is refused, which is what holds the push to its throwaway login.
    if args[1] not in box["images"] or not (config_dir and os.path.exists(os.path.join(config_dir, "config.json"))):
        sys.exit(1)
    box["registry"][args[1]] = box["images"][args[1]]
    save()
elif args[:1] == ["pull"]:
    if args[1] not in box["registry"]:
        sys.stderr.write("Error response from daemon: manifest unknown\n")
        sys.exit(1)
    box["images"][args[1]] = box["registry"][args[1]]
    save()
elif args[:1] == ["rmi"]:
    if box["images"].pop(args[1], None) is None:
        sys.exit(1)
    save()
else:
    sys.exit(1)
