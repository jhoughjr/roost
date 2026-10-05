# The Swift job image for opi-forge, for the GitHub independence spike.
#
# A container job runs JavaScript actions inside the container, and `actions/checkout` and `actions/cache` are both JavaScript.
# GitHub's runner mounts its own Node into the container. act_runner does not, so the image has to carry one.
# Without this a container job fails with `exec: "node": executable file not found in $PATH` before any Swift runs.
FROM swift:6.3

# The weekly prune on the box removes any image no container uses that is a week old, and a job image is in use only while
# a job runs. On 2026-10-04 it took this image and every job that names it failed at the pull. The prune skips this label.
LABEL house.job-image="1"
# The Swift this image carries. `job-images.py` reads it from the label, so the report starts no container to learn it.
# The base tag follows the 6.3 line, so this names the line and not a patch release.
LABEL house.swift="6.3"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs \
      ca-certificates \
      curl \
      git \
 && rm -rf /var/lib/apt/lists/*

RUN node --version && swift --version && git --version
