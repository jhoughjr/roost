# Roost — the platform playbook

**Roost** is the self-hosted platform this all runs on: one small ARM
box, Dokku, a Cloudflare tunnel, and a toolbelt of scripts. From nothing
→ a new app live at `<name>.jimmyhoughjr.net`, with data, crons,
accounts, and a status board. Written 2026-07-07 after building watts,
vault, and head2head this way.

**Starting from nothing?** Read [getting-started.md](getting-started.md)
first — prerequisites, installs on host and workstation, tunnel creation,
first deploy. This playbook starts where that ends: the box runs Dokku,
the tunnel exists, and `dokku@192.168.0.103` accepts your key.
(statusgen's [SETUP.md](https://github.com/jhoughjr/statusgen/blob/main/SETUP.md)
covers the locally-managed-tunnel variant of the same bootstrap.)

---

## 1. New app, the six steps

**One command does all of this** (including the Cloudflare route):

    ~/repos/roost/bin/new-app.sh <name> [--static|--node|--swift|--board]

It creates the Dokku app and domain, scaffolds a repo from a template
(static nginx / zero-dep Node / Hummingbird 2 Swift), makes the first
deploy, publishes the route via the Cloudflare API, and verifies LAN +
public. The manual steps below remain as the reference for what it does.

All toolbelt scripts read `~/.roostrc` (simple `KEY=VALUE` lines, for example `ROOST_DOKKU_HOST`, `ROOST_DOMAIN`, and `ROOST_STATUS_SITE`), so nothing is hardcoded to one person's host.
Copy `roostrc.example` from the repo and fill in yours.
The `roost` command wraps everything.
The header of `bin/roost` is the full list:

- lifecycle: `roost new`, `roost route`, `roost lan-cert`
- status site: `roost status`, `roost stats`, `roost fleet`, `roost kick`, `roost rollout`
- day-2 Dokku ops: `roost apps`, `roost ps`, `roost logs`, `roost restart`, and `roost config`, which reads only. `hatchery config set` writes config
- power: `roost tapo`, `roost ha-scoop`
- forge: `roost forge-log`
- backups and secrets: `roost backup`, `roost secrets`
- housekeeping: `roost prune`
- `roost doctor` (diagnoses ssh, the Cloudflare token and zone, and tooling problems. Run it first when anything misbehaves) and `roost ui` (full-screen terminal: console, monitor, config, docs, and backups tabs)

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

- Runtime: `hatchery config set <stack> <name> KEY=value`, which writes the declaration and applies it.
  A direct `ssh dokku@… config:set` also restarts the app, but the declaration does not see it, and `hatchery config audit` reports it as drift.
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

### Enabling a sign-in provider

Each provider is two config vars on vault; it stays hidden from
`/api/config` until both are set, so this is safe to do any time.

- **GitHub**: github.com → Settings → Developer settings → OAuth Apps →
  New OAuth App. Homepage `https://vault.jimmyhoughjr.net`, callback
  `https://vault.jimmyhoughjr.net/auth/github/callback`. Then
  `ssh dokku@192.168.0.103 config:set vault GITHUB_CLIENT_ID=… GITHUB_CLIENT_SECRET=…`
