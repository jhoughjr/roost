# Roost — a guided tour

A follow-along tutorial. By the end you'll have deployed an app, changed
it, given it a status board, and operated it — and you'll know which of
the three repos to touch when something needs to change.

This is the **workflow** guide. Two neighbours cover the other halves:

- [getting-started.md](getting-started.md) — building the box itself
  (hardware, Dokku, the tunnel). Do that first if you don't have a roost
  yet; come back here.
- [playbook.md](playbook.md) — the reference manual. Deeper on every
  topic, organized to look things up rather than to read start to finish.

**What you need:** a working roost (the box answers `roost doctor`
cleanly), `bin/` on your PATH, and about an hour.

Start by confirming the platform is healthy:

```sh
roost doctor
```

Every line should end in `✓`, followed by `✓ roost is healthy`. A `✗`
tells you exactly what's missing — fix that before continuing, because
everything below depends on the ssh channel and the Cloudflare token
being good.

---

## 0. The mental model

Two ideas explain almost everything.

**One box, dialing out.** Your apps run in Dokku containers on a single
small machine. Nothing dials *in* — a Cloudflare tunnel dials *out* from
the box, so it works behind CGNAT with no public IP and no port
forwarding. Deploys are `git push`. There is no dashboard to click.

```
Internet ─▶ Cloudflare ─▶ cloudflared tunnel ─▶ nginx :80 ─▶ Dokku containers
```

**Three repos, one job each.** The status-board side of the platform is
split three ways. Newcomers usually lose an hour to this, so here it is
plainly:

| Repo | Role | Holds | You touch it when… |
|---|---|---|---|
| **roost** (this one) | the **driver** | the `roost` command, collectors that know your fleet, deploy orchestration | you're changing *how* things run |
| **statusgen** | the **library** | board schema, the HTML/CSS/JS renderer, the validator, generic collectors | you're changing how boards *look* or *validate* |
| **status-site** | the **data** | `board.json` per board, thin HTML shells, nothing else | you're changing what a board *says* |

The reason for the split: `board.json` is a contract. Because the data is
pure and the renderer is shared, you can restyle every board at once
(statusgen), or rewrite how numbers are gathered (roost), without either
side knowing what the other did. `roost status` is the one command that
runs the whole pipeline across all three.

