# Roost

A self-hosted app platform on one small box. One ARM SBC (or any
Debian/Ubuntu machine), Dokku, a Cloudflare tunnel, and a toolbelt of
scripts — serving any number of apps at `<name>.yourdomain`, deployed by
`git push`, behind CGNAT with no public IP and no port forwarding.

```
Internet ─▶ Cloudflare ─▶ tunnel (dials OUT) ─▶ nginx :80 ─▶ Dokku app containers
```

New app, one command, ~40 seconds to a live URL:

```sh
bin/new-app.sh myapp --static      # or --node, or --swift (Hummingbird 2)
```

## What's here

| Path | What |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Prerequisites → first deploy: hardware, accounts, installs, tunnel |
| [docs/playbook.md](docs/playbook.md) | The operating manual: storage, crons, secrets, accounts, status boards, and every gotcha learned the hard way |
| [bin/new-app.sh](bin/new-app.sh) | Nothing → live app: Dokku app + domain + scaffold + deploy + route + verify |
| [bin/publish-route.sh](bin/publish-route.sh) | Publish a subdomain through the Cloudflare tunnel via API — no dashboard |

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
