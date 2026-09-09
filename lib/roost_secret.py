#!/usr/bin/env python3
"""roost_secret - ONE reader for the secrets roost's Python reporters send.

This is the Python mirror of lib/roost-secret.sh, down to the cache file both languages share, so a
host reads its sealed document once for every tool on it. A host used to hold one file per secret,
placed there by a person and rotated by hand on every box. Now it holds one secret of its own, its
vault app key, and fetches the rest from vault.

The resolution order is three arms:
  1. the vault document for ROOST_VAULT_APP, fetched with ROOST_VAULT_APP_KEY
  2. the legacy file for that name, so a host whose rc has no vault keys yet keeps reporting
  3. nothing, which is an empty string here and the source "none"

Config, from the environment first and then ~/.roostrc, read through bin/roostlib.py:
  ROOST_VAULT_URL      vault base URL (default: https://vault.jimmyhoughjr.net)
  ROOST_VAULT_APP      the app whose document holds this host's secrets, roost-<node name>
  ROOST_VAULT_APP_KEY  the app key that opens it, the one secret the host still holds

The document is cached for 300 seconds at mode 600, because node-report runs every 30 seconds and a
read per run is ten vault calls a minute from every host.

A consumer in bin/ puts this directory on sys.path itself, the way each one already does for
roostlib.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ~/.roostrc has one reader per language, and this module is not it. bin/roostlib.py is.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import roostlib  # noqa: E402

# The cache window, in seconds.
TTL = 300

VAULT_URL_DEFAULT = "https://vault.jimmyhoughjr.net"

# The file that held each name before vault did.
LEGACY_FILES = {
    "NODE_KEY": "~/.roost_node_key",
    "CI_KEY": "~/.roost_ci_key",
}


def roost_secret(name):
    """The value of NAME, or an empty string when no arm answers."""
    value = _document().get(name, "")
    if isinstance(value, str) and value:
        return value
    return _from_file(name)


def roost_secret_source(name):
    """Which arm answers for NAME: "vault", "file" or "none"."""
    value = _document().get(name, "")
    if isinstance(value, str) and value:
        return "vault"
    if _from_file(name):
        return "file"
    return "none"


def _config(name, default=""):
    """The environment first, then ~/.roostrc, then `default`."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return roostlib.read_rc().get(name, "").strip() or default


def _cache_path(app):
    """The cache file for one app's document, the same path lib/roost-secret.sh computes."""
    # TMPDIR can be shared, so the account owns its own copy, and an app name that is not a filename cannot leave it.
    safe = "".join(c if (c.isalnum() and c.isascii()) or c in "._-" else "-" for c in app)
    tmp = (os.environ.get("TMPDIR") or "/tmp").rstrip("/")
    return os.path.join(tmp, f"roost-secrets-{os.getuid()}-{safe}.json")


def _document():
    """The app's sealed document, from the cache or from vault. An empty dict means no vault arm."""
    app = _config("ROOST_VAULT_APP")
    key = _config("ROOST_VAULT_APP_KEY")
    if not app or not key:
        return {}

    cache = _cache_path(app)
    try:
        if 0 <= time.time() - os.stat(cache).st_mtime < TTL:
            with open(cache) as fh:
                cached = json.load(fh)
            if isinstance(cached, dict):
                return cached
    except (OSError, ValueError):
        pass

    url = _config("ROOST_VAULT_URL", VAULT_URL_DEFAULT).rstrip("/")
    request = urllib.request.Request(f"{url}/api/apps/{app}/secrets")
    request.add_header("authorization", f"Bearer {key}")
    # Cloudflare 403s the default Python-urllib agent outright - see tapo-poll.py. Identify honestly instead.
    request.add_header("user-agent", "roost-secret/1 (+https://github.com/jhoughjr/roost)")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            document = json.loads(response.read().decode())
    except Exception:
        # A vault that does not answer is not an error here. The next arm is the legacy file.
        return {}
    if not isinstance(document, dict):
        return {}

    _write_cache(cache, document)
    return document


def _write_cache(path, document):
    """Replace the cache in one move, and never leave it readable by another account.

    A cache that cannot be written costs one vault call next run and nothing else.
    """
    tmp = f"{path}.{os.getpid()}"
    try:
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as fh:
            json.dump(document, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _from_file(name):
    """The value the legacy file for NAME holds, or an empty string."""
    path = LEGACY_FILES.get(name)
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path)) as fh:
            return fh.read().strip()
    except OSError:
        return ""