- **Google**: console.cloud.google.com → OAuth client, callback
  `…/auth/google/callback` → `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
- **Apple**: developer.apple.com → Services ID with Sign in with Apple,
  return URL `…/auth/apple/callback` → `APPLE_TEAM_ID`, `APPLE_SERVICE_ID`,
  `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` (.p8 contents).

### Gating a whole static site (admin-only)

Any nginx-served subdomain can be made admin-only with zero app code:
the vault cookie is domain-wide, so nginx asks vault on every request
via `auth_request`. status-site is the worked example (status-site#1):

```nginx
location = /_vault_auth {
  internal;
  proxy_pass https://vault.jimmyhoughjr.net/api/admin/stats;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
  proxy_set_header Host vault.jimmyhoughjr.net;
  proxy_ssl_server_name on;
  proxy_http_version 1.1;
  proxy_connect_timeout 5s;
  proxy_read_timeout 10s;
}
location = /signin.html { try_files $uri =404; }   # the one ungated path
location / {
  auth_request /_vault_auth;
  error_page 401 403 = @signin;
  try_files $uri $uri/ =404;
}
location @signin { return 302 /signin.html?to=$request_uri; }
```

- Gate on **admin**, not merely signed-in: provider sign-up is open to
  anyone, so probe `/api/admin/stats` (200 only for an `ADMIN_EMAILS`
  session, 403 otherwise) rather than `/api/me`.
- Or gate on a **GitHub org**: probe `/api/authz/org/<org>` — 200 for a
  member of that org (captured at GitHub sign-in) *or* an admin account
  (the lockout escape hatch). status-site uses this today
  (`austin-macworks`). Members must sign in via GitHub; a renderer
  session chip (statusgen, via the site's `_assets/site.json`) shows
  who's signed in, since a passing session sails through invisibly.
- `/signin.html` must be self-contained (inline CSS/JS): it reads
  `/api/config` and links `VAULT/auth/<provider>?return=<url>` for each
  live provider; if `/api/me` is already 200 it says "signed in as X —
  not authorized" with a logout link. Copy status-site's.
- Fails closed when vault is down. Git pushes (dokku deploys, board
  updates) are unaffected — only HTTP viewing is gated.
- Cost: one subrequest to vault per asset request. Fine for small sites;
  add auth-response caching keyed on the session cookie if it ever hurts.

### Kiosk token: unattended displays through the gate

TVs and signage can't hold OAuth sessions (passkeys don't run in embedded
web views at all). Vault accepts a static kiosk token on the org probe,
scoped to exactly one org gate — nothing else in vault answers to it, and
the env stores only the hash. Requires vault-hb ≥ the `kiosk-token` change.

1. Mint a token and hash it; give vault the **hash**:

   ```sh
   TOKEN=$(openssl rand -hex 24); echo "token: $TOKEN"
   HASH=$(printf %s "$TOKEN" | shasum -a 256 | cut -d' ' -f1)
   ssh dokku@192.168.0.103 config:set vault KIOSK_TOKENS=$HASH:austin-macworks
   ```

2. Teach the gated site's nginx to forward it — query param on the first
   hit, cookie afterwards (the gate runs per asset; params don't ride
   along to CSS/JS/`board.json`):

   ```nginx
   # server level, above the locations
   set $kiosk_token $cookie_kiosk;
   set $kiosk_setcookie "";
   if ($arg_kiosk != "") {
     set $kiosk_token $arg_kiosk;
     set $kiosk_setcookie "kiosk=$arg_kiosk; Path=/; Max-Age=31536000; Secure; HttpOnly";
   }

   # inside location = /_vault_auth, next to the other proxy_set_header lines
   proxy_set_header X-Kiosk-Token $kiosk_token;

   # inside location /
   add_header Set-Cookie $kiosk_setcookie;   # empty value → header omitted
   ```

3. Point the display at `https://status.jimmyhoughjr.net/?kiosk=$TOKEN`
   (e.g. an SVS page item). The renderer session chip stays empty — right
   for a kiosk.

Notes: rotate by replacing the env value (old token dies instantly);
the gate still fails closed when vault is down; the plaintext token
appears in the site's nginx access log on first hit — org-scoped
read-only access keeps that tolerable, rotation keeps it cheap.

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

This scaffolds a complete statusgen board app with:
- `board.json` (starter template, edit to your metrics)
- `index.html` (board shell, loads the renderer from `_assets/`)
- `_assets/board.{js,css}` (renderer copied from your statusgen clone via
  `sync-renderer.sh`; re-run it and push to pick up renderer updates)
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

**For status-site (multi-board with history tracking):** `roost status`
is the one orchestration point — it runs the collectors (fleet, stats,
history), syncs the renderer, validates every board against the schema,
then commits and deploys:

```sh
# edit any board.json in ~/status-site
roost status "what changed"
```

