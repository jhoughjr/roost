# Status events — push-based CI → boards → history (design sketch)

> **Status: proposal / future direction. Not built.** Written 2026-07-13.
> A north star for unifying CI, status boards, and history — react to it, don't
> treat it as a spec. Companion to statusgen's `INTERFACES.md`.

## Why

Today status flows three disconnected ways:

- **Pull collectors** — `statusgen/bin/collect/*` reach into GitHub per push
  (`repo_stats`, `ci_status`, `shipped_week`, `api_consumption`) and rewrite
  board sections.
- **Manual narration** — `roost status "message"` writes a commit whose subject
  becomes the human story.
- **Reverse-engineered history** — `collect/history.py` reads the status-site
  git log back into a History board.

That works, but the seams show:

- **History has no per-entry messages of its own.** One commit = one message, so
  a push touching Clauffice + Fleet shows the *same* line on both boards' history
  (the `· also …` annotations are a patch over this).
- **Collectors pull; CI already knows.** A CI run has the exact truth (tests,
  coverage, what shipped) at the moment it finishes, but we throw it away and
  re-derive it later by polling GitHub.
- **Every repo re-implements its own reporting.** No single "how a repo reports
  status" contract.

## The idea

Invert it: **each CI run emits one structured event to a central ingest, and the
event stream is the history.**

```
   repo CI run                 events ingest              statusgen
 ┌──────────────┐   POST     ┌──────────────────┐  read  ┌──────────────┐
 │ roost-ci pkg │──event───▶ │ /api/events      │──────▶ │ board.json   │
 │  gates, then │            │  SQLite, keyed   │        │  (live state)│
 │  one event   │            │  by repo         │──────▶ │ history page │
 └──────────────┘            │ /api/events?repo │        │  (the stream)│
                             └──────────────────┘        └──────────────┘
```

The board renders the *latest* state; history is the *stream*, filtered by repo.

## What it unifies

- **It answers the history-storage question as a side effect.** The central store
  *is* the "append-only event log" option we deferred — so we don't decide
  git-derived-vs-event-log in the abstract; building the CI package settles it.
- **Per-repo history + real per-entry messages come for free.** Every event is
  tagged with its repo and carries its own message. The shared-per-push
  limitation dissolves; `· also …` goes away.
- **One reporting contract.** A repo "reports status" by using the CI package —
  not by hand-authoring board.json or hoping a puller notices it.
- **git-derived history becomes a fallback, not the source** — still there for
  hand pushes and pre-event history, but no longer load-bearing.

## We already have the pattern: pulse

`pulse` is exactly this shape for watts: nodes POST telemetry → SQLite →
`/api/history` serves the stream → the page renders current + history. A "roost
status events" ingest is **pulse-for-CI**. Same moving parts (`node:sqlite`
persistence on a dokku storage mount, a public read API, a tiny POST auth key),
a different payload. Build it by cloning pulse's proven bones, not from scratch.

## Sketch of the parts

1. **A reusable CI package** — one composite GitHub Action (or `roost-ci.yml`
   reusable workflow) every repo references. It runs the quality gates (the
   Phoenix `scripts/ci` extraction is already most of this), then POSTs one
   event on completion. Repos stop hand-rolling reporting.
2. **An events ingest** — a small service (pulse's skeleton) with:
   - `POST /api/events` (shared key) — validate + store one event.
   - `GET /api/events?repo=<slug>&limit=N` — the stream for a board's history.
   - `GET /api/state?repo=<slug>` — the latest reduced state for the live board.
3. **statusgen reads it** — a collector (or direct fetch in the shell) turns
   `/api/state` into the board and `/api/events` into the history/detail pages.
   The per-board `<slug>/history/` pages we just built are the natural render
   target — they'd read the event stream instead of git.

## Event schema (starting point)

```json
{
  "repo": "phoenix-electron",
  "sha": "842dfc8",
  "branch": "dev",
  "kind": "ci",                       // ci | deploy | manual | note
  "ts": "2026-07-13T21:42:00Z",       // UTC; renderer localizes
  "message": "quality gates + board seam",
  "metrics": { "tests": 3902, "coverage": 63, "shippedPRs": 3 },
  "status": "success"                 // success | failure | running
}
```

`kind` lets manual `roost status` notes and deploy markers share the stream with
CI events, so history stays one timeline.

## Migration path (incremental, non-breaking)

1. Stand up the ingest (fork pulse), no consumers yet.
2. Add the CI package to **one** repo (Phoenix); watch events land.
3. Point one board's `<slug>/history/` at `/api/events` with git-derived as
   fallback; compare.
4. Roll the CI package to the other repos; retire the pull collectors as each
   repo's events cover their board sections.
5. `roost status` keeps working — it just becomes a `kind:"manual"` event.

Nothing has to move at once; git-derived history stays valid throughout.

## Open questions

- **Ingest home** — its own dokku app (like pulse) vs. a route on pulse itself?
  Separate app is cleaner; pulse-hosted is one less thing to run.
- **State reduction** — does the ingest compute `/api/state` (server-side
  reduce), or does statusgen reduce the event list client-side? Server-side
  keeps the board dumb.
- **Auth** — one shared key like pulse's `NODE_KEY`, or per-repo keys so a
  leaked key only forges one repo's events?
- **Retention** — events are cheap; keep all, or window like pulse's 90-day
  watts history?
- **Backfill** — seed the ingest from the existing 72 git-derived pushes so
  history doesn't start empty.

## Relationship to what exists

- **roost** stays the driver: it'd own the CI package and the ingest's deploy,
  and `roost status` becomes an event emitter.
- **statusgen** stays the library: board schema + renderer unchanged; it gains
  an events-backed collector next to the git-derived one.
- **status-site** stays pure data, but more of its board.json becomes
  ingest-generated rather than hand-authored.
- Extends **#37** (push-based Clauffice/roost board seam) — that seam is the
  first step of exactly this.
