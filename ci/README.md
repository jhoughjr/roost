# The forge and its CI

The estate runs its own git forge and its own CI, so a build needs nothing from GitHub. This directory holds the parts that used to live only on the box.

`forgejo.jimmyhoughjr.net` is Forgejo, a dokku app on the pi. `s3.jimmyhoughjr.net` is vault, which holds Forgejo's artifacts and action logs. `opi-forge` is the `act_runner` that takes the jobs.

## Why a workflow runs here at all

Forgejo reads workflows from `.forgejo/workflows`, and when that directory is absent it falls back to `.github/workflows`. One copy of a workflow therefore runs on both forges, with no fork and no second file. That is what makes this a shadow of GitHub Actions rather than a migration away from it.

`DEFAULT_ACTIONS_URL` is `self`, so `uses: actions/checkout@v4` resolves inside this forge and never reaches github.com. Every action a workflow names must be mirrored here first, or the run fails at the clone.

**An absent action reads as a credential problem.** Forgejo answers `authentication required: Unauthorized` for a repository that simply does not exist, so the first thing to check is whether the action is mirrored and not whether a token is wrong. Mirrored so far:

```
actions/checkout   actions/cache   actions/setup-node   actions/upload-artifact
docker/setup-buildx-action   docker/login-action   docker/build-push-action
```

Mirror one with a migration, and give the organization a home first:

```sh
curl -X POST -H "Authorization: token $TOKEN" -H "Content-Type: application/json" \
  -d '{"clone_addr":"https://github.com/actions/setup-node.git","repo_name":"setup-node","repo_owner":"actions","mirror":false,"service":"git","private":false}' \
  https://forgejo.jimmyhoughjr.net/api/v1/repos/migrate
```

A large action can answer `504` and still finish, because the gateway gives up before the migration does. Check the listing before retrying.

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

## The macOS runner, which has to be built

**Forgejo publishes no macOS runner binary.** Its releases carry linux amd64 and linux arm64 and nothing else, so the mini's runner is built from source:

```sh
brew install go
git clone --depth 1 --branch v9.1.1 https://code.forgejo.org/forgejo/runner.git src
cd src && go build -o ../forgejo-runner ./
```

Register it in host mode, because the mini runs no docker and the work wants the host anyway. Phoenix signs its build with a certificate in the login keychain, and a container has neither the keychain nor the tools:

```sh
./forgejo-runner register --no-interactive \
  --instance https://forgejo.jimmyhoughjr.net --token <token> --name mini-forge \
  --labels "self-hosted:host,macos:host,arm64:host,mini:host"
./forgejo-runner daemon --config config.yaml
```

`ci/act-runner/config-macos.yaml` is that config. The `:host` suffix is what makes a label run on the host instead of in an image.

The label set is what routes work. The opi declares `self-hosted` and `arm64` too, so a job that names only those can land on either box, which `roost/check.yml` intends. `macos` and `mini` are what keep Phoenix's jobs on the mini.

A macOS runner needs nothing else installed. The mini already had node, npm and git, and Phoenix's workflow reads no secrets at all, so it ported with no edits and nothing to reissue.

**Start the daemon with Homebrew on the PATH.** A host-mode job inherits the daemon's environment, and a daemon started from a non-interactive shell or from launchd gets a minimal PATH with no `/opt/homebrew/bin`. The job then fails with `Cannot find: node in PATH` and `npm: command not found`, which reads like a missing install and is not one. This is the same trap `bin/ci-live-report.sh` already documents for launchd.

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  ./forgejo-runner daemon --config config.yaml
```

## Never route a service to a service through the tunnel

Forgejo and vault both run on the pi, and `s3.jimmyhoughjr.net` resolves publicly, so every artifact chunk left the box, crossed Cloudflare, and came back to the same box. That cost more than latency.

**Cloudflare strips response headers an S3 client needs.** Vault answers a `PUT` with `Last-Modified`, minio-go parses that header on every upload response, and it refuses an absent one. Through the tunnel the header did not arrive, and Forgejo failed every artifact upload with `Last-Modified time format is invalid, failed with unable to parse`, which reads like a malformed value and is a missing one. Probing vault directly over the local path returns the header, so the origin was always correct.

**Cloudflare challenges scripted clients.** A signed request from a script gets `403` with `error code: 1010`, which is bot protection and not an authorization failure. Diagnosing an S3 problem from outside the box will mislead you.

The fix is to resolve the name locally and skip the tunnel:

```sh
dokku docker-options:add forgejo deploy "--add-host s3.jimmyhoughjr.net:10.0.0.1"
dokku config:set forgejo FORGEJO__storage__MINIO_USE_SSL=false
```

The hostname stays the same, so the signature still covers the name the client dialed, and nothing else in the configuration changes. Apply the same shape to any service on this box that talks to another one by public name.

## Launch a long deploy detached on the box

A dokku build runs for as long as the client holds the connection, and a Swift build runs for ten minutes or more. If the machine that started it sleeps, the pipe breaks and the build dies part way, leaving a deploy lock and a half-finished image. Three deploys died that way in one evening, and each looked like a failure on the box when the fault was a laptop lid.

Start it on the host instead, so the build belongs to the host:

```sh
ssh jimmy@opi "nohup sh -c 'ssh dokku@opi ps:rebuild vault > /tmp/rebuild.log 2>&1' >/dev/null 2>&1 &"
```

The host needs to trust its own dokku endpoint once, or the inner ssh fails on host-key verification:

```sh
ssh-keyscan -H 192.168.0.103 >> ~/.ssh/known_hosts
```

This is the same shape as the ssh-to-localhost hop in `bin/status.sh`, for a different reason. That one routes around macOS Local Network Privacy, this one routes around the client going to sleep, and both end with the work owned by the machine that has to finish it.

## Probe the artifact store before you trust it

`probes/s3-conformance` makes the calls Forgejo makes against the S3 endpoint, with the client version
Forgejo carries, and names the first call that fails.

Use it whenever the artifact store changes, and before a deploy of it:

```
docker run --rm -e S3_ENDPOINT=... -e S3_ACCESS=... -e S3_SECRET=... \
  -v "$PWD/ci/probes/s3-conformance:/src" -w /src golang:1.24 \
  sh -c 'go mod tidy >/dev/null 2>&1; go run main.go'
