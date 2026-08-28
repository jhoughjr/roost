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
  ROOST_BACKUP_RESTIC         restic binary on that host (default ~/bin/restic)
  ROOST_BACKUP_SCRIPT         backup script on the service host (default ~/bin/opi-backup.sh)

Usage: backup-status.py [--json] [--run] [--check]
"""
import argparse
import datetime
import json
import os
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
    "ROOST_BACKUP_RESTIC": "~/bin/restic",
    "ROOST_BACKUP_SCRIPT": "~/bin/opi-backup.sh",
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
             "last_success": None, "last_run_ok": None, "log_tail": []}
    rc, out, _ = run(host, "cat ~/.local/state/opi-backup/last-success 2>/dev/null; "
                           "echo '---'; crontab -l 2>/dev/null | grep -c opi-backup; "
                           "echo '---'; tail -6 ~/.local/state/opi-backup/last-run.log 2>/dev/null")
    if rc != 0:
        return state
    state["reachable"] = True
    parts = out.split("---")
    if len(parts) >= 3:
        state["last_success"] = parts[0].strip() or None
        state["scheduled"] = parts[1].strip() not in ("", "0")
        state["log_tail"] = [ln for ln in parts[2].strip().splitlines() if ln.strip()]
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
        cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s snapshots --json 2>/dev/null"
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
        cmd = ("RESTIC_PASSWORD_FILE=%s %s -r %s stats --mode raw-data --json 2>/dev/null"
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
    tones = [r["tone"] for r in sto["repos"]] or ["unknown"]
    for level in ("bad", "warn", "unknown", "go"):
        if level in tones:
            return level
    return "unknown"


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
    args = ap.parse_args()

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
