#!/usr/bin/env python3
"""box-watch: a box watches its peers, answers their probes with its own view, and says when one goes down.

Every voter runs this. There is no coordinator: each box probes every other voter by name over the tailnet, keeps
its own view on its own disk, and works the majority out itself. A box is down when a majority of the other voters
cannot reach it. The lowest-named voter that sees the fault sends one alert, by ntfy and then Home Assistant, so
the alert never rides on the box that failed. The opi also speaks over the mesh radio when it can reach nobody,
since that is the one channel that does not share the network's fate. Pulse is sent each view and only reads.
Ruled on 2026-09-30, house#51 and #52.

What a box answers to a probe is its whole view document, its peers map included, because a watcher needs the
other voters' views of a third box to count a majority without asking pulse.

Settings, from the environment, which the job's declaration carries:
    BOX_WATCH_NAME       this voter's name, such as opi
    BOX_WATCH_PEERS      the other voters, as name=host:port, comma separated
    BOX_WATCH_PORT       the port this box answers on                 (default 9211)
    BOX_WATCH_INTERVAL   seconds between probes                        (default 30)
    BOX_WATCH_STACKS     the declared stacks this box checks itself, comma separated
    BOX_WATCH_PULSE      where the view is posted                      (default https://pulse.jimmyhoughjr.net)
    BOX_WATCH_MESH       a command that says one line over the mesh, on the box that has the radio
    BOX_WATCH_MESH_ENV   the env file that holds the radio's settings, such as MESH_DEST
    BOX_WATCH_STATE      where the view and the alert record are kept  (default ~/.local/state/box-watch)

Files it reads, each optional but the first:
    ~/.roost_node_key                 the key a probe and a post to pulse carry
    ~/.config/box-watch/ntfy.topic    the ntfy topic an alert goes to
    ~/.config/box-watch/ha.json       {"url", "token", "notify"} for Home Assistant, the second channel
    ~/.roost-node-declared.json       the declaration, as the node report caches it, for the box's own services
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
NAME = os.environ.get("BOX_WATCH_NAME", "")
PORT = int(os.environ.get("BOX_WATCH_PORT", "9211"))
INTERVAL = int(os.environ.get("BOX_WATCH_INTERVAL", "30"))
PULSE = os.environ.get("BOX_WATCH_PULSE", "https://pulse.jimmyhoughjr.net").rstrip("/")
STATE = Path(os.environ.get("BOX_WATCH_STATE", "~/.local/state/box-watch")).expanduser()
KEY_FILE = HOME / ".roost_node_key"
TOPIC_FILE = HOME / ".config/box-watch/ntfy.topic"
HA_FILE = HOME / ".config/box-watch/ha.json"
DECLARED = HOME / ".roost-node-declared.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_peers(text: str) -> dict[str, str]:
    """The other voters, as name -> host:port."""
    peers = {}
    for part in text.split(","):
        name, sep, where = part.strip().partition("=")
        if sep and name and where:
            peers[name] = where
    return peers


# MARK: the vote, as pure functions

def peer_state(probe_ok: bool, last_answer: float | None, now: float, interval: int) -> str:
    """What this voter says of one peer: up when the probe answered, stale while a miss is younger than three
    intervals, and unreachable after that. One dropped probe is not a fault."""
    if probe_ok:
        return "up"
    if last_answer is not None and now - last_answer <= 3 * interval:
        return "stale"
    return "unreachable"


def votes_on(box: str, me: str, my_peers: dict, peer_documents: dict) -> dict[str, str]:
    """Every other voter's word on one box: mine, and each peer's from the document it answered with.
    A voter whose own document is missing says nothing, and the box has no vote on itself."""
    votes = {}
    if box != me and box in my_peers:
        votes[me] = my_peers[box].get("state", "unreachable")
    for voter, document in peer_documents.items():
        if voter == box or not document:
            continue
        seen = (document.get("peers") or {}).get(box)
        if seen:
            votes[voter] = seen.get("state", "unreachable")
    return votes


def agreed(votes: dict[str, str]) -> str:
    """Down or up on a majority of the voters that count, and split otherwise. A stale vote does not count."""
    counted = [state for state in votes.values() if state in ("up", "unreachable")]
    if not counted:
        return "split"
    down = sum(1 for state in counted if state == "unreachable")
    if down * 2 > len(counted):
        return "down"
    if (len(counted) - down) * 2 > len(counted):
        return "up"
    return "split"


def alerter(votes: dict[str, str]) -> str | None:
    """The one voter that speaks: the lowest-named of those that cannot reach the box."""
    seeing = sorted(voter for voter, state in votes.items() if state == "unreachable")
    return seeing[0] if seeing else None


# MARK: this box's own services

def declared_document() -> dict:
    """The declaration: the node report's cache where the box keeps one, and else this watcher's own copy, which it
    fetches from pulse with the node key once an hour. The copy is what answers while pulse is down."""
    try:
        return json.loads(DECLARED.read_text())
    except (OSError, ValueError):
        pass
    own = STATE / "declared.json"
    fresh = own.is_file() and time.time() - own.stat().st_mtime < 3600
    if not fresh:
        try:
            key = KEY_FILE.read_text().strip()
            request = urllib.request.Request(f"{PULSE}/api/declared", headers={"x-roost-node-key": key, "User-Agent": "box-watch/1"})
            body = urllib.request.urlopen(request, timeout=15).read()
            json.loads(body)
            STATE.mkdir(parents=True, exist_ok=True)
            own.write_bytes(body)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    try:
        return json.loads(own.read_text())
    except (OSError, ValueError):
        return {}


def declared_services(stacks: list[str]) -> list[dict]:
    """The services the declaration names for this box's stacks."""
    document = declared_document()
    if not document:
        return []
    found = []
    for stack in document.get("stacks", []):
        if stack.get("name") in stacks:
            for service in stack.get("services", []):
                found.append({"name": service.get("name", ""), "kind": service.get("kind", ""),
                              # The published declaration marks a job by its kind and carries no job object.
                              "backend": stack.get("backend", ""), "job": service.get("kind") == "job" or bool(service.get("job")),
                              "label": (service.get("job") or {}).get("label")})
    return found