```

It exists because a storage fault is very hard to read from either end. Forgejo reports one as
`Artifact service responded with 500` against whichever step was running, and that is usually the
merge, so the message names the merge when the fault is elsewhere. The server sees nothing, because
it believes it answered the request. One absent header on a read cost a night of reading logs, and
this probe named it on the first run.

Two things it checks that a smaller probe would miss. A read of the body is separate from a head,
because a streamed read builds its own headers and can drop one that the head still carries. And a
write of unknown length is separate from a write of known length, because Forgejo writes the merged
artifact without a length, which streams and signs the body in chunks.

Hold the client version in step with the forge. minio-go v7.0.90 accepts an absent `Last-Modified` on
a read and v7.0.98 refuses it, so an older client passes an endpoint that Forgejo cannot use. `mc`
carries the older one.

## Two rules the box taught us

**A Forgejo config change needs a full stop.** dokku starts the new container while the old one still holds the persistent volume, and Forgejo's queue takes an exclusive LevelDB lock on it. Two instances cannot share one data directory, so a rolling deploy deadlocks with `unable to lock level db`. Use `config:set --no-restart`, then `ps:stop`, then `ps:start`.

**A git push over HTTP needs a bigger body.** dokku's nginx default is far below a real repository, and a push fails with `413`. Set it once:

```sh
dokku nginx:set forgejo client-max-body-size 1024m
```

## Where a forge-specific workflow belongs

In the repository, beside the GitHub one, and never as a patch applied to the forge's copy.

Forgejo reads `.forgejo/workflows` when that directory exists, and falls back to `.github/workflows` when it does not. GitHub ignores `.forgejo` entirely. So a repository can carry both, they stay independent, and the two copies of the repository stay byte-identical.

```
.github/workflows/ci.yml     what GitHub runs
.forgejo/workflows/ci.yml    what the forge runs
```

A workflow that needs no adaptation keeps one file and the fallback finds it, which is the case for `roost` and `hatchery`. A workflow that needs a different image, an older action, or a different scratch path gets a second file, and both are reviewable in the same pull request.

The alternative is to edit the forge's copy directly, and it is a trap. The adaptation then lives only on the forge, no one reading either repository can see it, and the next person to push overwrites it without knowing.

Note the fallback is all-or-nothing. Once `.forgejo/workflows` exists the forge stops reading `.github/workflows` for that repository, so the new file is a whole workflow and never a patch.

## What a repository costs to move

A workflow with no `container:` costs nothing. It runs unchanged.

A workflow with a `container:` costs one line for each job that has one, because the image has to carry Node.

A workflow that downloads a tool in a `run:` step costs an image that carries the tool, and nothing in the workflow when the step already checks for it.

A workflow that uploads an artifact costs a version pin. `actions/upload-artifact@v4` and later speak an artifact API the forge does not serve, and a run fails with `GHESNotSupportedError: upload-artifact@v4+ ... not currently supported on GHES`. `v3` speaks the older API and works. This is the same shape as the cache: a newer action talking to a service the forge does not implement, and the fix is the last version that speaks the old protocol.

## The shadow shares some ground with GitHub

The forge shadows GitHub rather than replacing it, so the same repository runs on both. Two directories on the mini are written by both, and that is worth knowing before reading a board.

`~/builds/phoenix` holds the published build zips and an `index.json`, and `statusgen`'s `builds.py` reads that manifest. A forge run publishes there exactly as a GitHub run does, so **the board's Builds section already shows forge-produced builds with no mark saying so**. That is what statusgen#65 is for.

`~/mirrors/phoenix-electron.git` is the mirror `sync-mirror.sh` maintains, and both systems run that script. Its `origin` points at GitHub because a GitHub run seeded it, so a forge run following it reaches github.com and fails on credentials. Re-pointing it would change what the GitHub runs do, so a forge run gets its own `MIRROR_DIR` instead and GitHub's mirror is left alone.

The general rule: anything a workflow writes outside its workspace is shared with the other forge, and the shadow will land in it.
