#!/usr/bin/env python3
"""backup-status.py - the state of the backup service, read from anywhere.

The service and the data sit on different machines. The service is the cron
job, `opi-backup.sh`, and the run state under `~/.local/state/opi-backup`, and
it lives on ROOST_BACKUP_SERVICE_HOST. The restic repositories live on
ROOST_BACKUP_REPO_HOST, under ROOST_BACKUP_REPO_DIR. Either host can be the
machine that runs this script, so every read goes direct when the host is this
machine and through ssh when it is not.

An unreachable host gives a `reachable: false` reading, and it never raises.
A backup tool that cannot say "I do not know" invites the reader to read
silence as "fine". The retired `roost backup` failed on 10 of its last 14
nights and nobody saw it, because nothing ever said so.

Config, through ~/.roostrc:
  ROOST_BACKUP_SERVICE_HOST   ssh target that runs the backup (default jimmy@opi.local)
  ROOST_BACKUP_REPO_HOST      ssh target that holds the repositories (default ROOST_STATUS_RUNNER)
  ROOST_BACKUP_REPO_DIR       repository directory on that host (default ~/backupdrive/restic)
  ROOST_BACKUP_PASSWORD_FILE  restic password file on that host (default ~/.config/opi-backup/restic-password)
  ROOST_BACKUP_SERVICE_PASSWORD_FILE  the same secret on the service host (default ~/.config/opi-backup/password)
  ROOST_BACKUP_RESTIC         restic binary on that host (default ~/bin/restic)
  ROOST_BACKUP_SCRIPT         backup script on the service host (default ~/bin/opi-backup.sh)

Usage: backup-status.py [--json] [--run] [--check] [--serve]
"""
import argparse
import datetime
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys

BIN = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BIN)
import roostlib  # noqa: E402

# A run at 03:30 is late once a second one should have happened.
# The amber band gives a single missed night, and red means the service stopped.
FRESH_HOURS = 26
STALE_HOURS = 50

DEFAULTS = {
    "ROOST_BACKUP_SERVICE_HOST": "jimmy@opi.local",
    "ROOST_BACKUP_REPO_DIR": "~/backupdrive/restic",
    "ROOST_BACKUP_PASSWORD_FILE": "~/.config/opi-backup/restic-password",
    # The service host keeps its own copy under its own name, which is the one
    # opi-backup.sh reads. An in-place restore runs there, so it needs this and
    # not the repository host's path.
    "ROOST_BACKUP_SERVICE_PASSWORD_FILE": "~/.config/opi-backup/password",
    "ROOST_BACKUP_RESTIC": "~/bin/restic",
    "ROOST_BACKUP_SCRIPT": "~/bin/opi-backup.sh",
    # The staging directory the backup writes through. A snapshot of it restores
    # nothing useful in place, because the next run wipes the directory.
    "ROOST_BACKUP_STAGING": "/mnt/nvme/backup-staging",
    # The repository directory as the SERVICE host addresses it. An in-place
    # restore runs there, not on the machine holding the drive.
    "ROOST_BACKUP_REPO_URL": ("sftp:jimmyhoughjr@jimmys-mac-mini.local:"
                              "/Users/jimmyhoughjr/backupdrive/restic"),
}


def cfg(key, default=None):
    """One value, in roost's order: ~/.roostrc, then the environment, then a default.

    roostlib.rc reads only the rc file, and this tool must also point at a host
    for one run without editing the rc.
    """
    from_rc = roostlib.read_rc().get(key)
    if from_rc:
        return from_rc
    return os.environ.get(key) or DEFAULTS.get(key, default)


def repo_host():
    """The repositories follow the status runner unless the rc says otherwise."""
    return cfg("ROOST_BACKUP_REPO_HOST") or cfg(
        "ROOST_STATUS_RUNNER", "jimmyhoughjr@jimmys-mac-mini.local")


def is_local(host):
    """True when `host` names the machine we are already on."""
    if not host:
        return True
    name = host.split("@")[-1]
    if name in ("localhost", "127.0.0.1", "::1"):
        return True
    me = socket.gethostname().lower()
    short = me.split(".")[0]
    return name.lower() in (me, short, short + ".local")


def run(host, command, timeout=45):
    """Run one shell command on `host`. Returns (rc, stdout, stderr)."""
    if is_local(host):
        argv = ["bash", "-lc", command]
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", host, command]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout
    except OSError as exc:
        return 127, "", str(exc)


