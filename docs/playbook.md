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

vault.jimmyhoughjr.net gives every subdomain app sign-in (Apple/Google)
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

## 7. Status board

```sh
~/repos/statusgen/bin/new-board.sh ~/status-site <slug> "Title" "Hub description"
# edit ~/status-site/<slug>/board.json (schema: statusgen/BOARD_SCHEMA.md)
cd ~/status-site && git add -A && git commit && git push dokku main
```

Section kinds: `stats`, `banner`, `barchart`, `pie`, `table`, `cards`
(items use `q` + `pill: {text, tone}`), `split` (uses `columns`).

**Canonical update flow:** edit any `board.json`, then run
`~/status-site/push-status.sh "message"` — it regenerates the History
board (every past status push, from git), refreshes the Claude usage
ledger on the docs site, commits, and deploys. The older
`update-status.sh` (raw-HTML era) is legacy; don't use it for boards.

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
