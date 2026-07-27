#!/usr/bin/env python3
"""Tests for bin/roostlib.py — the one shared ~/.roostrc reader.

Run:  python3 -m unittest discover -s tests   (from the roost root)
"""
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "roostlib", os.path.join(ROOT, "bin", "roostlib.py"))
roostlib = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roostlib)


class ReadRcTest(unittest.TestCase):
    def rc_file(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".roostrc", delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write(text)
        f.close()
        return f.name

    def test_parses_shell_style_lines(self):
        p = self.rc_file("# a comment\n"
                         "ROOST_DOMAIN=example.net\n"
                         "\n"
                         "SPACED = padded value \n"
                         "not a kv line\n")
        cfg = roostlib.read_rc(p)
        self.assertEqual(cfg["ROOST_DOMAIN"], "example.net")
        self.assertEqual(cfg["SPACED"], "padded value")
        self.assertEqual(len(cfg), 2)

    def test_strips_quotes_and_expands_vars(self):
        os.environ["ROOSTLIB_TEST_VAR"] = "expanded"
        self.addCleanup(os.environ.pop, "ROOSTLIB_TEST_VAR", None)
        p = self.rc_file('QUOTED="hello world"\n'
                         "EXPANDED=$ROOSTLIB_TEST_VAR/sub\n")
        cfg = roostlib.read_rc(p)
        self.assertEqual(cfg["QUOTED"], "hello world")
        self.assertEqual(cfg["EXPANDED"], "expanded/sub")

    def test_missing_file_is_empty_not_fatal(self):
        self.assertEqual(roostlib.read_rc("/nonexistent/roostrc"), {})

    def test_rc_precedence_file_then_defaults_then_arg(self):
        p = self.rc_file("ROOST_DOKKU_HOST=dokku@10.0.0.5\n")
        # File wins over the built-in default.
        self.assertEqual(roostlib.rc("ROOST_DOKKU_HOST", path=p),
                         "dokku@10.0.0.5")
        # Absent from file → built-in default (for the keys that have one).
        self.assertEqual(roostlib.rc("ROOST_DOMAIN", path=p),
                         roostlib.DEFAULTS["ROOST_DOMAIN"])
        # No default either → the caller's fallback.
        self.assertEqual(roostlib.rc("ROOST_PROJECTS_DIR", "~/repos", path=p),
                         "~/repos")

    def test_no_stale_cache(self):
        # The ui's config tab re-reads after edits; the reader must not cache.
        p = self.rc_file("K=one\n")
        self.assertEqual(roostlib.read_rc(p)["K"], "one")
        with open(p, "w") as f:
            f.write("K=two\n")
        self.assertEqual(roostlib.read_rc(p)["K"], "two")


if __name__ == "__main__":
    unittest.main()