def parse_time(text):
    """Read an ISO timestamp that may carry a timezone and nanoseconds."""
    text = text.strip()
    if not text:
        return None
    # Python 3.9 does not read a `Z` suffix, and it wants at most microseconds.
    text = text.replace("Z", "+00:00")
    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        while tail and tail[0].isdigit():
            digits, tail = digits + tail[0], tail[1:]
        text = head + "." + digits[:6] + tail
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def age_hours(when):
    if when is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (now - when).total_seconds() / 3600.0


def tone_for(hours):
    """`go` while a nightly run is on time, `warn` for one missed night, `bad` after."""
    if hours is None:
        return "unknown"
    if hours <= FRESH_HOURS:
        return "go"
    if hours <= STALE_HOURS:
        return "warn"
    return "bad"


def service_state():
    """Whether the schedule exists, and how the last run ended."""
    host = cfg("ROOST_BACKUP_SERVICE_HOST")
    state = {"host": host, "reachable": False, "scheduled": None,
             "last_success": None, "last_run_ok": None, "log_tail": [], "warnings": []}
    rc, out, _ = run(host, "cat ~/.local/state/opi-backup/last-success 2>/dev/null; "
                           "echo '---'; crontab -l 2>/dev/null | grep -c opi-backup; "
                           "echo '---'; tail -6 ~/.local/state/opi-backup/last-run.log 2>/dev/null; "
                           "echo '---'; cat ~/.local/state/opi-backup/last-warnings 2>/dev/null")
    if rc != 0:
        return state
    state["reachable"] = True
    parts = out.split("---")
    if len(parts) >= 3:
        state["last_success"] = parts[0].strip() or None
        state["scheduled"] = parts[1].strip() not in ("", "0")
        state["log_tail"] = [ln for ln in parts[2].strip().splitlines() if ln.strip()]
    if len(parts) >= 4:
        # Retention or verification did not run. The snapshots are stored, so this
        # is a warning and not a failure, but a repository that never prunes and
        # never verifies is a real problem.
        state["warnings"] = [ln for ln in parts[3].strip().splitlines() if ln.strip()]
    # The script writes this line last, so its absence means the run died partway.
    state["last_run_ok"] = any("opi-backup finished" in ln for ln in state["log_tail"])
    return state


def repo_state():
    """Snapshot counts, freshness, and size for every repository on the repo host."""
    host = repo_host()
    rdir = cfg("ROOST_BACKUP_REPO_DIR")
    restic = cfg("ROOST_BACKUP_RESTIC")
    pwfile = cfg("ROOST_BACKUP_PASSWORD_FILE")
    state = {"host": host, "dir": rdir, "reachable": False, "mounted": False, "repos": []}

    # Reachability and the drive are separate failures, and they need separate answers.
    # An unplugged drive leaves the host answering while the directory has gone, so the
    # marker below separates "the host did not reply" from "the host found nothing".
    # A restic repository always holds a `config` file, which keeps stray files such as
    # RESTORE.md out of the repository list.
    rc, out, _ = run(host, "echo HOST_OK; for d in %s/*/; do "
                           "[ -f \"$d/config\" ] && basename \"$d\"; done" % rdir, timeout=20)
    if "HOST_OK" not in out:
        return state
    state["reachable"] = True
    names = [n.strip() for n in out.splitlines()
             if n.strip() and n.strip() != "HOST_OK" and not n.startswith(".")]
    state["mounted"] = bool(names)

    for name in names:
        repo = "%s/%s" % (rdir, name)
        entry = {"name": name, "snapshots": None, "latest": None,
                 "age_hours": None, "tone": "unknown", "size_bytes": None, "error": None}
        cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s snapshots --no-lock --json 2>/dev/null"
               % (pwfile, restic, repo))
        rc, out, err = run(host, cmd, timeout=90)
        if rc != 0 or not out.strip():
            entry["error"] = (err or "cannot read repository").strip()[:200]
            state["repos"].append(entry)
            continue
        try:
            snaps = json.loads(out)
        except ValueError:
            entry["error"] = "unreadable snapshot list"
            state["repos"].append(entry)
            continue
        entry["snapshots"] = len(snaps)
        if snaps:
            newest = max(snaps, key=lambda s: s.get("time", ""))
            entry["latest"] = newest.get("time")
            hours = age_hours(parse_time(entry["latest"] or ""))
            entry["age_hours"] = round(hours, 1) if hours is not None else None
            entry["tone"] = tone_for(hours)
        cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s stats --no-lock --mode raw-data --json 2>/dev/null"
               % (pwfile, restic, repo))
        rc, out, _ = run(host, cmd, timeout=90)
        if rc == 0 and out.strip():
            try:
                entry["size_bytes"] = json.loads(out.strip().splitlines()[-1]).get("total_size")
            except (ValueError, IndexError):
                pass
        state["repos"].append(entry)
    return state


