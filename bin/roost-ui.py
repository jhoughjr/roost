#!/usr/bin/env python3
"""roost ui — a full-screen terminal for the roost platform.

A Claude-Code-style interface in four tabs:
  1 console   a prompt + transcript; platform commands stream through `roost`
  2 monitor   live fleet: pi host, app containers, node watts (via pulse)
  3 config    ~/.roostrc, derived settings, per-app config viewer
  4 docs      playbook / getting-started / TODO in a section-aware pager

shift+tab cycles tabs from anywhere; on tabs 2-4 the digits 1-4 jump
straight to a tab and q returns to the console. Stdlib only.

Usage: roost ui   (or: python3 bin/roost-ui.py)
Keys:  tab/wheel click tabs · up/down history · pgup/pgdn scroll · ctrl+c cancel · ctrl+d quit
"""
import curses
import json
import locale
import os
import queue
import shlex
import stat
import subprocess
import sys
import threading
import time
import urllib.request

# Mouse button constants
BUTTON4_PRESSED = curses.BUTTON4_PRESSED
BUTTON5_PRESSED = getattr(curses, "BUTTON5_PRESSED", 0x2000000)

BIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN)
ROOST = os.path.join(BIN, "roost")
PLAYBOOK = os.path.join(ROOT, "docs", "playbook.md")
GETTING_STARTED = os.path.join(ROOT, "docs", "getting-started.md")
TODO = os.path.join(ROOT, "TODO.md")
README = os.path.join(ROOT, "README.md")


def read_rc():
    cfg = {}
    try:
        for line in open(os.path.expanduser("~/.roostrc")):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = os.path.expandvars(v.strip().strip('"'))
    except OSError:
        pass
    return cfg


RC = read_rc()
DOKKU = RC.get("ROOST_DOKKU_HOST", "dokku@192.168.0.103")
DOMAIN = RC.get("ROOST_DOMAIN", "jimmyhoughjr.net")
PULSE = RC.get("ROOST_PULSE_URL", "https://pulse.jimmyhoughjr.net")

PASSTHROUGH = ["apps", "ps", "logs", "restart", "config", "status",
               "fleet", "stats", "doctor", "backup", "new", "route"]
INTERNAL = ["playbook", "start", "todo", "help", "clear", "quit",
            "monitor", "docs"]
ALL_CMDS = sorted(set(PASSTHROUGH + INTERNAL))
APP_ARG = {"ps", "logs", "restart", "config"}

TABS = ["console", "monitor", "config", "docs"]
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASS", "PWD")

HELP = [
    ("b", "platform commands (pass through to roost):"),
    ("", "  apps                     list deployed apps"),
    ("", "  ps [app]                 container status"),
    ("", "  logs <app> [-n N]        tail app logs (default 200)"),
    ("", "  restart <app>            restart an app"),
    ("", "  config <app> [K=V ...]   show or set app config"),
    ("", "  status [\"message\"]       push the status site (fleet+history+ledger)"),
    ("", "  fleet                    refresh the fleet board json"),
    ("", "  stats                    run configured board-stat collectors"),
    ("", "  doctor                   diagnose the setup"),
    ("", "  backup                   pull pi data to ~/Backups/roost"),
    ("", "  new <name> [--static|--node|--swift]    nothing → live app"),
    ("", "  route <subdomain>        publish a tunnel route"),
    ("", ""),
    ("b", "tabs (shift+tab cycles · digits jump from tabs 2-4):"),
    ("", "  monitor                  live fleet: pi, containers, node watts"),
    ("", "  config  (no app)         roostrc + per-app config viewer"),
    ("", "  docs                     playbook & friends in the pager"),
    ("", ""),
    ("b", "built in:"),
    ("", "  playbook                 browse the operating manual"),
    ("", "  start                    browse getting-started.md"),
    ("", "  todo                     show TODO.md"),
    ("", "  clear                    clear the transcript"),
    ("", "  help · quit"),
    ("", ""),
    ("d", "keys: tab complete · up/down history · pgup/pgdn scroll · ctrl+c cancel · ctrl+d quit"),
]

COLORS = {}


def init_colors():
    COLORS[""] = curses.A_NORMAL
    COLORS["d"] = curses.A_DIM
    COLORS["b"] = curses.A_BOLD
    if curses.has_colors():
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        COLORS["r"] = curses.color_pair(1)
        COLORS["y"] = curses.color_pair(2) | curses.A_BOLD
        COLORS["p"] = curses.color_pair(3) | curses.A_BOLD
        COLORS["g"] = curses.color_pair(4)
        COLORS["e"] = curses.color_pair(1) | curses.A_BOLD
    else:
        for k in "rypge":
            COLORS[k] = curses.A_BOLD


