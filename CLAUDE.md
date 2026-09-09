# Claude notes for roost

Pure bash + stdlib-Python toolbelt — no build system, no package manifest,
no dependencies beyond `gh`/`curl`/`ssh`. Everything executable lives in
`bin/`; `bin/roost` is a single bash `case` dispatcher whose catch-all
execs `roost-$cmd` — looked up on PATH, then in `$ROOST_STATUSGEN/bin`
— so plugins are git-style, while every builtin still wins over a
same-named plugin.

## Where truth lives

- **Script headers are the authoritative per-tool docs.** Every script in
  `bin/` opens with a usage/config comment block; the markdown docs
  summarize them and can lag. When they disagree, the header (and the
  code) wins — then fix the doc.
- `roost help` prints lines 4–22 of `bin/roost` verbatim (via `sed`). If
  you add a subcommand, add its header line **and** keep the `sed -n`
  range in the `help` case covering it.
- `~/.roostrc` is plain `KEY=VALUE`, read through **one** reader per
  language: `bin/roost-env.sh` (sourced by every bash entrypoint) and
  `bin/roostlib.py` (imported by every Python tool). Both also hold the
  fallback defaults — change a default in those two files, nowhere else.
  Precedence is rc → environment → default. Keep `roostrc.example` in
  sync with what code actually reads — grep `ROOST_` across `bin/` *and* `statusgen/bin/` (several
  example keys are consumed by statusgen collectors, not roost).
- One secret lives in `.roostrc`: `ROOST_VAULT_APP_KEY`, the app key that
  opens this host's sealed vault document. It is the only secret a host
  holds. The pulse `NODE_KEY` and the ci-live `CI_KEY` come out of that
  document through `lib/roost-secret.sh` and `lib/roost_secret.py`, and
  `~/.roost_node_key` and `~/.roost_ci_key` are the fallback until
  `roost secrets` says `vault` on that host. Delete a key file only after
  it does. See the README, "Secrets from vault".
- Every other secret is still its own chmod-600 file, never in `.roostrc`:
  `~/.cf_api_token`, `~/.eia_api_key`, `~/.ha_token`, `~/.tapo_pass`.

## Three-repo contract

roost is the **driver**; [statusgen](https://github.com/jhoughjr/statusgen)
is the **library** (schema, renderer, validator, collectors);
status-site is **pure data** (board.json + shells). The authoritative
contract is `statusgen/INTERFACES.md` — in that repo, not here.

## Behaviors that surprise

- `roost status` (`bin/status.sh`) **self-updates this clone** (`git pull
  --ff-only` + re-exec, guarded by `ROOST_SELF_UPDATED`) before
  collecting, then force-pushes the status site to dokku — dokku is a
  deploy *sink*; the GitHub mirror is canonical.
- Because dokku is a sink, the site clone's start-of-run freshness pull
  targets `origin`, not dokku (dokku is only a fallback if GitHub is
  unreachable) — and if the pre-push fetch of `origin` fails, the run
  aborts rather than force-pushing dokku from an unverified base. Both
  guard the same failure: regenerating from a stale local checkout and
  steamrolling another writer's push (bit the mini twice, 2026-07-26 and
  2026-07-29 — see `tests/test_status_publish.py`).
- `TODO.md` items (`- item -- detail`) render publicly on the Fleet
  board — don't park scratch notes there.
- `roost ui`'s console whitelists commands (`PASSTHROUGH` in
  `bin/roost-ui.py`); `prune`, `kick`, and `ui` are deliberately absent.
  A test enforces `CMD_DESC` ↔ `ALL_CMDS` sync.
- `new-app.sh` has a fourth template `--board` and a `--dir` flag.

## Tests & CI

```sh
python3 -m unittest discover -s tests   # from repo root
bash -n bin/*.sh bin/roost              # what CI's lint job runs
```

CI (`.github/workflows/check.yml`) runs on the self-hosted mini runner:
`bash -n`, shellcheck (advisory), `py_compile`, unittest. If CI hangs
with no runner pickup, check the runner is online before debugging.
