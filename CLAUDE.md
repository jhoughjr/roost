# Claude notes for roost

Pure bash + stdlib-Python toolbelt — no build system, no package manifest,
no dependencies beyond `gh`/`curl`/`ssh`. Everything executable lives in
`bin/`; `bin/roost` is a single bash `case` dispatcher (there is no
git-style `roost-$cmd` plugin mechanism, despite old plans).

## Where truth lives

- **Script headers are the authoritative per-tool docs.** Every script in
  `bin/` opens with a usage/config comment block; the markdown docs
  summarize them and can lag. When they disagree, the header (and the
  code) wins — then fix the doc.
- `roost help` prints lines 4–18 of `bin/roost` verbatim (via `sed`). If
  you add a subcommand, add its header line **and** keep the `sed -n`
  range in the `help` case covering it.
- `~/.roostrc` is plain `KEY=VALUE`; each Python tool re-parses it
  independently. Keep `roostrc.example` in sync with what code actually
  reads — grep `ROOST_` across `bin/` *and* `statusgen/bin/` (several
  example keys are consumed by statusgen collectors, not roost).
- Secrets are never in `.roostrc`: `~/.cf_api_token`, `~/.roost_node_key`,
  `~/.roost_ci_key`, `~/.eia_api_key` (all chmod 600).

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