def attr(key):
    return COLORS.get(key, curses.A_NORMAL)


def put(scr, y, x, text, a=curses.A_NORMAL, n=None):
    """addnstr that never throws on the last cell / narrow windows."""
    if n is None:
        n = max(0, scr.getmaxyx()[1] - x - 1)
    try:
        scr.addnstr(y, x, text, max(0, n), a)
    except curses.error:
        pass


def bar(frac, width=10):
    f = max(0, min(width, round(frac * width)))
    return "█" * f + "░" * (width - f)


def human_age(s):
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d"


def mask_val(v):
    return "•" * min(max(len(v), 3), 10)


class Runner:
    """Runs one roost command at a time, streaming lines back on a queue."""

    def __init__(self):
        self.q = queue.Queue()
        self.proc = None
        self.running = False
        self.label = ""

    def start(self, argv, label):
        self.running = True
        self.label = label
        threading.Thread(target=self._run, args=(argv,), daemon=True).start()

    def _run(self, argv):
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, errors="replace")
            for line in self.proc.stdout:
                self.q.put(("line", line.rstrip("\n")))
            code = self.proc.wait()
            self.q.put(("done", code))
        except OSError as e:
            self.q.put(("line", f"error: {e}"))
            self.q.put(("done", 127))
        finally:
            self.proc = None

    def cancel(self):
        p = self.proc
        if p:
            try:
                p.terminate()
            except OSError:
                pass


