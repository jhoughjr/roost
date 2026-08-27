# The Swift 6.1 job image for opi-forge, for vault's CI.
#
# The tag matches vault's own Dockerfile, so CI tests the toolchain that deploys rather than a newer one.
# Node is here because a container job runs its JavaScript actions inside the container, and act_runner mounts none of its own.
FROM swift:6.1-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs \
      ca-certificates \
      curl \
      git \
 && rm -rf /var/lib/apt/lists/*

RUN node --version && swift --version
