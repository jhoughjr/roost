# The forge and its CI

The estate runs its own git forge and its own CI, so a build needs nothing from GitHub. This directory holds the parts that used to live only on the box.

`forgejo.jimmyhoughjr.net` is Forgejo, a dokku app on the pi. `s3.jimmyhoughjr.net` is vault, which holds Forgejo's artifacts and action logs. `opi-forge` is the `act_runner` that takes the jobs.

## Why a workflow runs here at all

Forgejo reads workflows from `.forgejo/workflows`, and when that directory is absent it falls back to `.github/workflows`. One copy of a workflow therefore runs on both forges, with no fork and no second file. That is what makes this a shadow of GitHub Actions rather than a migration away from it.

`DEFAULT_ACTIONS_URL` is `self`, so `uses: actions/checkout@v4` resolves inside this forge and never reaches github.com. Every action a workflow names must be mirrored here first, or the run fails at the clone.

## The job images

`act_runner` runs each job in a container. Two things force a custom image.

A `container:` job runs its JavaScript actions inside that container, and `actions/checkout` and `actions/cache` are both JavaScript. GitHub's runner mounts its own Node. `act_runner` does not, so an image without Node fails with `exec: "node": executable file not found in $PATH` before the job's real work starts.

A `run:` step reaches wherever it likes, and no forge setting governs that. A step that downloads a tool from a GitHub release is a GitHub dependency that survives every mirror. An image that already carries the tool removes it, and the workflow needs no edit when the step already checks whether the tool is present.

| Image | Base | Carries | For |
|---|---|---|---|
| `roost-ci:arm64` | `node:22-bookworm` | shellcheck, git, curl, python3 | shell and script workflows |
| `roost-swift-ci:6.1-noble` | `swift:6.1-noble` | node, git, curl | vault |
| `roost-swift-ci:6.3` | `swift:6.3` | node, git, curl | swift-pdf-builder |

Build one with:

```sh
docker build -t roost-ci:arm64 ci/images/roost-ci
```

A Swift image tag matches the tag the project's own Dockerfile builds with, so a gate tests the toolchain that deploys.

## The runner

`ci/act-runner/config.yaml` maps every label to an image and carries the container options. Install it into the runner's data volume and start the daemon against it:

```sh
docker run -d --name act_runner --restart unless-stopped -w /data \
  --group-add "$(getent group docker | cut -d: -f3)" \
  -v act_runner_data:/data -v /var/run/docker.sock:/var/run/docker.sock \
  code.forgejo.org/forgejo/runner:9 forgejo-runner daemon --config /data/config.yaml
```

The options point every GitHub host at the loopback address inside a job. A run that stays green with them set needs nothing from GitHub, which is the only way to know rather than to hope.

Register a runner before the first start, and mint the token with `gitea actions generate-runner-token` inside the Forgejo container.

## Two rules the box taught us

**A Forgejo config change needs a full stop.** dokku starts the new container while the old one still holds the persistent volume, and Forgejo's queue takes an exclusive LevelDB lock on it. Two instances cannot share one data directory, so a rolling deploy deadlocks with `unable to lock level db`. Use `config:set --no-restart`, then `ps:stop`, then `ps:start`.

**A git push over HTTP needs a bigger body.** dokku's nginx default is far below a real repository, and a push fails with `413`. Set it once:

```sh
dokku nginx:set forgejo client-max-body-size 1024m
```

## What a repository costs to move

A workflow with no `container:` costs nothing. It runs unchanged.

A workflow with a `container:` costs one line for each job that has one, because the image has to carry Node.

A workflow that downloads a tool in a `run:` step costs an image that carries the tool, and nothing in the workflow when the step already checks for it.
