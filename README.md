# Roost

<img src="assets/roost-mark.svg" width="96" alt="Roost — a swoosh rooster on its perch">

A self-hosted app platform on one small box. One ARM SBC (or any
Debian/Ubuntu machine), Dokku, a Cloudflare tunnel, and a toolbelt of
scripts — serving any number of apps at `<name>.yourdomain`, deployed by
`git push`, behind CGNAT with no public IP and no port forwarding.

```
Internet ─▶ Cloudflare ─▶ tunnel (dials OUT) ─▶ nginx :80 ─▶ Dokku app containers
```

New app, one command, ~40 seconds to a live URL:

```sh
bin/new-app.sh myapp --static      # or --node, --swift (Hummingbird 2), --board (statusgen)
```

## The `roost` command

Everything is driven by one dispatcher, [bin/roost](bin/roost) (add
`bin/` to your PATH). `roost help` prints this list; `roost doctor`
diagnoses the setup when anything misbehaves.

| Command | What |
|---|---|
| `roost new <name> [--static\|--node\|--swift\|--board]` | nothing → live app in ~40 s |
| `roost route <subdomain>` | publish a tunnel route via the Cloudflare API |
| `roost status ["message"]` | collect + validate + deploy the status site (no message: narrative auto-composed from merged PRs) |
| `roost stats` | run the configured board-stat collectors |
| `roost fleet` | refresh the fleet board json |
| `roost kick` | fire the status runner's hourly deploy now |
| `roost apps` / `ps [app]` / `logs <app> [-n N]` / `restart <app>` / `config <app> [K=V …]` | day-2 Dokku operations over ssh |
| `roost prune [project] [--yes] [--deep] [--caches]` | reclaim build artifacts (dry-run by default) |
| `roost backup` | pull pi data to `~/Backups/roost` |
| `roost doctor` | diagnose ssh, token, zone, and tooling |
| `roost ui` | full-screen terminal: console, monitor, config, docs tabs |

Configuration lives in `~/.roostrc` ([roostrc.example](roostrc.example));
secrets live in separate chmod-600 dotfiles (`~/.cf_api_token`,
`~/.roost_node_key`, `~/.roost_ci_key`), never in the rc file or the repo.

## What's here

| Path | What |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Prerequisites → first deploy: hardware, accounts, installs, tunnel |
| [docs/playbook.md](docs/playbook.md) | The operating manual: storage, crons, secrets, accounts, status boards, disk reclaim, and every gotcha learned the hard way |
| [docs/status-events.md](docs/status-events.md) | Design sketch (future): push-based CI → central ingest → boards + history |
| [bin/roost](bin/roost) | The dispatcher — every command above |
| [bin/new-app.sh](bin/new-app.sh) | Nothing → live app: Dokku app + domain + scaffold + deploy + route + verify |
| [bin/publish-route.sh](bin/publish-route.sh) | Publish a subdomain through the Cloudflare tunnel via API — no dashboard |
| [bin/status.sh](bin/status.sh) | The `roost status` orchestrator: self-update, collect, validate, deploy |
| [bin/fleet-board.py](bin/fleet-board.py) / [bin/fleet-alert.py](bin/fleet-alert.py) | Fleet snapshot board + state-transition desktop/ntfy alerts |
| [bin/node-report.sh](bin/node-report.sh) | Per-Mac telemetry (load/mem/disk/watts/battery/runner) → pulse `/api/nodes`; launchd installer alongside |
| [bin/ci-live-report.sh](bin/ci-live-report.sh) | Live CI-run poller (runs on the CI Mac) → the ci-live app; launchd installer alongside |
| [bin/gen-narrative.py](bin/gen-narrative.py) | Composes the board narrative from merged PRs when `roost status` gets no message |
| [bin/roost-prune.py](bin/roost-prune.py) / [bin/backup-roost.sh](bin/backup-roost.sh) | Disk reclaim (dry-run default) / nightly storage-mount backups |
| [bin/roost-ui.py](bin/roost-ui.py) | `roost ui` — full-screen terminal in four tabs: console (prompt + streaming commands), monitor (live fleet via pulse), config, docs pager (stdlib only) |

Each script carries its own usage/config header — the headers are the
authoritative per-tool reference. Tests: `python3 -m unittest discover -s tests`.

Rendered docs: [docs.jimmyhoughjr.net](https://docs.jimmyhoughjr.net)

## The reference roost

What this pattern runs in production, on one 8-core / 16 GB Orange Pi:

- [watts](https://watts.jimmyhoughjr.net) — electric cost calculator (EIA rates cron, seasonal modeling)
- [vault](https://vault.jimmyhoughjr.net) — Apple/Google sign-in + per-app user storage (Swift/Hummingbird 2)
- [head2head](https://head2head.jimmyhoughjr.net) — measured implementation shootouts (Node vs Swift, bout 1)
- [status](https://status.jimmyhoughjr.net) — [statusgen](https://github.com/jhoughjr/statusgen) boards with git-generated history
- [docs](https://docs.jimmyhoughjr.net) — this repo's docs plus living usage reports
- a blog, and a `hello` created by `new-app.sh` as its living test

## Companion projects

- [statusgen](https://github.com/jhoughjr/statusgen) — data-driven status boards (and the bare-metal SETUP.md for the locally-managed-tunnel variant)

Built by Jimmy Hough Jr & Claude. Donations appreciated:
[$jimmyhoughjr](https://cash.app/$jimmyhoughjr)