def collect():
    return {"service": service_state(), "storage": repo_state()}


def human_size(n):
    if n is None:
        return "?"
    step = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if step < 1024 or unit == "T":
            return "%.1f%s" % (step, unit)
        step /= 1024


def human_age(hours):
    if hours is None:
        return "unknown"
    if hours < 1:
        return "%dm ago" % int(hours * 60)
    if hours < 48:
        return "%.1fh ago" % hours
    return "%.1fd ago" % (hours / 24)


MARK = {"go": "✓", "warn": "!", "bad": "✗", "unknown": "?"}


def render(state):
    """The plain-text report behind `roost backup`."""
    out = []
    svc, sto = state["service"], state["storage"]

    out.append("service  %s" % svc["host"])
    if not svc["reachable"]:
        out.append("  ? unreachable, so the schedule and the last run are unknown")
    else:
        out.append("  %s schedule %s" % ("✓" if svc["scheduled"] else "✗",
                                         "installed" if svc["scheduled"] else "MISSING"))
        out.append("  %s last run %s" % ("✓" if svc["last_run_ok"] else "✗",
                                         "finished" if svc["last_run_ok"] else "DID NOT FINISH"))
        out.append("  · last success %s" % (svc["last_success"] or "never recorded"))
        for w in svc.get("warnings", []):
            out.append("  ! %s" % w)

    out.append("")
    out.append("storage  %s:%s" % (sto["host"], sto["dir"]))
    if not sto["reachable"]:
        out.append("  ? unreachable, so the repositories cannot be read")
    elif not sto["mounted"]:
        out.append("  ✗ no repositories found, so the drive is probably not mounted")
    else:
        for r in sto["repos"]:
            if r["error"]:
                out.append("  ? %-14s %s" % (r["name"], r["error"]))
                continue
            out.append("  %s %-14s %3d snapshot%s %7s  %s"
                       % (MARK[r["tone"]], r["name"], r["snapshots"],
                          "s" if r["snapshots"] != 1 else " ",
                          human_size(r["size_bytes"]), human_age(r["age_hours"])))
    return "\n".join(out)


def worst_tone(state):
    """The tone a caller should show for the service as a whole."""
    svc, sto = state["service"], state["storage"]
    if not svc["reachable"] and not sto["reachable"]:
        return "unknown"
    if svc["reachable"] and (not svc["scheduled"] or not svc["last_run_ok"]):
        return "bad"
    if sto["reachable"] and not sto["mounted"]:
        return "bad"
    if svc["reachable"] and svc.get("warnings"):
        return "warn"
    tones = [r["tone"] for r in sto["repos"]] or ["unknown"]
    for level in ("bad", "warn", "unknown", "go"):
        if level in tones:
            return level
    return "unknown"


def repo_path(name):
    """The repository as the machine holding the drive addresses it."""
    return "%s/%s" % (cfg("ROOST_BACKUP_REPO_DIR"), name)


def restic_on_repo_host(name, args, timeout=120):
    """Run restic against `name` on the host that holds the drive."""
    cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s %s"
           % (cfg("ROOST_BACKUP_PASSWORD_FILE"), cfg("ROOST_BACKUP_RESTIC"),
              repo_path(name), args))
    return run(repo_host(), cmd, timeout=timeout)


def snapshots(name):
    """Every snapshot in one repository, newest last."""
    rc, out, err = restic_on_repo_host(name, "snapshots --no-lock --json 2>/dev/null")
    if rc != 0 or not out.strip():
        return None, (err or "cannot read repository").strip()[:200]
    try:
        snaps = json.loads(out)
    except ValueError:
        return None, "unreadable snapshot list"
    return [{"id": s.get("short_id"), "time": s.get("time"),
             "tags": s.get("tags") or [], "paths": s.get("paths") or [],
             "size": (s.get("summary") or {}).get("total_bytes_processed")}
            for s in snaps], None


