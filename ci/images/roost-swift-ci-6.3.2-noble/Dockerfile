# The Swift 6.3.2 job image for opi-forge, for MWServer.
#
# The tag matches the `container:` that MWServer's `fast_gate` and `build_push` jobs name, so a gate tests the toolchain that deploys.
#
# Node is here because a container job runs its JavaScript actions inside the container, and act_runner mounts none of its own.
# jemalloc is here because the release link passes `-Xlinker -ljemalloc`, and the workflow installs it in a `run:` step today.
# The docker client is here because the runner mounts its socket into the job, and only the client is needed on this side.
#
# Carrying all three removes two `run:` steps that reach an apt mirror on every build, which is the same class of outside dependency the action mirror removes.
FROM swift:6.3.2-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs \
      git \
      ca-certificates \
      curl \
      libjemalloc-dev \
      docker.io \
 && rm -rf /var/lib/apt/lists/*

RUN node --version && swift --version && docker --version
