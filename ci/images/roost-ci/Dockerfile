# The job image for opi-forge, for the GitHub independence spike.
#
# act_runner runs each job in a container, and the workflows reach out to github.com from inside `run:` steps to fetch tools.
# No forge setting governs a step, so the only way to close that hole is to give the job an image that already carries what the step wants.
# roost/check.yml skips its own shellcheck download when shellcheck is on PATH, so nothing in the workflow changes.
FROM node:22-bookworm

# The docker client and the Postgres client are here for MWServer's `test` job.
# That job declares no `container:` of its own, so it lands on whatever `ubuntu-latest` maps to, and it runs its suites inside the Dockerfile's Swift image over the runner's mounted socket.
# It also installs `postgresql-client` in a `run:` step today, to reach a Postgres service container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      shellcheck \
      git \
      ca-certificates \
      curl \
      python3 \
      docker.io \
      postgresql-client \
 && rm -rf /var/lib/apt/lists/*

RUN shellcheck --version && git --version && python3 --version && docker --version && psql --version