With no message, `roost status` composes the narrative itself from the
week's merged PRs (`bin/gen-narrative.py`, needs `ROOST_STATS_GH_REPO`
in `~/.roostrc`). And when a site or board change lands on GitHub and
you don't want to wait out the hour: `roost kick` fires the status
runner's LaunchAgent on the CI Mac immediately (it never kills a deploy
already in flight).

The site is pure data now (board.json + shells + manifest); the driver
logic lives in `roost/bin/status.sh` and the collectors in statusgen. See
statusgen's INTERFACES.md for the full contract. (The old in-site
`push-status.sh` / `update-status.sh` scripts are gone.)

**Two writers must run the same code.** Where more than one machine runs
`roost status` (a workstation plus the scheduled runner), a version skew
between them regenerates boards differently and the two fight. After
merging anything in roost or statusgen:

```sh
roost rollout          # ff-only pull of roost + statusgen here and on every ROOST_WRITERS machine
roost rollout --kick   # ...and fire the runner's deploy right after
```

`ROOST_WRITERS` is a space-separated list of ssh targets (default: the
`ROOST_STATUS_RUNNER` machine; set it empty on a single-machine setup).
Each machine resolves its own clone paths from its own `~/.roostrc`, so
the layouts don't have to match. A dirty or diverged clone is reported
and left alone rather than force-updated, and the command exits non-zero
so an incomplete rollout can't be mistaken for a finished one.

`roost status` also fast-forwards its own clones at the start of every
run, so the fleet converges on its own eventually — `roost rollout` is
how you make that happen *now*, before the next scheduled push.

**The site clone pulls origin, not dokku, before regenerating.** Dokku is
a deploy sink (see below) — pulling it as if it were a source let a
scheduled refresh regenerate from a checkout that never saw another
writer's hand lede, then force-push over it (bit the mini twice:
2026-07-26, 2026-07-29). `roost status` now pulls `origin/main` first
(falling back to dokku only if GitHub is unreachable), and refuses to
publish — exits non-zero rather than force-pushing — if it can't confirm
its commit sits on top of the mirror right before the final push.

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
  from /proc) behind a dashboard that polls every 5 s. Also serves `/map`,
  a live system-map view of the same data (see below).
- **Fleet board** — snapshot: `roost fleet` (bin/fleet-board.py) collects
  over the dokku@ channel — per-app running state, HTTP 200 checks through
  nginx, process-RSS memory — and writes a statusgen board. Runs on every
  `roost status`. Note: per-app memory is the SUM of process RSS via
  `dokku enter <app> web ps -o rss=`; cgroup files inside `enter` sessions
  report the exec scope, not the app.
- **History board** — evolution: every status push, generated from git.

Alerting: pulse does it, every 15 minutes, from the readings roost already posts it. An app that stops serving, an app answering badly at its declared health path, disk or memory crossing 85%, and a mains dip on a plug. It is state-transition based, so it says a thing once per incident rather than every 15 minutes, and it writes the same words to its events table because the phone forgets in about twelve hours. Set `NTFY_TOPIC` on the pulse app.

**Off-box nodes** (the CI Mac mini, a workstation, opi): `roost/bin/node-report.sh`
POSTs the box's load/memory/watts to pulse's `/api/nodes` with the
shared `NODE_KEY` (must match `dokku config pulse NODE_KEY`). The host reads it from vault first and from `~/.roost_node_key` after it, as the README section "Secrets from vault" describes.
`install-node-report.sh` wires it to launchd every 30 s — or, on Linux, to a
systemd **user** timer (no sudo, which is what opi has). pulse serves the
last report per node (with age) in `/api/stats`, and
watts.jimmyhoughjr.net/roost/ renders the whole fleet's live power draw and
cost — nodes quiet for 2+ minutes count as asleep at ≈0 W.