class Stats:
    """Background fetcher for pulse /api/stats — refreshes every 30 s."""

    def __init__(self, base):
        self.url = base.rstrip("/") + "/api/stats"
        self.data = None
        self.err = ""
        self.t0 = 0.0
        self.fetching = False
        self.ev = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            self.fetching = True
            try:
                # cloudflare 403s the default Python-urllib user agent
                req = urllib.request.Request(
                    self.url, headers={"User-Agent": "roost-ui"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    self.data = json.loads(r.read())
                self.err = ""
                self.t0 = time.monotonic()
            except Exception as e:               # noqa: BLE001 — surface any failure
                self.err = str(e)[:100]
            self.fetching = False
            self.ev.wait(30)
            self.ev.clear()

    def refresh(self):
        self.ev.set()

    def age(self):
        return (time.monotonic() - self.t0) if self.t0 else None


class DocView:
    """Section-aware markdown pager: a TOC of ## headings, then the doc."""

    def __init__(self, title, text):
        self.title = title
        self.raw = text.splitlines()
        self.sections = [(l[3:].strip(), i)
                         for i, l in enumerate(self.raw) if l.startswith("## ")]
        self.mode = "toc" if self.sections else "doc"
        self.sel = 0
        self.top = 0
        self.lines = []
        self.w = 0

    def wrap(self, w):
        if w == self.w and self.lines:
            return
        self.w = w
        self.lines = []
        inner = max(20, w - 4)
        fence = False
        for idx, raw in enumerate(self.raw):
            a = ""
            if raw.startswith("```"):
                fence = not fence
                a = "d"
            elif fence or raw.startswith("    ") or raw.startswith(">"):
                a = "d"
            elif raw.startswith("## "):
                a = "y"
            elif raw.startswith("#"):
                a = "b"
            if not raw:
                self.lines.append((a, "", idx))
                continue
            s = raw
            while len(s) > inner:
                cut = s.rfind(" ", inner // 2, inner)
                if cut < 0:
                    cut = inner
                self.lines.append((a, s[:cut], idx))
                s = s[cut:].lstrip()
            self.lines.append((a, s, idx))

    def offset_of(self, rawidx):
        for n, (_, _, i) in enumerate(self.lines):
            if i >= rawidx:
                return n
        return max(0, len(self.lines) - 1)

    def cur_section(self):
        if not self.lines:
            return ""
        rawidx = self.lines[min(self.top, len(self.lines) - 1)][2]
        name = ""
        for t, i in self.sections:
            if i <= rawidx:
                name = t
        return name

    def draw(self, scr, y0, body, w):
        """Render into rows y0..y0+body+1; returns the status-bar hint."""
        self.wrap(w)
        if self.mode == "toc":
            put(scr, y0, 1, f"{self.title} — sections", attr("b"))
            first = max(0, min(self.sel - body + 1, len(self.sections) - body))
            for i, (t, _) in enumerate(self.sections[first:first + body]):
                n = first + i
                a = curses.A_REVERSE if n == self.sel else attr("")
                put(scr, y0 + 2 + i, 2, f"{'▸' if n == self.sel else ' '} § {t}", a)
            return "up/down select · enter open · a whole doc · q back"
        self.top = max(0, min(self.top, max(0, len(self.lines) - body)))
        sect = self.cur_section()
        put(scr, y0, 1, self.title + (f" — § {sect}" if sect else ""), attr("b"))
        for i, (a, text, _) in enumerate(self.lines[self.top:self.top + body]):
            put(scr, y0 + 2 + i, 2, text, attr(a))
        return "up/down scroll · space/b page · n/p section · g/G ends · q back"

    def handle(self, ch, body):
        """Returns 'close' when the view is done, else None."""
        if ch == curses.KEY_RESIZE:
            self.w = 0
            return None
        if self.mode == "toc":
            if ch in ("q", "\x1b"):
                return "close"
            if ch in (curses.KEY_UP, "k"):
                self.sel = max(0, self.sel - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                self.sel = min(len(self.sections) - 1, self.sel + 1)
            elif ch == "a":
                self.top = 0
                self.mode = "doc"
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.top = self.offset_of(self.sections[self.sel][1])
                self.mode = "doc"
            return None
        if ch in ("q", "\x1b"):
            if self.sections:
                self.mode = "toc"
                return None
            return "close"
        if ch in (curses.KEY_UP, "k"):
            self.top -= 1
        elif ch in (curses.KEY_DOWN, "j", "\n", "\r"):
            self.top += 1
        elif ch in (curses.KEY_PPAGE, "b"):
            self.top -= body
        elif ch in (curses.KEY_NPAGE, " "):
            self.top += body
        elif ch == "g":
            self.top = 0
        elif ch == "G":
            self.top = len(self.lines)
        elif ch in ("n", "p"):
            offs = [self.offset_of(i) for _, i in self.sections]
            if ch == "n":
                nxt = [o for o in offs if o > self.top]
                self.top = nxt[0] if nxt else self.top
            else:
                prv = [o for o in offs if o < self.top]
                self.top = prv[-1] if prv else 0
        return None


DOCS = [
    ("playbook", PLAYBOOK, "the operating manual — six steps, upkeep, recovery"),
    ("getting started", GETTING_STARTED, "zero to a deployed app"),
    ("todo", TODO, "what's next for roost"),
    ("readme", README, "repo overview and file map"),
]


class UI:
    def __init__(self, scr):
        self.scr = scr
        self.h, self.w = scr.getmaxyx()
        self.tab = 0
        self.transcript = []          # (attr-key, wrapped line)
        self.scroll = 0               # lines scrolled up from the bottom
        self.buf = ""
        self.cur = 0
        self.hist = []
        self.hidx = None
        self.pending = ""
        self.runner = Runner()
        self.stats = Stats(PULSE)
        self.apps = []
        self.spin = 0
        self.done = False
        self.mon_scroll = 0
        self.cfg_scroll = 0
        self.cfg_sel = 0
        self.cfg_app = ""
        self.cfg_lines = []           # fetched `roost config <app>` output
        self.cfg_masked = True
        self.doc_sel = 0
        self.docview = None
        self.tab_spans = []           # [(x_start, x_end, tab_index), ...]
        threading.Thread(target=self._fetch_apps, daemon=True).start()

    def _fetch_apps(self):
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                 DOKKU, "apps:list"],
                capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                self.apps = [a.strip() for a in r.stdout.splitlines()
                             if a.strip() and not a.startswith("=")]
        except (OSError, subprocess.SubprocessError):
            pass

    def app_names(self):
        """Apps for completion and the config picker; pulse fills the gap
        until (or in case) the direct ssh listing fails."""
        if self.apps:
            return self.apps
        d = self.stats.data or {}
        return [a["name"] for a in d.get("apps", []) if a.get("name")]

    # ---- transcript -------------------------------------------------
    def say(self, text, a=""):
        w = max(20, self.w - 2)
        for raw in text.split("\n"):
            if not raw:
                self.transcript.append((a, ""))
                continue
            while len(raw) > w:
                self.transcript.append((a, raw[:w]))
                raw = raw[w:]
            self.transcript.append((a, raw))
        del self.transcript[:-5000]

    def welcome(self):
        self.say("   ,,,", "r")
        self.say("   (o>        r o o s t", "r")
        self.say(r"\\_//)        one prompt for the whole platform.", "r")
        self.say(r" \_/_)", "r")
        self.say("  _|_", "r")
        self.say("═════════", "r")
        self.say("")
        self.say("help lists commands · playbook opens the manual · quit exits", "d")
        self.say("shift+tab cycles tabs: console · monitor · config · docs", "d")
        self.say("")

    def show_file(self, path):
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            self.say(f"cannot read {path}: {e}", "e")
            self.say("")
            return
        for line in text.splitlines():
            self.say(line, "b" if line.startswith("#") else "")
        self.say("")

    def open_doc(self, path):
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            self.say(f"cannot read {path}: {e}", "e")
            self.say("")
            return
        for i, (_, p, _) in enumerate(DOCS):
            if p == path:
                self.doc_sel = i
        self.docview = DocView(os.path.relpath(path, ROOT), text)
        self.tab = 3

    def open_doc_by_index(self, idx):
        """Open a doc by its index in DOCS list."""
        if 0 <= idx < len(DOCS):
            name, path, _ = DOCS[idx]
            self.doc_sel = idx
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as e:
                text = f"cannot read {path}: {e}"
            self.docview = DocView(os.path.relpath(path, ROOT), text)

    # ---- command handling -------------------------------------------
    def submit(self):
        line = self.buf.strip()
        self.buf = ""
        self.cur = 0
        self.hidx = None
        self.scroll = 0
        if not line:
            return
        if not self.hist or self.hist[-1] != line:
            self.hist.append(line)
        self.say("❯ " + line, "p")
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            self.say(f"parse error: {e}", "e")
            self.say("")
            return
        cmd = tokens[0]
        if cmd in ("quit", "exit"):
            self.done = True
        elif cmd == "clear":
            self.transcript.clear()
        elif cmd == "help":
            for a, t in HELP:
                self.say(t, a)
            self.say("")
        elif cmd == "todo":
            self.show_file(TODO)
        elif cmd == "monitor":
            self.tab = 1
            self.stats.refresh()
        elif cmd == "docs":
            self.tab = 3
        elif cmd == "config" and len(tokens) == 1:
            self.tab = 2
        elif cmd in ("playbook", "start"):
            self.open_doc(PLAYBOOK if cmd == "playbook" else GETTING_STARTED)
        elif cmd in PASSTHROUGH:
            if self.runner.running:
                self.say("a command is already running — ctrl+c cancels it", "e")
                self.say("")
            else:
                self.runner.start([ROOST] + tokens, " ".join(tokens))
        else:
            self.say(f"unknown command: {cmd} (help lists everything)", "e")
            self.say("")

    def complete(self):
        head = self.buf[:self.cur]
        parts = head.split(" ")
        if len(parts) == 1:
            cands = [c for c in ALL_CMDS if c.startswith(parts[0])]
            add_space = True
        elif parts[0] in APP_ARG and len(parts) == 2:
            cands = sorted(a for a in self.app_names() if a.startswith(parts[-1]))
            add_space = False
        else:
            return
        if not cands:
            return
        pref = os.path.commonprefix(cands)
        new = " ".join(parts[:-1] + [pref])
        if len(cands) == 1 and add_space:
            new += " "
        self.buf = new + self.buf[self.cur:]
        self.cur = len(new)
        if len(cands) > 1:
            self.say("  ".join(cands), "d")

    def hist_move(self, d):
        if not self.hist:
            return
        if self.hidx is None:
            if d > 0:
                return
            self.pending = self.buf
            self.hidx = len(self.hist) - 1
        else:
            self.hidx += d
        if self.hidx is not None and self.hidx >= len(self.hist):
            self.hidx = None
            self.buf = self.pending
        else:
            self.hidx = max(0, self.hidx)
            self.buf = self.hist[self.hidx]
        self.cur = len(self.buf)

    # ---- monitor tab --------------------------------------------------
    def monitor_lines(self):
        st = self.stats
        d = st.data
        L = []
        if st.err and not d:
            L.append(("e", f"pulse unreachable: {st.err}"))
            L.append(("d", "r retries · it refreshes on its own every 30 s"))
            return L
        if not d:
            L.append(("d", "fetching stats from pulse …"))
            return L
        age = int(st.age() or 0)
        upd = "fetching …" if st.fetching else f"updated {human_age(age)} ago"
        note = f" · stale: {st.err}" if st.err else ""
        L.append(("d", f"{upd} · r refresh · auto every 30 s{note}"))
        L.append(("", ""))
        host = d.get("host", {})
        cores = host.get("cores", 1) or 1
        load1 = host.get("load1", 0)
        mt, mu = host.get("memTotalMb", 0), host.get("memUsedMb", 0)
        L.append(("b", f"pi · {DOKKU}"))
        L.append(("", f"  load [{bar(load1 / cores)}] {load1:.2f} / {cores} cores"
                      f"    mem [{bar(mu / mt if mt else 0)}] "
                      f"{mu / 1024:.1f} / {mt / 1024:.1f} GB"))
        L.append(("", ""))
        apps = d.get("apps", [])
        running = sum(1 for a in apps if a.get("state") == "running")
        L.append(("b", f"apps ({running}/{len(apps)} running)"))
        nw = max([len(a.get("name", "")) for a in apps] + [4])
        for a in sorted(apps, key=lambda x: x.get("name", "")):
            ok = a.get("state") == "running"
            L.append(("" if ok else "e",
                      f"  {a.get('name', '?'):<{nw}}  {a.get('state', '?'):<9}"
                      f" {a.get('cpuPct', 0):>5.1f}%  {a.get('memMb', 0):>7.1f} MB"
                      f"  {a.get('up', '')}"))
        system = d.get("system", [])
        if system:
            L.append(("", ""))
            L.append(("b", "system containers"))
            for a in system:
                ok = a.get("state") == "running"
                L.append(("d" if ok else "e",
                          f"  {a.get('name', '?'):<{nw}}  {a.get('state', '?'):<9}"
                          f" {a.get('cpuPct', 0):>5.1f}%  {a.get('memMb', 0):>7.1f} MB"
                          f"  {a.get('up', '')}"))
        nodes = d.get("nodes", [])
        L.append(("", ""))
        L.append(("b", f"nodes ({len(nodes)}) — watts estimated from load"))
        if not nodes:
            L.append(("d", "  none reporting — see bin/install-node-report.sh"))
        total_w = 0.0
        nnw = max([len(n.get("name", "")) for n in nodes] + [4])
        for n in sorted(nodes, key=lambda x: x.get("name", "")):
            ncores = n.get("cores", 1) or 1
            nl = n.get("load1", 0)
            frac = min(nl / ncores, 1.0)
            watts = n.get("idleW", 0) + frac * (n.get("maxW", 0) - n.get("idleW", 0))
            nage = n.get("ageS", 0) + age
            stale = nage > 90
            if not stale:
                total_w += watts
            nmt, nmu = n.get("memTotalMb", 0), n.get("memUsedMb", 0)
            L.append(("e" if stale else "",
                      f"  {n.get('name', '?'):<{nnw}}  {n.get('model', ''):<16}"
                      f" load {nl:>5.2f}/{ncores}"
                      f"  mem {nmu / 1024:.1f}/{nmt / 1024:.1f} GB"
                      f"  ~{watts:>4.1f} W"
                      f"  {'stale ' if stale else ''}{human_age(nage)}"))
        if total_w:
            L.append(("d", f"  ~{total_w:.1f} W estimated across live nodes"
                           " (pi wall watts not measured)"))
        return L

    # ---- config tab ---------------------------------------------------
    def config_lines(self):
        L = []
        rc_path = os.path.expanduser("~/.roostrc")
        L.append(("b", f"roostrc — {rc_path}"))
        try:
            for raw in open(rc_path):
                raw = raw.rstrip("\n")
                s = raw.strip()
                if not s or s.startswith("#"):
                    L.append(("d", "  " + raw))
                elif "=" in s:
                    k, v = s.split("=", 1)
                    secret = any(t in k.upper() for t in SECRET_HINTS)
                    shown = mask_val(v) if secret and self.cfg_masked else v
                    L.append(("", f"  {k.strip()} = {shown}"))
                else:
                    L.append(("", "  " + raw))
        except OSError:
            L.append(("d", "  (no ~/.roostrc — defaults in effect)"))
        L.append(("", ""))
        L.append(("b", "derived"))
        L.append(("", f"  dokku host   {DOKKU}"))
        L.append(("", f"  domain       {DOMAIN}"))
        L.append(("", f"  pulse        {PULSE}"))
        key_path = os.path.expanduser("~/.roost_node_key")
        try:
            mode = oct(stat.S_IMODE(os.stat(key_path).st_mode))[-3:]
            note = "" if mode == "600" else "  ← should be 600"
            L.append(("", f"  node key     {key_path} (mode {mode}){note}"))
        except OSError:
            L.append(("d", f"  node key     {key_path} missing"
                           " (node reporting disabled on this machine)"))
        L.append(("", ""))
        names = self.app_names()
        mode_hint = "m reveal" if self.cfg_masked else "m mask"
        L.append(("b", f"app config — ←/→ pick · enter fetch · {mode_hint}"))
        if not names:
            L.append(("d", "  (app list loading …)"))
        else:
            self.cfg_sel = max(0, min(self.cfg_sel, len(names) - 1))
            row = "  " + "  ".join(
                f"[{n}]" if i == self.cfg_sel else f" {n} "
                for i, n in enumerate(names))
            L.append(("p", row))
        if self.cfg_app:
            L.append(("", ""))
            L.append(("b", f"  {self.cfg_app}"))
            for a, ln in self.cfg_lines:
                if self.cfg_masked and ":" in ln and not a:
                    k, v = ln.split(":", 1)
                    ln = f"{k}: {mask_val(v.strip())}"
                L.append((a, "  " + ln))
        return L

    def fetch_cfg(self):
        names = self.app_names()
        if not names:
            return
        app = names[max(0, min(self.cfg_sel, len(names) - 1))]
        self.cfg_app = app
        self.cfg_lines = [("d", "fetching …")]

        def go():
            try:
                r = subprocess.run([ROOST, "config", app], capture_output=True,
                                   text=True, timeout=30)
                out = (r.stdout + r.stderr).splitlines()
                self.cfg_lines = [("", ln) for ln in out] or [("d", "(no output)")]
            except (OSError, subprocess.SubprocessError) as e:
                self.cfg_lines = [("e", f"error: {e}")]
        threading.Thread(target=go, daemon=True).start()

    # ---- event loop ---------------------------------------------------
    def drain(self):
        try:
            while True:
                kind, val = self.runner.q.get_nowait()
                if kind == "line":
                    self.say(val)
                else:
                    self.runner.running = False
                    if val not in (0, None):
                        self.say(f"(exit {val})", "e")
                    self.say("")
        except queue.Empty:
            pass

    def page(self):
        return max(1, self.h - 6)

    def draw_tabs(self):
        scr = self.scr
        put(scr, 0, 0, " (o> ", attr("r"))
        x = 6
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.tab_spans = []
        for i, name in enumerate(TABS):
            label = f" {i + 1} {name} "
            if i == 0 and self.runner.running:
                label = f" 1 {name} {frames[self.spin % len(frames)]} "
            a = curses.A_REVERSE if i == self.tab else attr("d")
            self.tab_spans.append((x, x + len(label), i))
            put(scr, 0, x, label, a)
            x += len(label) + 1
        right = f"{DOKKU} · {DOMAIN} "
        if x < self.w - len(right) - 1:
            put(scr, 0, self.w - len(right) - 1, right, attr("d"))
        try:
            scr.hline(1, 0, curses.ACS_HLINE, self.w)
        except curses.error:
            pass

    def draw_list_tab(self, lines, scroll):
        """Scrollable (attr, line) list on rows 2..h-2; returns clamped scroll."""
        body = max(1, self.h - 3)
        scroll = max(0, min(scroll, max(0, len(lines) - body)))
        for i, (a, text) in enumerate(lines[scroll:scroll + body]):
            put(self.scr, 2 + i, 1, text, attr(a))
        return scroll

    def render(self):
        scr = self.scr
        self.h, self.w = scr.getmaxyx()
        scr.erase()
        self.draw_tabs()
        if self.tab == 0:
            try:
                scr.hline(self.h - 3, 0, curses.ACS_HLINE, self.w)
            except curses.error:
                pass
            top, rows = 2, max(1, self.h - 5)
            total = len(self.transcript)
            self.scroll = max(0, min(self.scroll, max(0, total - rows)))
            start = max(0, total - rows - self.scroll)
            for i, (a, text) in enumerate(self.transcript[start:start + rows]):
                put(scr, top + i, 1, text, attr(a))
            if self.runner.running:
                s = f" running: {self.runner.label} — ctrl+c to cancel"
            elif self.scroll:
                s = f" ── scrolled up {self.scroll} · pgdn to follow"
            else:
                s = (" tab complete · up/down history · pgup/pgdn/wheel scroll"
                     " · click/shift+tab tabs · help · quit")
            put(scr, self.h - 2, 0, s, attr("d"))
            put(scr, self.h - 1, 0, " ❯ ", attr("p"))
            avail = max(1, self.w - 6)
            off = max(0, self.cur - avail + 1)
            put(scr, self.h - 1, 4, self.buf[off:off + avail], attr(""), avail)
            try:
                curses.curs_set(1)
                scr.move(self.h - 1, 4 + self.cur - off)
            except curses.error:
                pass
            scr.refresh()
            return
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        hint = ""
        if self.tab == 1:
            self.mon_scroll = self.draw_list_tab(self.monitor_lines(),
                                                 self.mon_scroll)
            hint = "r refresh · up/down scroll · 1-4 tabs · q console"
        elif self.tab == 2:
            self.cfg_scroll = self.draw_list_tab(self.config_lines(),
                                                 self.cfg_scroll)
            hint = ("←/→ app · enter fetch · m mask · r refresh"
                    " · up/down scroll · q console")
        elif self.tab == 3:
            if self.docview:
                hint = self.docview.draw(scr, 2, max(1, self.h - 5), self.w)
                hint += " · 1-4 tabs"
            else:
                put(scr, 2, 1, "docs", attr("b"))
                for i, (name, _, desc) in enumerate(DOCS):
                    a = curses.A_REVERSE if i == self.doc_sel else attr("")
                    put(scr, 4 + i, 2,
                        f"{'▸' if i == self.doc_sel else ' '} {name:<16} {desc}", a)
                hint = "up/down select · enter open · 1-4 tabs · q console"
        put(scr, self.h - 1, 1, hint, attr("d"))
        scr.refresh()

    # ---- key handling ---------------------------------------------------
    def handle_tabbed(self, ch):
        """Keys on the monitor / config / docs tabs."""
        if ch in ("1", "2", "3", "4"):
            self.tab = int(ch) - 1
            if self.tab == 1:
                self.stats.refresh()
            return
        if self.tab == 3 and self.docview:
            if self.docview.handle(ch, max(1, self.h - 5)) == "close":
                self.docview = None
            return
        if ch in ("q", "\x1b", "\x03"):
            self.tab = 0
            return
        if self.tab == 1:
            if ch == "r":
                self.stats.refresh()
            elif ch in (curses.KEY_UP, "k"):
                self.mon_scroll += 1
            elif ch in (curses.KEY_DOWN, "j"):
                self.mon_scroll = max(0, self.mon_scroll - 1)
            elif ch == curses.KEY_PPAGE:
                self.mon_scroll += self.page()
            elif ch == curses.KEY_NPAGE:
                self.mon_scroll = max(0, self.mon_scroll - self.page())
        elif self.tab == 2:
            if ch == curses.KEY_LEFT:
                self.cfg_sel = max(0, self.cfg_sel - 1)
            elif ch == curses.KEY_RIGHT:
                self.cfg_sel += 1        # clamped against the app list at render
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.fetch_cfg()
            elif ch == "m":
                self.cfg_masked = not self.cfg_masked
            elif ch == "r":
                global RC, DOKKU, DOMAIN, PULSE
                RC = read_rc()
                DOKKU = RC.get("ROOST_DOKKU_HOST", DOKKU)
                DOMAIN = RC.get("ROOST_DOMAIN", DOMAIN)
                PULSE = RC.get("ROOST_PULSE_URL", PULSE)
                threading.Thread(target=self._fetch_apps, daemon=True).start()
            elif ch in (curses.KEY_UP, "k"):
                self.cfg_scroll += 1
            elif ch in (curses.KEY_DOWN, "j"):
                self.cfg_scroll = max(0, self.cfg_scroll - 1)
            elif ch == curses.KEY_PPAGE:
                self.cfg_scroll += self.page()
            elif ch == curses.KEY_NPAGE:
                self.cfg_scroll = max(0, self.cfg_scroll - self.page())
        elif self.tab == 3:
            if ch in (curses.KEY_UP, "k"):
                self.doc_sel = max(0, self.doc_sel - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                self.doc_sel = min(len(DOCS) - 1, self.doc_sel + 1)
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.open_doc_by_index(self.doc_sel)

    def handle(self, ch):
        # Handle mouse events at the top, before anything else
        if ch == curses.KEY_MOUSE:
            try:
                _mouse_id, mx, my, _z, bstate = curses.getmouse()
            except curses.error:
                return
            # Wheel up: scroll active view up by 3
            if bstate & BUTTON4_PRESSED:
                if self.tab == 0:
                    self.scroll += 3
                elif self.tab == 1:
                    self.mon_scroll += 3
                elif self.tab == 2:
                    self.cfg_scroll += 3
                elif self.tab == 3 and self.docview:
                    self.docview.top -= 3
                elif self.tab == 3:
                    self.doc_sel = max(0, self.doc_sel - 1)
                return
            # Wheel down: scroll active view down by 3
            if bstate & BUTTON5_PRESSED:
                if self.tab == 0:
                    self.scroll = max(0, self.scroll - 3)
                elif self.tab == 1:
                    self.mon_scroll = max(0, self.mon_scroll - 3)
                elif self.tab == 2:
                    self.cfg_scroll = max(0, self.cfg_scroll - 3)
                elif self.tab == 3 and self.docview:
                    self.docview.top += 3
                elif self.tab == 3:
                    self.doc_sel = min(len(DOCS) - 1, self.doc_sel + 1)
                return
            # Left click: handle tab switching or doc selection
            if bstate & curses.BUTTON1_PRESSED:
                # Click on tab row (row 0)
                if my == 0:
                    for x_start, x_end, tab_idx in self.tab_spans:
                        if x_start <= mx < x_end:
                            self.tab = tab_idx
                            if self.tab == 1:
                                self.stats.refresh()
                            return
                # Click on doc list (tab 3, rows 4+, no docview open)
                if self.tab == 3 and not self.docview and my >= 4:
                    doc_idx = my - 4
                    if doc_idx < len(DOCS):
                        prev_sel = self.doc_sel
                        self.doc_sel = doc_idx
                        # If clicking the already-selected doc, open it
                        if doc_idx == prev_sel:
                            self.open_doc_by_index(self.doc_sel)
                return
            return

        if ch == curses.KEY_BTAB:                # shift+tab cycles tabs
            self.tab = (self.tab + 1) % len(TABS)
            if self.tab == 1:
                self.stats.refresh()
            return
        if ch == "\x04" and (self.tab != 0 or not self.buf):
            self.done = True                     # ctrl+d quits everywhere
            return
        if self.tab != 0:
            if ch == curses.KEY_RESIZE:
                if self.docview:
                    self.docview.w = 0
                return
            self.handle_tabbed(ch)
            return
        if isinstance(ch, int):
            if ch == curses.KEY_RESIZE:
                pass
            elif ch in (curses.KEY_BACKSPACE,):
                self.backspace()
            elif ch == curses.KEY_DC and self.cur < len(self.buf):
                self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
            elif ch == curses.KEY_LEFT:
                self.cur = max(0, self.cur - 1)
            elif ch == curses.KEY_RIGHT:
                self.cur = min(len(self.buf), self.cur + 1)
            elif ch == curses.KEY_UP:
                self.hist_move(-1)
            elif ch == curses.KEY_DOWN:
                self.hist_move(1)
            elif ch == curses.KEY_HOME:
                self.cur = 0
            elif ch == curses.KEY_END:
                self.cur = len(self.buf)
            elif ch == curses.KEY_PPAGE:
                self.scroll += self.page()
            elif ch == curses.KEY_NPAGE:
                self.scroll = max(0, self.scroll - self.page())
            return
        if ch in ("\n", "\r"):
            self.submit()
        elif ch == "\t":
            self.complete()
        elif ch in ("\x7f", "\b"):
            self.backspace()
        elif ch == "\x03":                       # ctrl+c
            if self.runner.running:
                self.runner.cancel()
                self.say("^C — cancelling", "e")
            elif self.buf:
                self.buf = ""
                self.cur = 0
            else:
                self.say("(type quit or press ctrl+d to exit)", "d")
        elif ch == "\x15":                       # ctrl+u
            self.buf = self.buf[self.cur:]
            self.cur = 0
        elif ch == "\x0b":                       # ctrl+k
            self.buf = self.buf[:self.cur]
        elif ch == "\x01":                       # ctrl+a
            self.cur = 0
        elif ch == "\x05":                       # ctrl+e
            self.cur = len(self.buf)
        elif ch == "\x0c":                       # ctrl+l
            self.transcript.clear()
            self.scroll = 0
        elif isinstance(ch, str) and ch.isprintable():
            self.buf = self.buf[:self.cur] + ch + self.buf[self.cur:]
            self.cur += 1

    def backspace(self):
        if self.cur > 0:
            self.buf = self.buf[:self.cur - 1] + self.buf[self.cur:]
            self.cur -= 1

    def run(self):
        curses.raw()
        self.scr.keypad(True)
        self.scr.timeout(90)
        # Enable mouse support
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        self.welcome()
        while not self.done:
            self.drain()
            self.render()
            try:
                ch = self.scr.get_wch()
            except curses.error:                 # timeout tick
                self.spin += 1
                continue
            self.handle(ch)


def main(scr):
    init_colors()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    # Enable xterm mouse reporting for wheel events and better tracking
    sys.stdout.write("\x1b[?1002h\x1b[?1006h")
    sys.stdout.flush()
    try:
        UI(scr).run()
    finally:
        # Restore terminal to normal mouse mode
        sys.stdout.write("\x1b[?1002l\x1b[?1006l")
        sys.stdout.flush()


if __name__ == "__main__":
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        sys.exit("roost ui needs an interactive terminal")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
