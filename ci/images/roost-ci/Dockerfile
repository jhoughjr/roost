# The job image for opi-forge, for the GitHub independence spike.
#
# act_runner runs each job in a container, and the workflows reach out to github.com from inside `run:` steps to fetch tools.
# No forge setting governs a step, so the only way to close that hole is to give the job an image that already carries what the step wants.
# roost/check.yml skips its own shellcheck download when shellcheck is on PATH, so nothing in the workflow changes.
FROM node:22-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      shellcheck \
      git \
      ca-certificates \
      curl \
      python3 \
 && rm -rf /var/lib/apt/lists/*

RUN shellcheck --version && git --version && python3 --version
