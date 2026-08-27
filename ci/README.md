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

The container options can point every GitHub host at the loopback address inside a job. A run that stays green with them set needs nothing from GitHub, which is the only way to know rather than to hope. They ship commented out, because they are a proof tool and not a default.

**SwiftPM is the limit of that proof.** A Swift package resolves its dependencies by cloning them from github.com, so a package with dependencies fails with the block on, at `fatal: unable to access 'https://github.com/apple/...'`. A shell workflow passes, and so does a Swift package that declares no dependencies. That difference is easy to mistake for success, so read what the repository actually depends on before believing a green run.

The way to pass with the block on is a git rewrite rule, and it needs no change to any manifest:

```sh
git config --global url."https://forgejo.jimmyhoughjr.net/mirror/".insteadOf "https://github.com/"
```

That sends every clone to a local mirror while `Package.swift` and `Package.resolved` stay as they are. MWServer's `build.yml` already uses the same pattern pointed at GitHub with a PAT, so only the target changes. The cost is a mirror of every dependency, which is the same shape as the action mirror list.

Register a runner before the first start, and mint the token with `gitea actions generate-runner-token` inside the Forgejo container.

## Caching: use the mounted directory, not the cache action

Vault's gate went from 6 minutes 31 seconds cold to **1 minute 46 seconds warm**, and two consecutive warm runs measured 106 and 105 seconds, so the number holds. The 17 minutes an earlier run took was mostly a one-time pull of the 4.53 GB Swift image and not compilation.

`swift-pdf-builder` uses the same mechanism and runs in 60 seconds, against 91 before. It gains little from a warm build, because it declares no dependencies, and it still got faster by no longer uploading 31 MB to a cache that never served it back.

The mechanism is a directory, not `actions/cache`.

The runner mounts `/home/jimmy/forge-swift` into every job at `/swiftcache`, and a workflow opts in:

```yaml
run: swift test --no-parallel --scratch-path /swiftcache/<repo>
```

Compiled artifacts then survive between runs, with nothing in the path that can fail. Each workflow picks a subdirectory of its own so jobs do not collide.

**`actions/cache` does not work on this runner.** It saves an entry and never serves it back. The evidence is direct: one run reported `Cache saved with key: spm-static-v1`, the next reported `Cache not found for input keys: spm-static-v1`, with the key and the version both matching and the 32 MB entry sitting on disk. Getting saves to work at all took two settings that look interchangeable and are not, and it is recorded in the config file. Reads never came back, so every run stayed cold and paid a 31 MB upload on the end of it.

That action was never the right mechanism here. On GitHub it exists because the runner is ephemeral and the cache has to live somewhere else. This runner is a box that stays up, so persisting a directory is the native answer and the action is a workaround for not having one.

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
