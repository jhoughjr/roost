# Roost

<img src="assets/roost-mark.svg" width="96" alt="Roost — a swoosh rooster on its perch">

A self-hosted app platform on one small box. One ARM SBC (or any
Debian/Ubuntu machine), Dokku, a Cloudflare tunnel, and a toolbelt of
scripts — serving any number of apps at `<name>.yourdomain`, deployed by
`git push`, behind CGNAT with no public IP and no port forwarding.

Roost also runs the box after the apps are up: **status boards** that publish
what the fleet is doing, **fleet observability** through pulse, **backups and
restore** for the whole stack, and **disk reclaim**. A roost is where things
come home to rest, so keeping them safe is part of the job.

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
| `roost lan-cert <name> <user@host> <dir> [label]` | issue or renew a LAN name's certificate through DNS-01 in the certbot container, and place it on the host that serves it; `bin/install-lan-cert.sh` runs it daily |
| `roost status ["message"]` | collect + validate + deploy the status site (no message: narrative auto-composed from merged PRs) |
| `roost stats` | run the configured board-stat collectors |
| `roost fleet` | refresh the fleet board json |
| `roost kick` | fire the status runner's hourly deploy now |
| `roost rollout [--kick]` | ff-only pull roost + statusgen on every writer machine after a merge |
| `roost apps` / `ps [app]` / `logs <app> [-n N]` / `restart <app>` / `config <app>` | day-2 Dokku reads over ssh. Config writes moved to `hatchery config set`, which keeps the declaration true; `--force` keeps the old direct write for emergencies |
| `roost prune [project] [--yes] [--deep] [--caches]` | reclaim build artifacts (dry-run by default) |
| `roost backup [--json|--run|--check|--serve]` | backup service state; `--serve` opens a local web page, and tab 5 of `roost ui` does the same in the terminal |
| `roost secrets` | say where this host reads each secret from: vault, a legacy file, or nowhere |
| `roost doctor` | diagnose ssh, token, zone, and tooling |
| `roost ui` | full-screen terminal: console, monitor, config, docs tabs |

Configuration lives in `~/.roostrc` ([roostrc.example](roostrc.example)). The
pulse `NODE_KEY` and the ci-live `CI_KEY` come from vault, described in the
next section. Every other secret is still a chmod-600 dotfile
(`~/.cf_api_token`, `~/.ha_token`, `~/.tapo_pass`), never in the rc file and
never in the repo.

## Secrets from vault

A host used to hold one file per secret, placed there by a person. Rotating
the pulse `NODE_KEY` meant visiting every node and getting every one of them
right. A host now holds one secret of its own, its vault app key, and fetches
the rest from vault's sealed per-app document.

`lib/roost-secret.sh` and `lib/roost_secret.py` are the one reader, and both
resolve a name in the same three arms:

1. the vault document for `ROOST_VAULT_APP`, fetched with `ROOST_VAULT_APP_KEY`
2. the legacy file for that name: `~/.roost_node_key` for `NODE_KEY`, and
   `~/.roost_ci_key` for `CI_KEY`
3. nothing, and the caller says so rather than sending an empty key

Three keys in `~/.roostrc` turn the first arm on:

| Key | What |
|---|---|
| `ROOST_VAULT_URL` | vault base URL (default `https://vault.jimmyhoughjr.net`) |
| `ROOST_VAULT_APP` | the app whose document holds this host's secrets |
| `ROOST_VAULT_APP_KEY` | the app key that opens it, the one secret the host still holds |

The app is one per host, named `roost-<ROOST_NODE_NAME>`, so
`roost-jimmys-mac-mini` and `roost-opi`. A node key belongs to one host, and
revoking one host's key must not touch the others. The names inside the
document are `NODE_KEY` and `CI_KEY`.

The app key travels in a curl header file and never on a command line. The
document is cached for 300 seconds at mode 600 under `TMPDIR`, shared by both
languages, because node-report runs every 30 seconds and a read per run would
be ten vault calls a minute from every host.

The cutover runs one host at a time, because the legacy file keeps answering
while it exists. Put the three keys in that host's `~/.roostrc`, then run:

```sh
roost secrets
```

It prints one line per name and never a value. Delete the legacy file on that
host once both lines say `vault`.

## What's here

