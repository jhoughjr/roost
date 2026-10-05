# The Swift 6.1 job image for opi-forge, for vault's CI.
#
# The tag matches vault's own Dockerfile, so CI tests the toolchain that deploys rather than a newer one.
# Node is here because a container job runs its JavaScript actions inside the container, and act_runner mounts none of its own.
FROM swift:6.1-noble

# The weekly prune on the box removes any image no container uses that is a week old, and a job image is in use only while
# a job runs. On 2026-10-04 it took this image and every job that names it failed at the pull. The prune skips this label.
LABEL house.job-image="1"
# The Swift this image carries. `job-images.py` reads it from the label, so the report starts no container to learn it.
# The base tag follows the 6.1 line, so this names the line and not a patch release.
LABEL house.swift="6.1"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs \
      ca-certificates \
      curl \
      git \
 && rm -rf /var/lib/apt/lists/*

RUN node --version && swift --version
