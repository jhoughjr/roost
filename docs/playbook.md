# Roost — the platform playbook

**Roost** is the self-hosted platform this all runs on: one small ARM
box, Dokku, a Cloudflare tunnel, and a toolbelt of scripts. From nothing
→ a new app live at `<name>.jimmyhoughjr.net`, with data, crons,
accounts, and a status board. Written 2026-07-07 after building watts,
vault, and head2head this way.

**Starting from nothing?** Read [getting-started.md](#getting-started.md)
first — prerequisites, installs on host and workstation, tunnel creation,
first deploy. This playbook starts where that ends: the box runs Dokku,
the tunnel exists, and `dokku@192.168.0.103` accepts your key.
(statusgen's [SETUP.md](https://github.com/jhoughjr/statusgen/blob/main/SETUP.md)
covers the locally-managed-tunnel variant of the same bootstrap.)

---

## 1. New app, the six steps

**One command does all of this** (including the Cloudflare route):

    ~/repos/roost/bin/new-app.sh <name> [--static|--node|--swift]

It creates the Dokku app and domain, scaffolds a repo from a template
(static nginx / zero-dep Node / Hummingbird 2 Swift), makes the first
deploy, publishes the route via the Cloudflare API, and verifies LAN +
public. The manual steps below remain as the reference for what it does.

All toolbelt scripts read `~/.roostrc` (simple `KEY=VALUE` lines:
`ROOST_DOKKU_HOST`, `ROOST_DOMAIN`, `ROOST_METRIC_APP`,
`ROOST_STATUS_SITE`) so nothing is hardcoded to one person's host — copy
`roostrc.example` from the repo and fill in yours. The `roost` command
wraps everything: `roost new`, `roost route`, `roost fleet`,
`roost backup`, `roost status`, and `roost doctor` (diagnoses SSH, token,
tunnel, and tooling problems — run it first when anything misbehaves).

```sh
ssh dokku@192.168.0.103 apps:create <name>
ssh dokku@192.168.0.103 domains:set <name> <name>.jimmyhoughjr.net
mkdir <name>-site && cd <name>-site        # repo with a Dockerfile (see §2)
git init -b main && git add -A && git commit -m "init"
git remote add dokku dokku@192.168.0.103:<name>
git push dokku main
```

Then publish the route — **no dashboard needed**:

```sh
~/repos/roost/bin/publish-route.sh <name>
```

That creates the proxied CNAME and the tunnel ingress rule via the
Cloudflare API, idempotently. One-time setup: an API token from
dash.cloudflare.com/profile/api-tokens with **Zone / DNS / Edit** (zone
jimmyhoughjr.net) and **Account / Cloudflare Tunnel / Edit**, stored in
`~/.cf_api_token` (chmod 600). The dashboard route (Zero Trust → Tunnels
→ published application routes: subdomain, HTTP → `localhost:80`)
remains the manual fallback.

> The service URL is `localhost:80` for **every** app, always. Port 80 is
> Dokku's nginx, not any app; nginx routes by the `Host` header. Apps
> never conflict on it.

Verify before blaming DNS or the tunnel:

```sh
curl -H "Host: <name>.jimmyhoughjr.net" http://192.168.0.103/   # LAN truth
```

If LAN works and the browser says "can't find the server," it's DNS
negative-caching from before the route existed — flush or wait.

## 2. Dockerfiles that work here

**Static site** (watts, head2head, status):

```dockerfile
FROM nginx:alpine
COPY . /usr/share/nginx/html
```

**Node service** (vault's original): `FROM node:22-alpine`, copy source,
`CMD ["node", "server.js"]`, listen on 80. Zero-dep preferred.

**Swift service** (vault today): multi-stage, builds ON the pi (8-core /
16 GB handles it; first build ~8 min, cached ~5 min):

```dockerfile
FROM swift:6.1-noble AS build
# resolve deps in their own layer, then build --static-swift-stdlib
FROM ubuntu:noble   # + ca-certificates libcurl4 libxml2 for Foundation
```

**Swift gotcha: Mac-clean ≠ pi-clean.** The pi's Linux toolchain is
stricter (Sendable) and Linux Foundation has gaps (`URLResponse()`).
Always `swift build` locally first, but expect one on-device surprise.

## 3. Persistent data (survives deploys)

```sh
ssh dokku@192.168.0.103 storage:ensure-directory <name>
ssh dokku@192.168.0.103 storage:mount <name> /var/lib/dokku/data/storage/<name>:/data
ssh dokku@192.168.0.103 ps:restart <name>      # mounts apply on (re)start
```

Verify the mount took: `storage:report <name>` — the first attempt has
silently failed before.

## 3b. Databases — the nest (Postgres + jsonb)

Storage mounts hold flat files; when an app outgrows them (queries,
concurrent writers, real schemas) it gets a Postgres service. Document
data goes in `jsonb` columns — same shove-JSON-in ergonomics as a
document store, but with SQL, indexes (`GIN`), and first-class drivers
everywhere we work (PostgresNIO for Swift, `pg` for Node).

One-time plugin install — the only step that needs root on the pi
(the dokku@ channel can't do it):

```sh
sudo dokku plugin:install https://github.com/dokku/dokku-postgres.git --name postgres
```

Then per app, from any workstation:

```sh
roost db create <app>       # creates service <app>-db and links it
```

Linking injects `DATABASE_URL` into the app's env and restarts it. One
service per app — Postgres idles at a few tens of MB, and the isolation
is real (separate container, separate credentials). Everything else:

```sh
roost db list               # all services + status
roost db info <app>-db      # connection info, container, volume
roost db psql <app>-db      # interactive psql
roost db export <app>-db > f.dump    # pg_dump custom format
roost db import <app>-db < f.dump
```

`roost backup` exports every service nightly alongside the storage-mount
tars — a live data-dir tar is *not* crash-consistent, `postgres:export`
is. `roost doctor` reports whether the plugin is installed.

## 4. Scheduled jobs

`app.json` in the repo root; the command runs in a fresh container from
the app image, with the storage mounts and config env:

```json
{ "cron": [{ "command": "/usr/local/bin/refresh-thing", "schedule": "0 6 2 * *" }] }
```

Confirm with `cron:list <name>`. Pattern in the wild: watts' monthly EIA
rates refresh writes to the storage mount; the page reads the mounted
copy first and falls back to the deploy-baked file.

## 5. Secrets

- Runtime: `ssh dokku@… config:set <name> KEY=value` (restarts the app).
- Build-time (Dockerfile ARG): `docker-options:add <name> build "--build-arg KEY=value"`
  — e.g. the blog's `GITHUB_TOKEN`.
- Never in the repo. Local copies live in `~/.something` chmod 600
  (`~/.eia_api_key`) or come from `gh auth token`.

## 6. Accounts & user data — vault

vault.jimmyhoughjr.net gives every subdomain app sign-in (GitHub/Google/Apple)
and storage with zero app-side auth code:

1. Add the app's origin to vault's `ALLOWED_ORIGINS` (config:set).
2. Frontend calls with `credentials: "include"`:
   - `GET /api/config` → which providers are live (hide UI if none)
   - `GET /api/me` → session or 401
   - `GET/PUT /api/apps/<name>/data` → this user's private JSON blob
   - `GET/POST /api/apps/<name>/submissions` → shared public collection
     (signed-in append, public read, admin delete)
3. Sign-in links: `VAULT/auth/google?return=<your-url>` (and `apple`).

The session cookie is set on `.jimmyhoughjr.net`, so one login covers
every app.

## 7. Status boards — the blessed component

**statusgen** is Roost's supported status-board solution: a standalone
generator (GitHub jhoughjr/statusgen) that turns a `board.json` data
model into a rendered HTML board with client-side charts, cards, and
tables. Every Roost app can have a status board; status-site
(status.jimmyhoughjr.net) is the hub that hosts multiple boards and
their history.

**When to use:** Document a service's state (metrics, features, operations
blockers, team progress) as a living status board. The board.json schema
is simple — stats, banners, cards, tables, charts — and renders client-side
with no build step. Update flows are git-native: edit board.json, commit,
push dokku main.

### Standalone statusgen board (any Roost app)

Create a new repo that runs statusgen as its deployed service:

```sh
~/repos/roost/bin/new-app.sh myapp --board
```

This scaffolds a minimal statusgen board app with:
- `board.json` (starter template, edit to your metrics)
- `index.html` (loads the shared statusgen renderer from cdn)
- nginx Dockerfile (serve static assets)

Then:

```sh
cd ~/myapp-site
# edit board.json to add sections: stats, cards, tables, charts, etc.
# (schema: github.com/jhoughjr/statusgen/BOARD_SCHEMA.md)
git push dokku main
```

Deploy pushes at `https://myapp.jimmyhoughjr.net/`, and the board renders
live from `board.json`. No build dependencies; no versioning overhead.

### Multi-board hub (status-site example)

For multiple boards under one domain, clone status-site (which holds
the hub router + individual board repos as subdirs). Each subdir is a
`board.json` + `index.html` pair:

```sh
git clone dokku@192.168.0.103:status ~/status-site-local
cd ~/status-site-local
~/repos/statusgen/bin/new-board.sh . slug "Title" "Description"
# creates ./slug/{board.json,index.html}
```

### Updates and history

**For a single board:** edit `board.json` and `git push dokku main`.

**For status-site (multi-board with history tracking):** the hub runs
`push-status.sh` to regenerate the History board (every past push from
git history), commit, and deploy:

```sh
cd ~/status-site
# edit any board.json
~/status-site/push-status.sh "what changed"
```

The older `update-status.sh` (raw-HTML era) is legacy; don't use it for
new boards.

### Board schema

The `board.json` data model lives in statusgen's BOARD_SCHEMA.md.
Section kinds: `stats`, `banner`, `barchart`, `pie`, `table`, `cards`
(items use `q` + `pill: {text, tone}`), `split` (uses `columns`).
The `.tone` field drives color: `go`, `wip`, `srv`, `done`, `none`.

## 7b. Fleet observability

Three timescales, all part of Roost:

- **pulse** (`pulse.<domain>`) — realtime: a tiny Node app with read-only
  access to the Docker socket (`dokku docker-options:add pulse deploy
  "-v /var/run/docker.sock:/var/run/docker.sock:ro"`), serving `/api/stats`
  (per-container CPU%/memory from the Docker Engine API, host memory/load
  from /proc) behind a dashboard that polls every 5 s.
- **Fleet board** — snapshot: `roost fleet` (bin/fleet-board.py) collects
  over the dokku@ channel — per-app running state, HTTP 200 checks through
  nginx, process-RSS memory — and writes a statusgen board. Runs on every
  `push-status`. Note: per-app memory is the SUM of process RSS via
  `dokku enter <app> web ps -o rss=`; cgroup files inside `enter` sessions
  report the exec scope, not the app.
- **History board** — evolution: every status push, generated from git.

Alerting: `roost/bin/fleet-alert.py` runs every 15 minutes via launchd and
sends a desktop notification when an app stops serving 200 or disk/memory
cross thresholds — state-transition based, so it alerts once per incident,
not every 15 minutes.

**Off-box nodes** (the CI Mac mini, a workstation): `roost/bin/node-report.sh`
POSTs the Mac's load/memory/power-model to pulse's `/api/nodes` with the
shared key in `~/.roost_node_key` (must match `dokku config pulse NODE_KEY`);
`install-node-report.sh` wires it to launchd every 30 s. pulse serves the
last report per node (with age) in `/api/stats`, and
watts.jimmyhoughjr.net/roost/ renders the whole fleet's live power draw and
cost — nodes quiet for 2+ minutes count as asleep at ≈0 W.

## 7c. Backups

`roost backup` (bin/backup-roost.sh) tars each persistent storage mount
from inside a container (the only channel is dokku@) to
`~/Backups/roost/<name>-<date>.tgz`, and exports each Postgres service
(§3b) to `pg-<service>-<date>.dump`, keeps 14 days. Runs nightly at
04:15 via launchd. Every site repo also has a private GitHub remote —
the pi is never the only copy of anything.

## 8. Operational gotchas (all learned the hard way)

- **Trailing slashes in links.** nginx 301s `/blog` → `http://…/blog/`
  (it can't see the tunnel's TLS), and browsers refuse the downgrade.
  Link `/blog/` directly. Consider Cloudflare "Always Use HTTPS."
- **Long builds vs push timeouts.** A killed `git push` can leave a
  deploy lock (`apps:unlock <name>`) — but check first: the interrupted
  build may still be running and may even finish. Run long pushes in the
  background with output to a file.
- **Failed builds are safe.** Dokku retags the old image; production
  never flips to a broken build.
- **Rate limits during builds.** Anything fetching external APIs at build
  time (blog ↔ GitHub) needs a token and/or batching; the pi's IP shares
  one unauthenticated quota across all builds in an hour.
- **cloudflared DNS vs published apps.** Published-application routes in
  the dashboard create the DNS record and ingress in one step — no pi
  shell needed, which matters because only `dokku@` has key auth.

## 8b. Secrets inventory

| Secret | Lives at | Used by | Rotate by |
|---|---|---|---|
| Cloudflare API token | `~/.cf_api_token` (600) | `publish-route.sh` | dash.cloudflare.com → API Tokens |
| EIA API key | `~/.eia_api_key` (600) + `dokku config watts EIA_API_KEY` | rates refresh (local + pi cron) | eia.gov/opendata, then update both |
| GitHub build token | `dokku docker-options blog` build-arg | portfolio build-time API calls | github.com/settings/tokens, re-add docker-option |
| vault SESSION_SECRET | `dokku config vault` | session cookie HMAC | `config:set` new random hex (logs everyone out) |
| vault OAuth creds (pending) | `dokku config vault` | Apple/Google sign-in | provider consoles |
| pulse NODE_KEY | `~/.roost_node_key` (600, per node) + `dokku config pulse NODE_KEY` | node-report.sh → pulse `/api/nodes` | `config:set` new random hex, update each node's file |

Never commit any of these; `rates.json` and other derived data are public.

## 9. The roost (current fleet)

| App | What | Repo |
|---|---|---|
| blog | Astro site + portfolio (GitHub stats at build time) | `~/repos/personal-site` |
| status | statusgen hub + boards | `~/status-site` |
| watts | electric cost calculator, EIA rates cron | `~/watts-site` |
| vault | sign-in + user storage (Swift/HB2; Node revert in `~/repos/vault`) | `~/repos/vault-hb` |
| head2head | implementation-shootout reports + community proposals | `~/head2head-site` |
| docs | this playbook and friends, rendered from markdown | `~/docs-site` (content: `~/repos/docs`) |
| hello | living example created by `new-app.sh` | `~/hello-site` |
| pulse | realtime resource stats (Docker API + /proc) | `~/pulse-site` |
