# Roost and hatchery: what each one abstracts

Two tools, one lab. They are siblings on purpose, and their marks say so:
hatchery draws a cracked egg on the same perch roost draws its rooster on, same
64x64 field, same flat geometry, same `#C4602A`. The boundary is one sentence,
from hatchery's own README:

> roost owns machines; hatchery owns the stacks that hatch on them.

That sentence is easy to agree with and hard to apply while you are standing in
front of a broken deploy deciding which tool to reach for. Both can put a Dokku
app on a box. One box carries an app roost deployed **and** six services
hatchery declared. This page says what the sentence means in practice.

## The three strata

The lab is three layers deep. Each layer answers exactly one question, and the
middle layer is shared, which is precisely where the two tools get confused for
each other.

```
┌───────────────────────────────────────────────────────┬──────────────────┐
│  CONFIG                                               │  hatchery        │
│    does this service have what it needs?              │  owns            │
│    service kind + env contract · stack manifest       │                  │
├───────────────────────────────────────────────────────┼──────────────────┤
│  PLATFORM                                             │  hatchery        │
│    what runs a service, and where?                    │  declares        │
│    dokku · Cloud Run · App Runner · App Platform      │  roost           │
│                                                       │  provisions      │
├───────────────────────────────────────────────────────┼──────────────────┤
│  HARDWARE                                             │  roost           │
│    what does the platform sit on, and how does        │  owns            │
│    the outside reach it?                              │                  │
│    opi · mini · pi · CGNAT · tunnel · nginx           │                  │
└───────────────────────────────────────────────────────┴──────────────────┘

   deeper ▼   an answer at any layer is worthless if the layer under it lies
```

hatchery comes down from a declaration. roost comes up from a machine. They
meet in the middle.

| Stratum | The question | Owner | What you type |
|---|---|---|---|
| config | Does this service have what it needs to be correct? | hatchery | `config validate` · `audit` · `sync` |
| platform | What runs a service, and where? | hatchery declares, roost provisions | `service new` · `roost new` |
| hardware | What does the platform sit on, and how does the outside reach it? | roost | `roost route` · `apps` · `doctor` |

## What roost abstracts

**A name becomes a live URL.** That is the abstraction, and everything else
roost does exists to keep it true.

```
Internet ──▶ Cloudflare ──▶ tunnel (dials OUT) ──▶ nginx :80 ──▶ Dokku containers
```

`roost new myapp --swift` goes from nothing to a served app in about forty
seconds: Dokku app, domain, scaffold, deploy, tunnel route, verify. It works
behind CGNAT with no public IP and no port forwarding, because the tunnel dials
out rather than waiting to be dialled.

Below that line roost owns everything true of the **machine** rather than of any
app on it: day-2 Dokku operations over ssh (`apps`, `ps`, `logs`, `restart`,
`config`, `prune`, `backup`), per-node telemetry, the fleet board, the
smart-plug and Home Assistant pollers, and the schedules that run all of it. Its
configuration is `~/.roostrc`, a flat file of `ROOST_*` keys read by splitting
on `=`, with secrets in separate chmod-600 dotfiles.

roost is **imperative and scheduled**. It is a toolbelt with a cron behind it.

## What hatchery abstracts

**A service kind has an environment contract, and the contract is data.** That
is the abstraction, and it is what lets hatchery answer questions roost cannot.

A manifest names stacks, a stack holds services, and a service has a kind
(`mwserver`, `payment-gateway`) and a backend. Because the contract is data
rather than code:

- `config validate` says a service is misconfigured **before** anything is deployed.
- `config audit` says the live box has drifted from what was declared.
- `stack clone` decides key by key what a copy into another environment should do: carry it, rewrite the names in it, mint a fresh one, or refuse and say why. Nothing that points at the source's database or grants the source's authority is ever copied.

Providers are the top level rather than an afterthought. The dashboard opens on
which backends exist and whether **this machine** is configured for each, before
anything is defaulted to one.

hatchery is **declarative and convergent**. It is a model with a reconciler
behind it.

### It does not run inside what it manages

There is no Dockerfile in the hatchery repo on purpose. Running it as an app on
the box it administers means a restart of that stack kills the tool mid-action,
and the moment you most need it, box wedged and apps down, is exactly the moment
it would not be there.

So it is a local process. On this lab it runs on the laptop at
`192.168.0.162:7878`, bound to loopback and token-gated, and the mini's status
collector reaches it across the LAN to draw the Stacks tab on the board.

## Where the seam is

The two meet on one box. `192.168.0.103` (opi) carries the `status` app that
roost deploys **and** the `mwlab` / `mwlab-2` stacks that hatchery declares.
Same Dokku, two different stories about how something got there.

```
  the writers                             opi · 192.168.0.103 · dokku
                                        ┌──────────────────────────────────┐
  hatchery serve                        │                                  │
  laptop · :7878  ──declares · audits──▶│  mwlab · paylab · comlab         │
       │                                │  mwlab-2 · ...                   │
       │ LAN, curl                      │                                  │
       ▼                                │                                  │
  statusgen collectors ◀── GitHub       │                                  │
  mini · every 900s        Actions      │                                  │
       │                                │                                  │
       ▼                                │                                  │
  status-site clone                     │  status app ─▶ the board         │
       │  git push --force (retry)      │                                  │
       └───────────────────────────────▶│                                  │
                                        └──────────────────────────────────┘
```

Read the two paths into opi and the gap states itself. The lab stacks arrive
from a declaration that can be validated before the fact and audited after it.
The status app arrives from a shell script that pushes.

Concretely, for the `status` app:

- Nothing declares what it should be, so nothing can audit whether it still is.
- Nothing validates its configuration before the push.
- `validate-board.py` checks the board **data** and says nothing about the deployment.
- The push is a forced one in a retry loop, because the mini's LAN access flaps.

**Ruled: later.** roost predates hatchery, the status pipeline works, and
rewriting a working pipeline to make a diagram symmetrical is not a reason.
Written down so the next person to look knows the gap is known rather than
missed.

## Which tool do I reach for

| The thing in front of you | Reach for |
|---|---|
| A new app needs a URL on the lab box | `roost new` |
| A subdomain needs publishing through the tunnel | `roost route` |
| An app is up but wrong, and you want its logs or its env | `roost logs` · `roost config` |
| A box is out of disk | `roost prune` |
| The status board is stale or wrong | `roost status` |
| A service needs an env key and you do not know which | `hatchery config validate` |
| The live box may have drifted from what was declared | `hatchery config audit` |
| You need this stack again in another environment | `hatchery stack clone` |
| Something is down and you want the history of when it turned | `hatchery events` |
| You are starting from an empty directory and a bare box | `hatchery stack new` |

## Where the board already shows all three

The Clauffice status board reads bottom-up through the same layers, which is a
decent way to learn them:

- **Stacks** is hatchery's own answer, live: which stacks are declared, which services answer, on which backend, at what latency. That is the config stratum.
- **Deployed servers** is what is actually running on the Dokku box. That is the platform stratum.
- The **fleet** board and the node telemetry are the machines themselves. That is the hardware stratum.

When those three disagree, the disagreement is the finding. A service that is
declared, is not running, on a box that is up, is a different problem from all
three being down.
