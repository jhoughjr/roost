# The Swift job image for opi-forge, for the GitHub independence spike.
#
# A container job runs JavaScript actions inside the container, and `actions/checkout` and `actions/cache` are both JavaScript.
# GitHub's runner mounts its own Node into the container. act_runner does not, so the image has to carry one.
# Without this a container job fails with `exec: "node": executable file not found in $PATH` before any Swift runs.
FROM swift:6.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs \
      ca-certificates \
      curl \
      git \
 && rm -rf /var/lib/apt/lists/*

RUN node --version && swift --version && git --version
