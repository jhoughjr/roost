# shellcheck shell=bash
# roost-secret.sh - sourced, never executed: ONE reader for the secrets roost's reporters send.
#
# A host used to hold one file per secret, placed there by a person and rotated by hand on every box.
# Now it holds one secret of its own, its vault app key, and fetches the rest from vault's sealed
# per-app document. lib/roost_secret.py is the Python mirror, down to the cache file both share.
#
# `roost_secret NAME` prints the value of NAME and nothing else, and exits 1 when no arm answers.
# `roost_secret_source NAME` prints `vault`, `file` or `none`, which is what `roost secrets` reports.
#
# The resolution order is three arms:
#   1. the vault document for ROOST_VAULT_APP, fetched with ROOST_VAULT_APP_KEY
#   2. the legacy file for that name, so a host whose rc has no vault keys yet keeps reporting
#   3. nothing, and exit 1
#
# Config, from the environment first and then ~/.roostrc:
#   ROOST_VAULT_URL      vault base URL (default: https://vault.jimmyhoughjr.net)
#   ROOST_VAULT_APP      the app whose document holds this host's secrets, roost-<node name>
#   ROOST_VAULT_APP_KEY  the app key that opens it, the one secret the host still holds
#
# The rc is read key by key here and never sourced, matching runner-watchdog.sh. dokku-reconcile.sh
# decides what gets restarted and deliberately refuses to source the rc, and a helper that sourced it
# would put that decision back in the rc's hands. A script that did source the rc already carries
# these three keys in its environment, so the rc still wins there.
#
# The app key travels in a curl header file, so it never reaches a command line. The document is
# cached for 300 seconds under TMPDIR at mode 600: node-report runs every 30 seconds, and a read per
# run is ten vault calls a minute from every host.

# The cache window, in seconds.
ROOST_SECRET_TTL=300

# The base URL when neither the environment nor the rc names one.
ROOST_SECRET_VAULT_URL_DEFAULT="https://vault.jimmyhoughjr.net"

_roost_secret_rc() { # _roost_secret_rc NAME [DEFAULT] - the environment first, then ~/.roostrc, then DEFAULT.
  local name="$1" fallback="${2:-}" value
  value="${!name:-}"
  if [ -z "$value" ]; then
    value="$(grep "^$name=" "$HOME/.roostrc" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^"//' | sed 's/"$//')" || value=""
  fi
  [ -n "$value" ] || value="$fallback"
  printf '%s' "$value"
}

_roost_secret_mtime() { # _roost_secret_mtime FILE - the epoch second of the last write, in both stat dialects.
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

_roost_secret_tmpfile() { # _roost_secret_tmpfile PREFIX - a private file in TMPDIR, mode 600 before anything is written to it.
  local dir path
  dir="${TMPDIR:-/tmp}"
  path="$(mktemp "${dir%/}/$1.XXXXXX")" || return 1
  chmod 600 "$path"
  printf '%s' "$path"
}

_roost_secret_cache_path() { # _roost_secret_cache_path APP - the cache file for one app's document.
  local dir safe
  dir="${TMPDIR:-/tmp}"
  # TMPDIR can be shared, so the account owns its own copy, and an app name that is not a filename cannot leave it.
  safe="$(printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-')"
  printf '%s/roost-secrets-%s-%s.json' "${dir%/}" "$(id -u)" "$safe"
}

_roost_secret_is_document() { # _roost_secret_is_document TEXT - true when TEXT parses as a JSON object.
  [ -n "$1" ] || return 1
  printf '%s' "$1" | python3 -c 'import json, sys; sys.exit(0 if isinstance(json.load(sys.stdin), dict) else 1)' 2>/dev/null
}

_roost_secret_document() { # _roost_secret_document - the app's sealed document as JSON, from the cache or from vault.
  local app key url cache mtime age doc hdr tmp
  app="$(_roost_secret_rc ROOST_VAULT_APP)"
  key="$(_roost_secret_rc ROOST_VAULT_APP_KEY)"
  if [ -z "$app" ] || [ -z "$key" ]; then
    return 1
  fi

  cache="$(_roost_secret_cache_path "$app")"
  if [ -f "$cache" ]; then
    mtime="$(_roost_secret_mtime "$cache")" || mtime=0
    [ -n "$mtime" ] || mtime=0
    age=$(( $(date +%s) - mtime ))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$ROOST_SECRET_TTL" ]; then
      cat "$cache"
      return 0
    fi
  fi

  url="$(_roost_secret_rc ROOST_VAULT_URL "$ROOST_SECRET_VAULT_URL_DEFAULT")"
  hdr="$(_roost_secret_tmpfile roost-secret-hdr)" || return 1
  printf 'Authorization: Bearer %s\n' "$key" > "$hdr"
  doc="$(curl -sf -m 10 -H "@$hdr" "${url%/}/api/apps/$app/secrets" 2>/dev/null)" || doc=""
  rm -f "$hdr"

  # A vault that answers with anything but an object is a vault that did not answer.
  _roost_secret_is_document "$doc" || return 1

  # The cache is replaced in one move, so a reader never sees a half-written document.
  # A cache that cannot be written costs one vault call next run and nothing else.
  if tmp="$(_roost_secret_tmpfile roost-secrets)"; then
    printf '%s' "$doc" > "$tmp"
    mv -f "$tmp" "$cache" 2>/dev/null || rm -f "$tmp"
  fi
  printf '%s' "$doc"
}

_roost_secret_from_vault() { # _roost_secret_from_vault NAME - the value NAME holds in the sealed document.
  local doc
  doc="$(_roost_secret_document)" || return 1
  printf '%s' "$doc" | python3 -c '
import json, sys
value = json.load(sys.stdin).get(sys.argv[1], "")
if not isinstance(value, str) or not value:
    sys.exit(1)
sys.stdout.write(value)
' "$1" 2>/dev/null
}

_roost_secret_legacy_file() { # _roost_secret_legacy_file NAME - the file that held NAME before vault did.
  case "$1" in
    NODE_KEY) printf '%s' "$HOME/.roost_node_key" ;;
    CI_KEY)   printf '%s' "$HOME/.roost_ci_key" ;;
    *)        return 1 ;;
  esac
}

_roost_secret_from_file() { # _roost_secret_from_file NAME - the value the legacy file for NAME holds.
  local path value
  path="$(_roost_secret_legacy_file "$1")" || return 1
  [ -f "$path" ] || return 1
  value="$(tr -d '\r\n' < "$path")" || return 1
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

roost_secret() { # roost_secret NAME - the value of NAME on stdout, and exit 1 when no arm answers.
  local value
  if value="$(_roost_secret_from_vault "$1")" && [ -n "$value" ]; then
    printf '%s' "$value"
    return 0
  fi
  if value="$(_roost_secret_from_file "$1")" && [ -n "$value" ]; then
    printf '%s' "$value"
    return 0
  fi
  return 1
}

roost_secret_source() { # roost_secret_source NAME - which arm answers for NAME: vault, file or none.
  local value
  if value="$(_roost_secret_from_vault "$1")" && [ -n "$value" ]; then
    printf 'vault\n'
    return 0
  fi
  if value="$(_roost_secret_from_file "$1")" && [ -n "$value" ]; then
    printf 'file\n'
    return 0
  fi
  printf 'none\n'
}
