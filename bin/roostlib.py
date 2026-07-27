#!/usr/bin/env python3
"""roostlib — shared ~/.roostrc access for roost's Python tools.

Every tool used to carry its own copy of this loop, each with slightly
different semantics (quote stripping, $VAR expansion, defaults). One
reader, one behavior: shell-style KEY=VALUE lines, # comments skipped,
values $VAR-expanded and stripped of surrounding double quotes. No
caching — the ui's config tab re-reads the file to pick up edits.

The personal fallbacks (dokku host, domain, pulse URL) live HERE and only
here on the Python side, so the portability work (roost#10) has one place
to look.

Executed scripts in bin/ can `import roostlib` directly (a script's own
directory is on sys.path); anything loading them by file path — the tests
do — must put bin/ on sys.path first, which each consumer does itself.
"""
import os

RC_PATH = os.path.expanduser("~/.roostrc")

# Fallbacks when ~/.roostrc doesn't say. roost#10 wants these gone entirely;
# until then they are at least in one file instead of five.
DEFAULTS = {
    "ROOST_DOKKU_HOST": "dokku@192.168.0.103",
    "ROOST_DOMAIN": "jimmyhoughjr.net",
    "ROOST_PULSE_URL": "https://pulse.jimmyhoughjr.net",
}


def read_rc(path=None):
    """Dict of every KEY=VALUE in ~/.roostrc. No defaults applied."""
    cfg = {}
    try:
        with open(path or RC_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = os.path.expandvars(v.strip().strip('"'))
    except OSError:
        pass
    return cfg


def rc(key, default="", path=None):
    """One value: ~/.roostrc first, then DEFAULTS, then `default`."""
    return read_rc(path).get(key, DEFAULTS.get(key, default))