The formal version of that contract is
[statusgen/INTERFACES.md](https://github.com/jhoughjr/statusgen/blob/main/INTERFACES.md).
You don't need it yet.

---

## 1. Nothing → a live app

```sh
roost new demo --static
```

That's the whole thing. It prints its steps as it goes — creating the
Dokku app and mapping `demo.<your-domain>`, scaffolding a repo, making
the first deploy, publishing the tunnel route, then verifying over both
LAN and the public URL — and finishes with:

```
✓ https://demo.<your-domain> is live
```

If the public check prints a code that isn't `200`, wait a few seconds
and reload: DNS propagation is the usual cause, and the app is already
serving on the LAN line above it.

**What actually happened**, because you'll want to do these individually
later:

1. `dokku apps:create` + `domains:set` over ssh — no dashboard involved.
2. A scaffolded repo in `~/demo-site` with a Dockerfile and a `dokku` git
   remote already wired.
3. `git push dokku main` — Dokku built the image and started the
   container.
4. A Cloudflare DNS record published through the API by
   `roost route demo` (usable on its own for any subdomain).

Other flavors: `--node` (zero-dependency Node server with `/health`),
`--swift` (Hummingbird 2 — the first build on the box takes ~8 minutes),
and `--board`, which we'll use in part 4.

---

## 2. Change it, ship it

Deploys are just git. Edit and push:

```sh
cd ~/demo-site
$EDITOR index.html
git commit -am "say something else"
git push dokku main
```

Reload the page. That is the entire deploy story for every app on the
platform — there is no second mechanism to learn.

---

## 3. Operating it

Four commands cover almost every day-2 need. All of them go over the same
ssh channel `roost doctor` checked:

```sh
roost apps                 # everything deployed on the box
roost ps demo              # is it running?
roost logs demo -n 50      # what did it say?
roost restart demo         # turn it off and on again
```

Configuration and secrets are environment variables on the app, set the
same way you'd read them:

```sh
roost config demo                    # show
roost config demo GREETING=hello     # set (this restarts the app)
```

Two things not to learn the hard way:

- **Config values live only on the box.** They are not in your repo and
  not in `~/.roostrc` — so they are also not in any backup unless you
  wrote them down. Keep a note of what each app needs.
- **Persistent data needs a mount.** A container's filesystem is
  discarded on every deploy. Anything that must survive needs
  `dokku storage:mount` — see playbook §3.

---

## 4. Give it a status board

A board is a `board.json` plus a shared renderer. Scaffold a standalone
one the same way you scaffolded the app:

```sh
roost new demo-board --board
```

You get a deployed board at `https://demo-board.<your-domain>` with the
renderer copied out of your statusgen clone. Now edit the data:

```sh
cd ~/demo-board-site
$EDITOR board.json          # add sections: stats, cards, tables, charts…
git push dokku main
```

The schema — every section kind, with examples — is
[BOARD_SCHEMA.md](https://github.com/jhoughjr/statusgen/blob/main/BOARD_SCHEMA.md)
in statusgen. Validate before you push and you'll never deploy a broken
board:

```sh
python3 ~/repos/statusgen/bin/validate-board.py board.json
```

(That's wherever your statusgen clone lives — the same path
`ROOST_STATUSGEN` names in `~/.roostrc`. The rc is sourced by roost's own
scripts, not by your shell, so spell the path out here.)

When the renderer improves upstream, pull the new one in and push:

```sh
~/repos/statusgen/bin/sync-renderer.sh ~/demo-board-site
git -C ~/demo-board-site commit -am "renderer update" && git -C ~/demo-board-site push dokku main
```

That's a standalone board: one board, one app, you own the data by hand.
The multi-board hub is the next part.

---

## 5. Boards that update themselves

`status-site` is the hub — many boards under one domain, most of their
numbers collected automatically. One command runs everything:

```sh
roost status "what changed today"
```

In order, that: refreshes its own clones (roost, statusgen, the site, and
your source repos), runs the collectors, syncs the renderer, **validates
every board against the schema — fatally**, commits, and deploys.

The gate matters: a board that doesn't satisfy the schema stops the
deploy instead of shipping a broken page.

Collectors are configured entirely in `~/.roostrc` — which repos to read,
which board and column each result lands in. `roostrc.example` documents
every key with a comment explaining what it produces. Start by copying
one collector's keys and pointing them at your own repo.

Two habits worth forming now:

```sh
ROOST_STATUS_DRYRUN=1 roost status     # regenerate + validate, no commit, no deploy
roost status                           # no message: composes one from merged PRs
```

Run the dry-run whenever you've touched a collector or a board — it
exercises the whole pipeline without publishing anything.

---

## 6. Watching the fleet

```sh
roost fleet     # regenerate the fleet board: every app, disk, memory, HTTP health
roost ui        # full-screen terminal — console, live monitor, config, docs
```

For unattended alerting, `bin/install-fleet-alert.sh` installs a launchd
agent that checks every 15 minutes and notifies **on state transitions
only** — an app going down, or disk crossing 85%. Silence means nothing
changed, which is what makes it worth having: an alert that fires every
15 minutes is one you'll learn to ignore.

Set `ROOST_NTFY_TOPIC` in `~/.roostrc` and the same alerts reach your
phone.

When the box gets tight, reclaim build artifacts — dry-run by default, so
looking is free:

```sh
roost prune                # show what's reclaimable, everywhere
roost prune myproject --yes
```

---

## 7. Keeping several machines in sync

If more than one machine runs `roost status` — say a laptop plus a
scheduled runner — they must agree on the code, or they'll regenerate
boards differently and fight each other.

After merging anything in roost or statusgen:

```sh
roost rollout          # ff-only pull of roost + statusgen here and on every ROOST_WRITERS machine
roost rollout --kick   # …then fire the runner's deploy immediately
```

A clone that's dirty or diverged is reported and **left alone** — never
force-updated — and the command exits non-zero so a half-done rollout
can't look finished. `roost kick` on its own just triggers the runner.

Set `ROOST_WRITERS` in `~/.roostrc` to your machines (space-separated ssh
targets); leave it empty on a single-machine setup.

---

## 8. The failure modes that actually bite

Ranked by how much time they cost the first time.

**Hand-edited board sections need GitHub first.** Sections no collector
owns — a banner, a hand-kept table — must be committed in `status-site`
and pushed to **GitHub `origin main` before** you run `roost status`.
The pipeline rebases onto `origin/main` and force-pushes the deploy
remote, so an edit that exists only locally can be dropped when another
writer wins the race. Once it's on `origin/main` it survives every
later collector cycle.

**Uncommitted changes in `~/status-site` are not safe.** The site is
derived data, so the pipeline is entitled to hard-reset it to the remote
— and will, on any pull hiccup. Commit before you run anything.

**A conflicted status run now fails loudly.** If `roost status` exits
non-zero saying `status NOT posted`, it means it lost a race twice; just
run it again. What it will never do again is print `✓ deployed` over a
message that was silently discarded.

**Check the exit code, not the vibe.** Collector failures are non-fatal
by design — one broken collector shouldn't block a deploy — so they print
`note: … failed` and the run continues. Read the notes.

---

## Where to go next

- [playbook.md](playbook.md) — persistent storage (§3), scheduled jobs
  (§4), secrets (§5), sign-in and per-app user data via vault (§6),
  backups (§7c), and the accumulated gotchas (§8).
- `roost help` — the current command list, always accurate; the script
  headers in `bin/` are the authoritative per-tool docs.
- `roostrc.example` — every configuration key with a comment saying what
  it produces.
- [statusgen](https://github.com/jhoughjr/statusgen) — board schema,
  renderer, and the collector library.

**Extending the command itself:** an unrecognized command dispatches to
`roost-<name>` on your PATH (then `$ROOST_STATUSGEN/bin`), git-style. A
plugin is any executable with the right name — no registration.