**Power: measured vs estimated (added 2026-07-10).** If `macmon` is on the
node (`brew install macmon`, Apple silicon only), node-report.sh samples
`macmon pipe -s 1` → `sys_power` — the SMC system-total watts, the same
number Stats.app shows, no sudo needed — and sends it as `wattsW`. pulse
relays it and the /roost/ page shows it with a green **measured** badge.
Without macmon the field is omitted and the page falls back to the model
`idleW + (maxW − idleW) × load1/cores`, badged **est**. The gap is real:
the mini under CI load measured 17.8 W where the model said 40 W.

On Linux there is no sudoless system-power sensor (the Pi-class PMIC exposes none).
A Linux node reports measured watts only from a Tapo smart plug on its cord: `tapo-poll.py` writes the plug reading to `~/.roost-tapo.json`, and node-report sends it as `wattsW` with `wattsSrc: "plug"`.
A Linux node with no plug is **est**. Set `ROOST_NODE_IDLE_W`/`MAX_W` in `~/.roostrc` to make the estimate honest (opi: 4 / 15, the ceiling its 5 V supply can actually deliver).

Also reported: `power` (ac|battery) + `batteryPct` from `pmset -g batt`, or on
Linux from `/sys/class/power_supply` — a box with no battery reads `ac`. A node
on battery bills 0 toward grid cost on /roost/ (its watts were already paid at
the wall) and shows a 🔋 badge; /map shows ⚡AC / 🔋 per node.

**Temperatures (added 2026-09-05).** Linux nodes report every temperature sysfs exposes, as `temps`, and pulse draws them as a Temperatures card with the drive first. Each entry carries a name, the reading in C, and its own warning and critical limit, so the card colours a number without holding a table of sensors. Names come from the sensor itself: an NVMe entry is named for its controller (`nvme0`), and a thermal zone is named from its `type` file (`soc`, `bigcore0`, `gpu`, `npu`), never from its index. A zone with no readable type reports as `zoneN`, which says that only the index is known.

A zone takes its limits from its own passive trip points, the temperatures at which the kernel throttles it. A zone that declares none takes the lowest and the highest passive trip found on the box, because the zones of one SoC share a die and a throttle policy. On the opi (RK3588) that is 75 C and 85 C, declared by `soc-thermal` alone; its critical trip of 115 C is a shutdown, so it is too late to be a warning. A drive takes 70 C and 80 C. 70 C is the top of the operating range a consumer NVMe drive specifies, and where it starts to throttle itself. The drive in the opi declares 84.85 C for both its composite warning limit and its composite critical limit, so 80 C still leaves room to act first.

Linux nodes also report `fanPwm` where the board drives a fan, which is the duty sysfs commands from 0 to 255. There is no tachometer on the opi, so this is what the board asks the fan to do, not a measured speed. The card warns when the fan reads 0 while a sensor is at or over its warning limit.

Why it exists: on 2026-09-04 the opi's NVMe drive overheated and left the PCIe bus after 9 days of uptime. Docker keeps its `data-root` on that drive, so docker went down and took vault and forgejo with it. The drive had been moved above a router, with no heatsink and no airflow. The kernel already carries `nvme_core.default_ps_max_latency_us=0` and `pcie_aspm=off`, so the APST bug is ruled out and the cause was heat.

macOS nodes with macmon send the CPU and GPU temperatures from the same macmon sample as the watts, with a warning limit of 95 C and a critical limit of 105 C. A Mac with a battery also sends the battery temperature from `ioreg`, with or without macmon, with a warning limit of 40 C and a critical limit of 45 C. A desktop Mac without macmon sends no temperatures.

Putting a new box on the meter:

1. give the box its `NODE_KEY`: set the three `ROOST_VAULT_*` keys in its `~/.roostrc`, or copy `~/.roost_node_key` from the workstation (chmod 600). `roost secrets` says which one answers
2. run `roost/bin/install-macmon.sh` (or `install-macmon.sh user@host` from another Mac). Skip it on Intel and on Linux, where the node reports estimates or plug watts
3. set `ROOST_NODE_NAME=<name>` in `~/.roostrc` — the default name comes
   from ComputerName (Linux: hostname) and the sanitizer turns curly
   apostrophes into dashes ("Jimmy's MacBook Air" → `jimmy---s-macbook-air`)
