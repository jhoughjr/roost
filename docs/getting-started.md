# Roost — getting started

Prerequisites to first deploy.

**Roost** is a self-hosted app platform on one small box. The
[playbook](#playbook.md) assumes a working stack. This guide gets you *to* that stack from nothing: what to buy,
what to install, what accounts you need, and the first live app.

What you're building:

```
Internet ─▶ Cloudflare ─▶ tunnel (dials OUT — works behind CGNAT) ─▶ nginx :80 ─▶ Dokku apps
```

One small box serves any number of apps at `<name>.yourdomain`, deployed
by `git push`, with no port forwarding, no public IP, and no dashboard
after the initial setup.

---

## 0. Prerequisites checklist

**Hardware — the host.** Any Debian/Ubuntu box: an ARM64 SBC (Orange Pi,
Raspberry Pi), an old mini PC, or a VPS. 4 GB RAM runs static sites and
Node services comfortably; **16 GB if you want to build Swift on-device**
(compiles happen on the box). Reference fleet: an 8-core, 16 GB Orange Pi.

**Accounts.**

| Account | Needed for | Cost |
|---|---|---|
| Cloudflare + a domain using its nameservers | the tunnel and DNS | free plan is fine |
| GitHub | publishing repos, portfolio stats | free |
| Apple Developer / Google Cloud | only if you want vault-style sign-in | Apple $99/yr, Google free |
| EIA API key | only for the watts rates pipeline | free |

**Workstation** (macOS or Linux):

- `git`, `curl`, `python3` — usually already present (macOS: `xcode-select --install`)
- an SSH keypair: `ssh-keygen -t ed25519` if `~/.ssh/id_ed25519.pub` doesn't exist
- optional: `gh` (GitHub CLI), Swift/Xcode (only to type-check Swift apps
  locally before the slower pi build), Node (only to test Node apps locally)

---

## 1. Prepare the host

SSH in (or sit at it):

```sh
sudo apt update && sudo apt -y upgrade
sudo apt -y install curl git
sudo hostnamectl set-hostname mybox
sudo systemctl enable --now ssh        # never lock yourself out
```

## 2. Install Dokku

Check [dokku.com](https://dokku.com/docs/getting-started/installation/)
for the current version, then:

```sh
wget -NP . https://dokku.com/install/v0.35.20/bootstrap.sh
sudo DOKKU_TAG=v0.35.20 bash bootstrap.sh

sudo dokku domains:set-global yourdomain.net
```

Authorize your workstation to deploy (paste your **public** key):

```sh
cat ~/.ssh/id_ed25519.pub | sudo dokku ssh-keys:add laptop
```

From your workstation, `ssh dokku@<host-ip> apps:list` should now answer.
That one channel does everything: Dokku commands and `git push` deploys.

## 3. Create the tunnel (remotely-managed)

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a
tunnel** (Cloudflared type). Name it, then copy the one-line connector
install command it shows and run it **on the host**. That's the whole
tunnel setup — the connector installs as a service and survives reboots.

> **Why remotely-managed:** the toolbelt's `publish-route.sh` publishes
> new subdomains through Cloudflare's API, which only works on
> remotely-managed tunnels. The locally-managed "catch-all config file"
> variant in [statusgen's SETUP.md](https://github.com/jhoughjr/statusgen/blob/main/SETUP.md)
> also works — but then routes are added with `cloudflared tunnel route
> dns` on the host instead of `publish-route.sh`, and you maintain the
> ingress rules yourself. Pick one; don't mix.

No ingress rules needed yet — `publish-route.sh` adds one per app, each
pointing at `http://localhost:80` (Dokku's nginx routes by hostname; every
app shares that one port).

## 4. Set up the workstation toolbelt

Clone the toolbelt (this repo):

```sh
git clone https://github.com/jhoughjr/roost ~/repos/roost
```

Create a Cloudflare API token at
[dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
→ *Create Token* → *Custom token* with exactly two permissions:

- **Zone → DNS → Edit** (scoped to your zone)
- **Account → Cloudflare Tunnel → Edit**

Store it where the scripts look:

```sh
printf '%s' '<token>' > ~/.cf_api_token && chmod 600 ~/.cf_api_token
```

Finally, configure the toolbelt — copy `roostrc.example` from the repo
to `~/.roostrc` and fill in your host, domain, and status-site path. Then
run `roost doctor` (add `~/repos/roost/bin` to your PATH) — it checks the
SSH channel, the token, the zone, and your tooling, and tells you exactly
what's missing.

## 5. First app — one command

```sh
~/repos/roost/bin/new-app.sh hello --static
```

~40 seconds later `https://hello.yourdomain.net` is live: Dokku app,
domain, scaffolded repo at `~/hello-site`, first deploy, DNS + tunnel
route via API, LAN and public verification. Templates: `--static`
(nginx), `--node` (zero-dep server with `/health`), `--swift`
(Hummingbird 2; first pi build ~8 min).

If the browser says "can't find the server" while the script's public
check passed: that's your machine's cached negative DNS from before the
route existed. Flush (`sudo dscacheutil -flushcache; sudo killall -HUP
mDNSResponder` on macOS) or wait it out.

## 6. Where to go next

- **The [playbook](#playbook.md)** — persistent storage,
  crons, secrets, accounts (vault), status boards, and every gotcha this
  stack has taught us, including the troubleshooting table in
  [statusgen SETUP.md](https://github.com/jhoughjr/statusgen/blob/main/SETUP.md)
  for tunnel/DNS issues.
- **Status boards** — [statusgen](https://github.com/jhoughjr/statusgen):
  JSON in, styled boards out, with a History board generated from git.
- **Reboot test** — before trusting the box, `sudo reboot` and confirm
  your app comes back on its own. Everything above installs as services;
  if something doesn't return, find out now, not during an outage.
