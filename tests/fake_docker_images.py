#!/usr/bin/env python3
"""A stand-in for the three docker calls bin/job-images.py makes, answered from the JSON file $FAKE_DOCKER_IMAGES names.

The file holds `config`, the runner's config text or null for a box with no runner, and `images`, a map of
`name:tag` to `created`, `size`, `id` and `labels`. A test stub `docker` hands its arguments to this file.
"""
import json
import os
import sys

with open(os.environ["FAKE_DOCKER_IMAGES"]) as fh:
    box = json.load(fh)
args = sys.argv[1:]

if args[:1] == ["exec"] and "cat" in args:
    if box.get("config") is None:
        sys.exit(1)
    sys.stdout.write(box["config"])
elif args[:2] == ["image", "ls"]:
    for name, image in box["images"].items():
        repository, _, tag = name.partition(":")
        print(json.dumps({"Repository": repository, "Tag": tag, "ID": image.get("id", "0123456789abcdef"),
                          "CreatedAt": image.get("created", ""), "Size": image.get("size", "1GB")}))
elif args[:2] == ["image", "inspect"]:
    for name in args[4:]:
        image = box["images"].get(name)
        if image is None:
            sys.exit(1)
        print("%s|%s|%s" % (json.dumps([name]), image.get("created", ""), json.dumps(image.get("labels") or {})))
else:
    sys.exit(1)