| Path | What |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Prerequisites → first deploy: hardware, accounts, installs, tunnel |
| [docs/tutorial.md](docs/tutorial.md) | Guided tour: deploy an app, board it, operate it — and which of the three repos to touch |
| [docs/playbook.md](docs/playbook.md) | The operating manual: storage, crons, secrets, accounts, status boards, fleet observability, backups and restore, disk reclaim, and every gotcha learned the hard way |
| [homeauto](https://github.com/jhoughjr/homeauto) | Smart plugs, bulbs and Home Assistant: wiring, credential locations, rebuild-from-nothing plan (separate repo) |
| [docs/status-events.md](docs/status-events.md) | Design sketch (future): push-based CI → central ingest → boards + history |
| [bin/roost](bin/roost) | The dispatcher — every command above |
| [lib/roost-secret.sh](lib/roost-secret.sh) / [lib/roost_secret.py](lib/roost_secret.py) | The one secret reader, shell and Python: vault first, the legacy file after it, and one cached document per host |
| [bin/new-app.sh](bin/new-app.sh) | Nothing → live app: Dokku app + domain + scaffold + deploy + route + verify, and its kind file |
| [bin/publish-route.sh](bin/publish-route.sh) | Publish a subdomain through the Cloudflare tunnel via API — no dashboard |
| [bin/status.sh](bin/status.sh) | The `roost status` orchestrator: self-update, collect, validate, deploy |
| [bin/fleet-board.py](bin/fleet-board.py) | Fleet snapshot board. Alerting is pulse's, from the readings roost posts it |
| [bin/node-report.sh](bin/node-report.sh) | Per-node telemetry (load/mem/disk/watts/battery/runner/temps) → pulse `/api/nodes`, macOS + Linux; launchd/systemd installer alongside |
| [bin/ci-live-report.sh](bin/ci-live-report.sh) | Live CI-run poller (runs on the CI Mac) → the ci-live app; launchd installer alongside |
| [bin/gen-narrative.py](bin/gen-narrative.py) | Composes the board narrative from merged PRs when `roost status` gets no message |
| [bin/roost-prune.py](bin/roost-prune.py) | Disk reclaim (dry-run default) |
| [bin/opi-backup.sh](bin/opi-backup.sh) | Nightly disaster-recovery backup of the opi stack; runs on the opi, writes restic repositories to the drive on the mini |
| [bin/backup-ui.html](bin/backup-ui.html) | The page `roost backup --serve` presents: status, snapshot browser, extract and restore |
| [bin/backup-status.py](bin/backup-status.py) | Reads that service from any machine: the schedule and last run on the service host, the repositories on the storage host |
| [bin/backup-report.sh](bin/backup-report.sh) | Pushes that reading to pulse `/api/backups` for the dashboard card; hourly launchd/systemd installer alongside |
| [bin/roost-ui.py](bin/roost-ui.py) | `roost ui` — full-screen terminal in five tabs: console (prompt + streaming commands), monitor (live fleet via pulse), config, docs pager, backups (status, snapshot browser, extract and restore) (stdlib only) |

Each script carries its own usage/config header — the headers are the
authoritative per-tool reference. Tests: `python3 -m unittest discover -s tests`.

Rendered docs: [docs.jimmyhoughjr.net](https://docs.jimmyhoughjr.net)

## The reference roost

What this pattern runs in production, on one 8-core / 16 GB Orange Pi:

- [watts](https://watts.jimmyhoughjr.net) — electric cost calculator (EIA rates cron, seasonal modeling)
- [vault](https://vault.jimmyhoughjr.net) — Apple/Google sign-in + per-app user storage (Swift/Hummingbird 2)
- [head2head](https://head2head.jimmyhoughjr.net) — measured implementation shootouts (Node vs Swift, bout 1)
- [status](https://status.jimmyhoughjr.net) — [statusgen](https://github.com/jhoughjr/statusgen) boards with git-generated history
- [pulse](https://pulse.jimmyhoughjr.net) — live fleet dashboard and system map, and the Backups card
- [docs](https://docs.jimmyhoughjr.net) — this repo's docs plus living usage reports
- a blog, and a `hello` created by `new-app.sh` as its living test

## Companion projects

- [statusgen](https://github.com/jhoughjr/statusgen) — data-driven status boards (and the bare-metal SETUP.md for the locally-managed-tunnel variant)

Built by Jimmy Hough Jr & Claude. Donations appreciated:
[$jimmyhoughjr](https://cash.app/$jimmyhoughjr)
