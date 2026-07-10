#!/usr/bin/env python3
"""roost ui — a full-screen terminal for the roost platform.

A Claude-Code-style interface: a transcript that scrolls, a prompt that
stays at the bottom, and the platform one word away. Platform commands
(apps, ps, logs, status, doctor, ...) pass through to `roost` and stream
their output into the transcript; `playbook` opens the operating manual
in a section-aware pager. Stdlib only — nothing to install.

Usage: roost ui   (or: python3 bin/roost-ui.py)
Keys:  tab complete · up/down history · pgup/pgdn scroll · ctrl+c cancel · ctrl+d quit
"""
import curses
import locale
import os
import queue
import shlex
import subprocess
import sys
import threading

BIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN)
ROOST = os.path.join(BIN, "roost")
PLAYBOOK = os.path.join(ROOT, "docs", "playbook.md")
GETTING_STARTED = os.path.join(ROOT, "docs", "getting-started.md")
TODO = os.path.join(ROOT, "TODO.md")


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

PASSTHROUGH = ["apps", "ps", "logs", "restart", "config", "status",
               "fleet", "stats", "doctor", "backup", "new", "route"]
INTERNAL = ["playbook", "start", "todo", "help", "clear", "quit"]
ALL_CMDS = sorted(PASSTHROUGH + INTERNAL)
APP_ARG = {"ps", "logs", "restart", "config"}

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


class Pager:
    """Section-aware markdown pager: a TOC of ## headings, then the doc."""

    def __init__(self, scr, title, text):
        self.scr = scr
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

    def run(self):
        scr = self.scr
        scr.timeout(-1)
        while True:
            h, w = scr.getmaxyx()
            self.wrap(w)
            body = h - 3
            scr.erase()
            if self.mode == "toc":
                put(scr, 0, 1, f"{self.title} — sections", attr("b"))
                first = max(0, min(self.sel - body + 1, len(self.sections) - body))
                for i, (t, _) in enumerate(self.sections[first:first + body]):
                    n = first + i
                    a = curses.A_REVERSE if n == self.sel else attr("")
                    put(scr, 2 + i, 2, f"{'▸' if n == self.sel else ' '} § {t}", a)
                put(scr, h - 1, 1,
                    "up/down select · enter open · a whole doc · q close", attr("d"))
            else:
                self.top = max(0, min(self.top, max(0, len(self.lines) - body)))
                sect = self.cur_section()
                put(scr, 0, 1, self.title + (f" — § {sect}" if sect else ""), attr("b"))
                for i, (a, text, _) in enumerate(self.lines[self.top:self.top + body]):
                    put(scr, 2 + i, 2, text, attr(a))
                put(scr, h - 1, 1,
                    "up/down scroll · space/b page · n/p section · g/G ends · q back",
                    attr("d"))
            scr.refresh()
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_RESIZE:
                self.w = 0
                continue
            if self.mode == "toc":
                if ch in ("q", "\x1b"):
                    return
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
            else:
                if ch in ("q", "\x1b"):
                    if self.sections:
                        self.mode = "toc"
                    else:
                        return
                elif ch in (curses.KEY_UP, "k"):
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


class UI:
    def __init__(self, scr):
        self.scr = scr
        self.h, self.w = scr.getmaxyx()
        self.transcript = []          # (attr-key, wrapped line)
        self.scroll = 0               # lines scrolled up from the bottom
        self.buf = ""
        self.cur = 0
        self.hist = []
        self.hidx = None
        self.pending = ""
        self.runner = Runner()
        self.apps = []
        self.spin = 0
        self.done = False
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
        elif cmd in ("playbook", "start"):
            path = PLAYBOOK if cmd == "playbook" else GETTING_STARTED
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as e:
                self.say(f"cannot read {path}: {e}", "e")
                self.say("")
                return
            Pager(self.scr, os.path.relpath(path, ROOT), text).run()
            self.scr.timeout(90)
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
            cands = sorted(a for a in self.apps if a.startswith(parts[-1]))
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

    def render(self):
        scr = self.scr
        self.h, self.w = scr.getmaxyx()
        scr.erase()
        apps = f" · {len(self.apps)} apps" if self.apps else ""
        put(scr, 0, 0, f" (o>  roost · {DOKKU} · {DOMAIN}{apps}", attr("b"))
        try:
            scr.hline(1, 0, curses.ACS_HLINE, self.w)
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
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            s = f" {frames[self.spin % len(frames)]} running: {self.runner.label} — ctrl+c to cancel"
        elif self.scroll:
            s = f" ── scrolled up {self.scroll} · pgdn to follow"
        else:
            s = " tab complete · up/down history · pgup/pgdn scroll · help · quit"
        put(scr, self.h - 2, 0, s, attr("d"))
        put(scr, self.h - 1, 0, " ❯ ", attr("p"))
        avail = max(1, self.w - 6)
        off = max(0, self.cur - avail + 1)
        put(scr, self.h - 1, 4, self.buf[off:off + avail], attr(""), avail)
        try:
            scr.move(self.h - 1, 4 + self.cur - off)
        except curses.error:
            pass
        scr.refresh()

    def handle(self, ch):
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
        elif ch == "\x04":                       # ctrl+d on empty line
            if not self.buf:
                self.done = True
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
    UI(scr).run()


if __name__ == "__main__":
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        sys.exit("roost ui needs an interactive terminal")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