def list_tree(name, snap, path):
    """One level of a snapshot's tree. `path` empty means the roots."""
    arg = "ls --no-lock --json %s" % snap
    if path:
        arg += " %s" % shlex.quote(path)
    rc, out, err = restic_on_repo_host(name, arg + " 2>/dev/null", timeout=180)
    if rc != 0:
        return None, (err or "cannot list the snapshot").strip()[:200]
    entries, want = [], (path.rstrip("/") if path else "")
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("struct_type") != "node" and "path" not in j:
            continue
        p = j.get("path") or ""
        # `restic ls` walks the whole subtree, and the browser wants one level.
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if parent != want:
            continue
        entries.append({"path": p, "name": p.rsplit("/", 1)[-1],
                        "type": j.get("type"), "size": j.get("size")})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"]))
    return entries, None


def in_place_ok(name):
    """Whether restoring this repository in place means anything.

    The critical repository holds the staging directory, and the next backup run
    wipes that directory. Restoring it there would report success and achieve
    nothing, so the answer is no, and the caller is sent to the runbook.
    """
    staging = cfg("ROOST_BACKUP_STAGING").rstrip("/")
    snaps, err = snapshots(name)
    if err or not snaps:
        return False, "cannot read the repository"
    paths = snaps[-1]["paths"]
    if all(p.rstrip("/") == staging or p.startswith(staging + "/") for p in paths):
        return False, ("this repository holds %s, which every run wipes. "
                       "Extract the tar and follow docs/opi-backup-restore.md." % staging)
    return True, ""


def extract(name, snap, path, target):
    """Pull one path out of a snapshot onto THIS machine, into a new directory.

    The target must not exist. Nothing here may overwrite anything, and that is
    what separates this from the in-place restore below.
    """
    target = os.path.abspath(os.path.expanduser(target))
    if os.path.exists(target):
        return 1, "refusing to extract: %s already exists" % target
    host = repo_host()

    def restic_restore(dest):
        return ("RESTIC_PASSWORD_FILE=%s %s -r %s restore %s --include %s --target %s --verify"
                % (cfg("ROOST_BACKUP_PASSWORD_FILE"), cfg("ROOST_BACKUP_RESTIC"),
                   repo_path(name), shlex.quote(snap), shlex.quote(path), dest))

    os.makedirs(target)
    try:
        if is_local(host):
            rc, out, err = run(host, restic_restore(shlex.quote(target)), timeout=3600)
            if rc != 0:
                raise RuntimeError((err or out).strip()[:400])
            return 0, "extracted %s into %s" % (path, target)

        # The repository is on another machine. restic writes there, and the
        # result streams back as a tar. restic reports progress on stdout, so
        # that goes to stderr here: anything else corrupts the tar stream.
        remote = ('set -e; T=$(mktemp -d /tmp/roost-extract.XXXXXX); '
                  + restic_restore('"$T"') + ' >&2; '
                  'tar -cf - -C "$T" . ; rm -rf "$T"')
        argv = ["ssh", "-o", "BatchMode=yes", host, remote]
        tarball = os.path.join(target, ".incoming.tar")
        with open(tarball, "wb") as fh:
            p = subprocess.run(argv, stdout=fh, stderr=subprocess.PIPE, timeout=3600)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:400])
        rc2, out2, err2 = run("", "tar -xf %s -C %s" % (shlex.quote(tarball), shlex.quote(target)))
        if rc2 != 0:
            raise RuntimeError((err2 or out2).strip()[:400])
        os.remove(tarball)
        return 0, "extracted %s into %s" % (path, target)
    except Exception as exc:                     # noqa: BLE001 - report any failure
        # A half-written target would block the retry, because we refuse to
        # extract onto anything that already exists.
        shutil.rmtree(target, ignore_errors=True)
        return 1, "extract failed: %s" % exc


def restore_in_place(name, snap, path, confirm=None):
    """Restore a path back over its original location on the service host.

    This is the one action here that cannot be undone, so it runs as a dry run
    unless `confirm` repeats the path exactly. `--delete` is never passed: a
    restore may put files back, and it may not remove any.
    """
    ok, why = in_place_ok(name)
    if not ok:
        return 1, "in-place restore is not available for %s: %s" % (name, why)
    dry = confirm != path
    # restic refuses --dry-run together with --verify, so the preview lists what
    # it would write and the real run reads the result back to check it.
    mode = "--dry-run --verbose" if dry else "--verify"
    cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s/%s restore %s --include %s --target / %s"
           % (cfg("ROOST_BACKUP_SERVICE_PASSWORD_FILE"), cfg("ROOST_BACKUP_RESTIC"),
              cfg("ROOST_BACKUP_REPO_URL"), name, shlex.quote(snap),
              shlex.quote(path), mode))
    host = cfg("ROOST_BACKUP_SERVICE_HOST")
    rc, out, err = run(host, cmd, timeout=7200)
    said = (out or err).strip()
    if dry:
        head = ("DRY RUN on %s. Nothing was written.\n"
                "To do it, repeat the path with --confirm.\n" % host)
        return rc, head + said[-2000:]
    return rc, ("restored %s onto %s\n" % (path, host)) + said[-2000:]