4. run `roost/bin/install-node-report.sh`
5. Linux only: `sudo loginctl enable-linger <user>`, or the user timer dies at
   logout and never comes back after a reboot (the installer warns if it's off)

pulse keeps nodes in memory only — after a rename or a stale ghost,
`roost restart pulse` wipes the list and live agents repopulate it within
30 s.

**Live CI runs.** `roost/bin/ci-live-report.sh` runs on the CI Mac and
POSTs in-progress/queued GitHub Actions runs (via `gh`) to the ci-live
app's `/api/runs`, which the boards' live console renders. Config:
`ROOST_CI_LIVE_REPOS` (`owner/repo:project:intervalSec`, comma-separated)
and `ROOST_CI_LIVE_ENDPOINT` in `~/.roostrc`. The shared POST key is `CI_KEY`, read from vault first and from `~/.roost_ci_key` after it, and it must match `dokku config ci-live CI_KEY`.
`install-ci-live-report.sh` wires it to launchd
(`net.jimmyhoughjr.roost-ci-live`, every 20 s) with Homebrew on PATH so
the poller finds `gh`/`jq`.

The host Pi itself (Orange Pi 5 Plus, RK3588): pulse already self-reports its load/memory from /proc, but the SoC exposes **no power sensor**.
Tools like rktop read utilization/frequencies/temps from sysfs, not watts (verified against its README 2026-07-10).
The opi's watts come from the Tapo plug `ROOST_TAPO_FLEET` names (default `opi`), through `tapo-poll.py` and its cache.

**Tapo plugs and Home Assistant.** `roost tapo` (`bin/tapo-poll.py`) reads every plug in `ROOST_TAPO_DEVICES` over the local KLAP API, or through Home Assistant for a plug named in `ROOST_HA_TAPO`.
It posts the whole set to pulse `/api/tapo`, and `install-tapo-poll.sh` runs it as one long-running `--watch` process.
`roost ha-scoop` (`bin/ha-scoop.py`) copies the hourly power statistics of Home Assistant into pulse, tagged `src:"ha"`, and fills only hours with no sampled data.
`install-ha-scoop.sh` runs it at 10 minutes past each hour.
`bin/volts.py [hours]` prints the mains voltage and load from pulse `/api/history` as sparklines, to match a dip to its cause.

**Watchdogs on the opi.** Each one is a systemd user timer, so linger must be on.

- `dokku-reconcile.sh`, every 10 minutes: starts deployed apps that are down, names apps with no image, rebuilds the vhosts, and asks each declared app its health path. It posts to pulse `/api/answers` and `/api/events`. When it finds a thing it cannot fix, `dokku-reconcile-alert.service` sends the line through `mesh-alert.sh` over LoRa. [mesh-alert-setup.md](mesh-alert-setup.md) has the radio setup.
- `backup-watch.sh`, at 08:00: sends an ntfy alert when the nightly backup wrote no finish stamp.
- `runner-watchdog.sh`, every 15 minutes: watches the self-hosted GitHub runners through the GitHub API, and sends an ntfy alert for a runner offline too long or a run queued too long.

**Watts history (added 2026-07-10).** pulse samples once a minute to its
persistent mount (`dokku storage:mount pulse
/var/lib/dokku/data/storage/pulse:/data`, dir `/data/history`, 90-day
retention) — host load plus each awake node's measured watts or estimate
inputs. `/api/history?hours=N` returns the samples (decimated to ≤2000
points), and the /roost/ page renders them as a stacked-area History card
(24 h / 7 d / 30 d) where the top edge is the whole fleet. No mount → the
sampler no-ops and the endpoint returns empty.

**System map (added 2026-07-10).** `pulse.<domain>/map` renders `/api/stats`
as a topology instead of tables: Mac node cards (measured macmon watts vs
modeled estimate, asleep past 90 s of silence) with animated wires into the
pi card, dokku apps as clickable up/down chips, and an ingress card.
Linked from the dashboard header subtitle and footer; refreshes every 5 s.
Gotcha: pulse's Dockerfile COPYs files by name, so a new file 500s in
production until it's added to the COPY line.

**Ingress/tunnel state (added 2026-07-11).** cloudflared runs on the pi
host, invisible from pulse's container — so pulse probes its own public URL
(`/health`) out through the Cloudflare edge every 30 s, exercising the full
chain cloudflare → tunnel → nginx → app, and reports up/down + round-trip
ms in `/api/stats` as `tunnel.probe` (public URL overridable via `dokku
config pulse PUBLIC_URL`). Optional richer layer via the Cloudflare API:

```sh
# token: dash.cloudflare.com → API Tokens → Custom → permission
#   Account → Cloudflare Tunnel → Read   (Account scope, not Zone)
# account id: domain Overview page, right column
ssh dokku@<pi> config:set pulse CF_API_TOKEN=<token> CF_ACCOUNT_ID=<id>
```

That adds `tunnel.cf` — the tunnel's official status, connector count, and
edge colos (normal shape: 4 connections across 2 nearby colos) — polled at
boot and every 5 min. Both layers render on the /map ingress card; without
the token the card just shows the probe.

**WAN / Starlink visibility (added 2026-07-12).** The Docker image bakes in
grpcurl to poll the Starlink dish gRPC API at 192.168.100.1:9200 (overridable
via DISH_ADDR; answers from the LAN even in bridge mode). server.js polls
`get_status` every 30 s and `dish_get_obstruction_map` every 10 min, serving
the map at `/api/wan/obstruction` in `/api/stats`. The AX440 router has no
SNMP or local API (Tether port 20002 closed), so pulse TCP-probes its web UI
(192.168.0.1:80, overridable via ROUTER_ADDR) every 30 s for up/RTT. The
gigabit switch is unmanaged and drawn statically. `/map` renders the ingress
chain as dish → router → switch with click-to-expand cards and a canvas
obstruction sky-map, refreshing every 5 s.

## 7c. Backups

### What runs

`bin/opi-backup.sh` runs on the opi from cron at 03:30 and writes two restic
repositories over SFTP to the drive on the mini. `opi-critical` holds the
PostgreSQL dumps, `/var/lib/dokku`, `/etc`, the Home Assistant config, the
docker volumes, and the host metadata. `opi-bulk` holds the home directory.
Together they store about 3 GB, because the docker images and the build caches
are most of the disk and they rebuild. `restic check` runs on every pass, and
`docs/opi-backup-restore.md` carries the restore order.

### Seeing whether it works

`roost backup` reports that service from any machine. It reads the schedule and
the last run from the service host, and the repositories from the storage host,
and it reports an unreachable host rather than staying quiet about it.
`roost backup --run` starts a backup, and `roost backup --check` verifies every
repository. Tab 5 of `roost ui` draws the same reading and binds those two
actions to `b` and `c`.

`bin/backup-report.sh` pushes the same reading to pulse `/api/backups` every
hour, and the dashboard draws a Backups card from it: one chip for the service
and one per repository, green while a nightly run is on time, amber for one
missed night, red for a missing schedule, an unfinished run, an unmounted drive,
or an unreachable host. A reading that has itself gone stale gets its own amber
chip, because a reporter that stopped is a failure that would otherwise look
like silence. Install it on a machine that reaches both hosts.

### The manifest

Every run writes `meta/manifest.json` describing each artifact it produced: the
source, the kind, and how it goes back. A tar names the path it untars to and
whether that needs root, a `vol-` tar names its docker volume, and a `.sql` dump
names the container it is piped into. The restore path reads the manifest rather
than a human remembering the mapping, and because the producer writes it, it
cannot drift from what the snapshot actually holds.

### Restoring

`roost backup --snapshots REPO`, `--ls`, and `--extract` browse a snapshot and
pull a path out to a new local directory, which must not already exist.
`--restore` puts a path back on the service host, and it reports what it would
write unless `--confirm` repeats the path exactly. It refuses entirely for
`opi-critical`, whose snapshots hold the staging directory that every run wipes:
extract the tar and follow `docs/opi-backup-restore.md` instead.

`roost backup --serve` presents the same thing as a web page, the way
`hatchery serve` does. It binds 127.0.0.1 and it is started by a person at a
shell, so there is nobody else to authenticate and no public surface: that is
what makes it safe to offer restore from a browser at all. Binding anywhere
else needs `--token`, and every request then carries it. The page browses
snapshots, extracts to a new directory, and restores in place behind the same
rules the command line uses, because it calls the same code.

Tab 5 of `roost ui` does the same walk without the command line. The status view
lists the repositories, `enter` opens one repository's snapshots newest first,
and `enter` again walks the snapshot's tree: `enter` descends, `left` goes back
up, `q` returns. On a selected entry, `e` extracts it to a path you type, which
must not already exist, and `R` restores it in place. `R` asks you to type the
path in full, because a keystroke is too cheap for the one action here that
cannot be undone.

### Locks, and why a failed step is not a failed backup

Retention and verification run after the snapshots are stored, so a failure
there records a warning and lets the run finish. Another machine reading the
repository holds a lock, and restic cannot tell that a lock on a different host
is dead, so it waits it out. Read-only commands pass `--no-lock` for that reason.

### History

The earlier `roost backup` (bin/backup-roost.sh) is retired. It pulled the
vault and watts storage mounts to a Mac, and `/var/lib/dokku` already holds
both. It also ran as a launchd agent that reached the opi over the LAN, so the
Local Network Privacy gate failed it on 10 of its last 14 nights.

Every site repo also has a private GitHub remote, so the opi is never the only
copy of anything.

## 7d. Reclaiming disk

`roost prune [project]` frees regenerable build artifacts per project (a repo
under `ROOST_PROJECTS_DIR`, default `~/repos`): `out/`, `dist/`, `build/`,
`DerivedData`, etc. **Dry-run by default** — it lists what it would free,
biggest first; `--yes` deletes. `--deep` also targets `node_modules`, `.build`,
and the electron/npm/Xcode caches (regenerable but slow to restore); `--caches`
does only the global caches. It never touches anything outside a known artifact
dir (source and `.git` are safe). Useful on the CI mini when Electron build
output fills the disk — `roost prune --deep` there surfaced 14 GB.

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
| vault OAuth creds | `dokku config vault` | GitHub/Google/Apple sign-in (§6) | provider consoles |
| pulse NODE_KEY | vault document `roost-<node>`, or `~/.roost_node_key` (600, per node), + `dokku config pulse NODE_KEY` | node-report.sh → pulse `/api/nodes`, and the other pulse posts | `config:set` new random hex, then update vault or each node's file |
| pulse CF_API_TOKEN (+ CF_ACCOUNT_ID) | `dokku config pulse` | tunnel status/colos on `/map` (`tunnel.cf`) | dash.cloudflare.com → API Tokens (Account → Cloudflare Tunnel → Read), then `config:set` |
| ci-live CI_KEY | vault document `roost-<node>`, or `~/.roost_ci_key` (600, on the CI Mac), + `dokku config ci-live CI_KEY` | ci-live-report.sh → ci-live `/api/runs` | `config:set` new random hex, then update vault or the poller Mac's file |
| vault app key | `ROOST_VAULT_APP_KEY` in `~/.roostrc` (per host) | `lib/roost-secret.sh`, `lib/roost_secret.py` | vault admin, then the host's `~/.roostrc` |
| Home Assistant token | `~/.ha_token` (600) | `ha-scoop.py`, the HA path of `tapo-poll.py` | HA Profile → Security |
| TP-Link account password | `~/.tapo_pass` (600) | `tapo-poll.py` | the TP-Link account |

Never commit any of these; `rates.json` and other derived data are public.

## 9. The roost (current fleet)

This table lists the apps this playbook uses as examples. hatchery's declaration lists every app on the box.

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