def run(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def check_self(stacks: list[str]) -> list[dict]:
    """Each declared service as ok, failing or unknown, by asking the supervisor or docker on this box."""
    services = declared_services(stacks)
    if not services:
        return []
    status, listing = run(["docker", "ps", "--format", "{{.Names}}"])
    running = set(listing.split()) if status == 0 else None
    darwin = platform.system() == "Darwin"
    out = []
    for service in services:
        name = service["name"]
        state, detail = "unknown", ""
        if service["job"]:
            if darwin:
                label = service["label"] or (name if "." in name else f"net.jimmyhoughjr.{name}")
                code, text = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
                if code != 0:
                    state, detail = "failing", "launchd does not hold the job"
                else:
                    last = [line.split("=")[-1].strip() for line in text.splitlines() if "last exit code" in line]
                    bad = bool(last) and last[0] not in ("0", "(never exited)")
                    state, detail = ("failing", f"last exit {last[0]}") if bad else ("ok", "")
            else:
                unit = name
                code, text = run(["systemctl", "--user", "is-failed", f"{unit}.service"])
                state, detail = ("failing", "the unit is failed") if text.strip() == "failed" else ("ok", "")
        elif running is not None:
            container = f"{name}.web.1" if service["backend"] == "dokku" else name
            state, detail = ("ok", "") if container in running else ("failing", "no running container")
        out.append({"name": name, "state": state, "detail": detail})
    return out


# MARK: the channels

def ntfy(title: str, message: str) -> bool:
    try:
        topic = TOPIC_FILE.read_text().strip()
    except OSError:
        return False
    if not topic:
        return False
    request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode(), method="POST",
                                     headers={"Title": title, "Priority": "high", "Tags": "rotating_light", "User-Agent": "box-watch/1"})
    try:
        urllib.request.urlopen(request, timeout=15).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def home_assistant(title: str, message: str) -> bool:
    try:
        settings = json.loads(HA_FILE.read_text())
    except (OSError, ValueError):
        return False
    request = urllib.request.Request(f"{settings['url'].rstrip('/')}/api/services/notify/{settings['notify']}",
                                     data=json.dumps({"title": title, "message": message}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {settings['token']}", "Content-Type": "application/json", "User-Agent": "box-watch/1"})
    try:
        urllib.request.urlopen(request, timeout=15).read()
        return True
    except (urllib.error.URLError, OSError, KeyError):
        return False


def say(title: str, message: str) -> list[str]:
    """One alert through every channel this box holds. The names of the channels that took it come back."""
    took = []
    if ntfy(title, message):
        took.append("ntfy")
    if home_assistant(title, message):
        took.append("home-assistant")
    return took


def mesh(line: str) -> bool:
    """One line over the mesh radio. The radio's settings come from the env file the mesh alert's own unit reads,
    named by BOX_WATCH_MESH_ENV, so the destination is kept in one place."""
    command = os.environ.get("BOX_WATCH_MESH", "")
    if not command:
        return False
    env = dict(os.environ)
    try:
        for entry in Path(os.environ.get("BOX_WATCH_MESH_ENV", "")).expanduser().read_text().splitlines():
            key, sep, value = entry.strip().partition("=")
            if sep and key and not key.startswith("#"):
                env[key] = value.strip().strip('"')
    except OSError:
        pass
    try:
        done = subprocess.run([os.path.expanduser(command), line], capture_output=True, text=True, timeout=90, env=env)
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# MARK: the watcher

class Watcher:
    def __init__(self, name: str, peers: dict[str, str], stacks: list[str]):
        self.name = name
        self.peers = peers
        self.stacks = stacks
        self.key = KEY_FILE.read_text().strip() if KEY_FILE.is_file() else ""
        self.last_answer: dict[str, float] = {}
        # A peer never heard from is given the same grace as one that stopped, counted from this watcher's own start,
        # or a box that starts first calls every other box down. On 2026-09-30 the mini did that to the opi.
        self.started = time.time()
        self.documents: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.view: dict = {"voter": name, "at": now_iso(), "intervalS": INTERVAL, "peers": {}, "self": {"services": [], "checkedAt": now_iso()},
                           "alerting": {"box": None, "sentAt": None}}
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            self.said = json.loads((STATE / "said.json").read_text())
        except (OSError, ValueError):
            self.said = {}

    def probe(self, where: str) -> tuple[dict | None, int | None]:
        request = urllib.request.Request(f"http://{where}/view", headers={"x-roost-node-key": self.key, "User-Agent": "box-watch/1"})
        start = time.time()
        try:
            with urllib.request.urlopen(request, timeout=6) as answer:
                return json.loads(answer.read()), int((time.time() - start) * 1000)
        except (urllib.error.URLError, OSError, ValueError):
            return None, None

    def cycle(self) -> None:
        now = time.time()
        seen = {}
        for peer, where in self.peers.items():
            document, latency = self.probe(where)
            if document is not None:
                self.last_answer[peer] = now
                self.documents[peer] = document
            else:
                # A peer that does not answer keeps no say in the votes on others.
                self.documents.pop(peer, None)
            last = self.last_answer.get(peer)
            seen[peer] = {"state": peer_state(document is not None, last if last is not None else self.started, now, INTERVAL),
                          "lastAnswer": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
                          "latencyMs": latency}
        services = check_self(self.stacks)
        alerting = dict(self.view.get("alerting") or {"box": None, "sentAt": None})
        for box in self.peers:
            votes = votes_on(box, self.name, seen, self.documents)
            verdict = agreed(votes)
            before = self.said.get(box, "up")
            if verdict == "down" and before != "down":
                if alerter(votes) == self.name:
                    took = say(f"Box down: {box}", f"{box} does not answer {', '.join(sorted(v for v, s in votes.items() if s == 'unreachable'))}. Said by {self.name}.")
                    alerting = {"box": box, "sentAt": now_iso(), "via": took}
                self.said[box] = "down"
            elif verdict == "up" and before == "down":
                if sorted(votes)[0] == self.name:
                    say(f"Box back: {box}", f"{box} answers again. Said by {self.name}.")
                self.said[box] = "up"
        # The box that holds the radio speaks over it when it can reach nobody, once until somebody answers again.
        alone = bool(seen) and all(entry["state"] == "unreachable" for entry in seen.values())
        if alone and not self.said.get("_alone"):
            self.said["_alone"] = mesh(f"{self.name}: reaches no other box") or "no radio"
        elif not alone:
            self.said.pop("_alone", None)
        with self.lock:
            self.view = {"voter": self.name, "at": now_iso(), "intervalS": INTERVAL, "peers": seen,
                         "self": {"services": services, "checkedAt": now_iso()}, "alerting": alerting}
            body = json.dumps(self.view).encode()
        (STATE / "view.json").write_bytes(body)
        (STATE / "said.json").write_text(json.dumps(self.said))
        request = urllib.request.Request(f"{PULSE}/api/views", data=body, method="POST",
                                         headers={"x-roost-node-key": self.key, "Content-Type": "application/json", "User-Agent": "box-watch/1"})
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except (urllib.error.URLError, OSError):
            pass

    def handler(self):
        watcher = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments):  # a probe a minute is not worth a log line
                return

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
                if self.path != "/view":
                    self.send_response(404); self.end_headers(); return
                if watcher.key and self.headers.get("x-roost-node-key") != watcher.key:
                    self.send_response(401); self.end_headers(); return
                with watcher.lock:
                    body = json.dumps(watcher.view).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def serve(self) -> None:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), self.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"box-watch: {self.name} answers on :{PORT} and watches {', '.join(sorted(self.peers)) or 'nobody'} every {INTERVAL}s", flush=True)
        while True:
            started = time.time()
            try:
                self.cycle()
            except Exception as error:  # noqa: BLE001 - a bad pass is said and the next one runs
                print(f"box-watch: a pass failed: {error}", file=sys.stderr, flush=True)
            time.sleep(max(1.0, INTERVAL - (time.time() - started)))


def main() -> None:
    if not NAME:
        print("box-watch: BOX_WATCH_NAME is not set, so this box has no name to vote under", file=sys.stderr)
        sys.exit(2)
    stacks = [s.strip() for s in os.environ.get("BOX_WATCH_STACKS", "").split(",") if s.strip()]
    Watcher(NAME, parse_peers(os.environ.get("BOX_WATCH_PEERS", "")), stacks).serve()


if __name__ == "__main__":
    main()