LOCAL_BINDS = ("127.0.0.1", "localhost", "::1")


def serve(bind, port, token):
    """Present the backup service as a local web page, the way hatchery serve does.

    The server is started by a person at a shell and binds the loopback address,
    so there is nobody else to authenticate. That is the whole reason this can
    offer restore at all: the page is not reachable from the network.

    Binding anywhere else demands a token, because the same page then reaches the
    repositories from another machine.
    """
    import http.server
    import urllib.parse

    if bind not in LOCAL_BINDS and not token:
        return 2, ("refusing to bind %s without --token. The page can extract and "
                   "restore, so off-loopback it needs a secret." % bind)

    page = os.path.join(BIN, "backup-ui.html")
    if not os.path.exists(page):
        return 2, "missing %s" % page

    def body(handler):
        length = int(handler.headers.get("content-length") or 0)
        try:
            return json.loads(handler.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    class Handler(http.server.BaseHTTPRequestHandler):
        # The default logger writes a line per request to stderr, which buries
        # the one line a person started this to read.
        def log_message(self, fmt, *args):
            pass

        def allowed(self):
            return not token or self.headers.get("x-roost-token") == token

        def send(self, code, payload, kind="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-type", kind)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):                                    # noqa: N802
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            one = lambda k, d="": (q.get(k) or [d])[0]       # noqa: E731
            if url.path == "/":
                with open(page, "rb") as fh:
                    return self.send(200, fh.read(), "text/html; charset=utf-8")
            if not self.allowed():
                return self.send(401, {"error": "bad token"})
            if url.path == "/api/state":
                state = collect()
                state["tone"] = worst_tone(state)
                return self.send(200, state)
            if url.path == "/api/snapshots":
                rows, err = snapshots(one("repo"))
                return self.send(200 if not err else 502, {"rows": rows or [], "error": err})
            if url.path == "/api/ls":
                rows, err = list_tree(one("repo"), one("snap", "latest"), one("path"))
                return self.send(200 if not err else 502, {"rows": rows or [], "error": err})
            if url.path == "/api/inplace":
                ok, why = in_place_ok(one("repo"))
                return self.send(200, {"ok": ok, "why": why})
            return self.send(404, {"error": "no such path"})

        def do_POST(self):                                   # noqa: N802
            url = urllib.parse.urlparse(self.path)
            if not self.allowed():
                return self.send(401, {"error": "bad token"})
            j = body(self)
            if url.path == "/api/extract":
                rc, msg = extract(j.get("repo", ""), j.get("snap", "latest"),
                                  j.get("path", ""), j.get("to", ""))
                return self.send(200, {"rc": rc, "message": msg})
            if url.path == "/api/restore":
                rc, msg = restore_in_place(j.get("repo", ""), j.get("snap", "latest"),
                                           j.get("path", ""), j.get("confirm") or None)
                return self.send(200, {"rc": rc, "message": msg})
            return self.send(404, {"error": "no such path"})

    # Threading, because an extract holds its request open for as long as the
    # copy takes and the page must stay usable meanwhile.
    srv = http.server.ThreadingHTTPServer((bind, port), Handler)
    where = "http://%s:%d/" % ("localhost" if bind in LOCAL_BINDS else bind, port)
    print("backup ui on %s" % where)
    if bind not in LOCAL_BINDS:
        print("bound off-loopback, so every request needs the token")
    print("ctrl+c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0, ""


def start_run():
    """Start a backup on the service host and return at once.

    The run takes minutes, so the caller gets the start and reads the result later
    through the ordinary status path. `setsid` keeps it alive after the ssh session
    closes, and the script's own flock refuses a second run.
    """
    host = cfg("ROOST_BACKUP_SERVICE_HOST")
    script = cfg("ROOST_BACKUP_SCRIPT")
    rc, out, err = run(host, "setsid nohup %s > /tmp/opi-backup-boot.log 2>&1 "
                             "< /dev/null & echo started" % script, timeout=20)
    if rc != 0:
        return 1, "could not start the backup on %s: %s" % (host, (err or out).strip()[:200])
    return 0, "backup started on %s. The reading updates when it finishes." % host


def check_repos():
    """Run `restic check` against every repository, and report each verdict."""
    host = repo_host()
    rdir, restic, pwfile = (cfg("ROOST_BACKUP_REPO_DIR"), cfg("ROOST_BACKUP_RESTIC"),
                            cfg("ROOST_BACKUP_PASSWORD_FILE"))
    state = repo_state()
    if not state["repos"]:
        return 1, "no repositories to check on %s" % host
    lines, worst = [], 0
    for r in state["repos"]:
        cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s/%s check"
               % (pwfile, restic, rdir, r["name"]))
        rc, out, err = run(host, cmd, timeout=900)
        ok = rc == 0 and "no errors were found" in out
        worst = worst or (0 if ok else 1)
        lines.append("%s %-14s %s" % ("✓" if ok else "✗", r["name"],
                                      "no errors were found" if ok
                                      else (err or out).strip().splitlines()[-1][:120]
                                      if (err or out).strip() else "check failed"))
    return worst, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="report the state of the backup service")
    ap.add_argument("--json", action="store_true", help="emit the reading as JSON")
    ap.add_argument("--run", action="store_true", help="start a backup on the service host")
    ap.add_argument("--check", action="store_true", help="run restic check on every repository")
    ap.add_argument("--snapshots", metavar="REPO", help="list the snapshots in one repository")
    ap.add_argument("--ls", metavar="REPO", help="list one level of a snapshot's tree")
    ap.add_argument("--extract", metavar="REPO", help="pull a path out of a snapshot to --to")
    ap.add_argument("--restore", metavar="REPO", help="restore a path onto the service host")
    ap.add_argument("--snapshot", default="latest", help="snapshot id (default: latest)")
    ap.add_argument("--path", default="", help="path inside the snapshot")
    ap.add_argument("--to", help="local directory for --extract; it must not exist")
    ap.add_argument("--serve", action="store_true",
                    help="present the backup service as a local web page")
    ap.add_argument("--bind", default="127.0.0.1", help="address for --serve (default loopback)")
    ap.add_argument("--port", type=int, default=7979, help="port for --serve (default 7979)")
    ap.add_argument("--token", help="secret --serve demands when it binds off-loopback")
    ap.add_argument("--confirm", metavar="PATH",
                    help="repeat --path exactly to perform an in-place restore. "
                         "Without it, --restore only reports what it would write.")
    args = ap.parse_args()

    if args.serve:
        rc, msg = serve(args.bind, args.port, args.token)
        if msg:
            print(msg)
        return rc

    if args.snapshots:
        snaps, err = snapshots(args.snapshots)
        if err:
            print(err)
            return 1
        if args.json:
            print(json.dumps(snaps, indent=2, sort_keys=True))
        else:
            for s_ in snaps:
                print("%-10s %s  %-10s %s" % (s_["id"], (s_["time"] or "")[:19],
                                              ",".join(s_["tags"]), " ".join(s_["paths"])))
        return 0

    if args.ls:
        entries, err = list_tree(args.ls, args.snapshot, args.path)
        if err:
            print(err)
            return 1
        if args.json:
            print(json.dumps(entries, indent=2, sort_keys=True))
        else:
            for e in entries:
                print("%-4s %10s  %s" % (e["type"], human_size(e["size"]), e["path"]))
        return 0

    if args.extract:
        if not args.path or not args.to:
            print("--extract needs --path and --to")
            return 2
        rc, msg = extract(args.extract, args.snapshot, args.path, args.to)
        print(msg)
        return rc

    if args.restore:
        if not args.path:
            print("--restore needs --path")
            return 2
        rc, msg = restore_in_place(args.restore, args.snapshot, args.path, args.confirm)
        print(msg)
        return rc

    if args.run:
        rc, msg = start_run()
        print(msg)
        return rc
    if args.check:
        rc, msg = check_repos()
        print(msg)
        return rc

    state = collect()
    state["tone"] = worst_tone(state)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
    else:
        print(render(state))
    # A red reading is worth a non-zero exit, so a caller can gate on it.
    return 1 if state["tone"] == "bad" else 0


if __name__ == "__main__":
    sys.exit(main())
