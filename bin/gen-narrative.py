#!/usr/bin/env python3
"""gen-narrative.py — compose a board narrative from merged PRs.

The board's narrative/banner is the `roost status "<message>"` text, captured
per revision by the history collector. Left to a human it drifts: the auto-
collected tiles say "today" while the prose says days ago. This generates the
message from what actually merged, so CI can keep it current:

    roost status "$(bin/gen-narrative.py owner/repo --branch dev --since-days 1)"

Pure composition (compose_narrative) is separated from the gh call so it can be
tested without the network. Non-fatal by habit: on any gh failure it prints
nothing and exits 0, so a wrapper falls back to its default message.
"""
import argparse
import json
import subprocess
import sys


def compose_narrative(prs, date, branch, label=None):
    """A one-line narrative from merged PRs. `prs` is a list of dicts with
    `number` and `title`; newest first is fine — we sort by number ascending so
    the story reads in merge order. Returns "" when there's nothing to say."""
    if not prs:
        return ""
    who = f"{label} " if label else ""
    ordered = sorted(prs, key=lambda p: int(p["number"]))
    parts = [f"#{p['number']} {_short_title(p['title'])}" for p in ordered]
    n = len(ordered)
    return (f"{date}: {who}merged {n} PR{'s' if n != 1 else ''} to {branch} — "
            + "; ".join(parts) + ".")


def _short_title(title):
    """Trim a conventional-commit prefix and any trailing PR-number so the line
    reads as prose, not a changelog dump."""
    t = str(title).strip()
    for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:", "ci:"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):].strip()
            break
    # Drop a trailing " (#123)" the squash-merge appends.
    if t.endswith(")") and " (#" in t:
        t = t[:t.rindex(" (#")].strip()
    return t


def merged_prs(slug, branch, since_days):
    """Merged PRs into `branch` in the last `since_days`. Empty on any failure —
    the caller decides what to do with nothing."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "-R", slug, "--state", "merged", "--base", branch,
             "--search", f"merged:>={_since_date(since_days)}", "-L", "50",
             "--json", "number,title"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            print(f"gen-narrative: gh failed: {out.stderr.strip()[:200]}", file=sys.stderr)
            return []
        return json.loads(out.stdout or "[]")
    except Exception as e:  # noqa: BLE001 — non-fatal by contract
        print(f"gen-narrative: {e}", file=sys.stderr)
        return []


def _since_date(days):
    """ISO date `days` ago. Uses `date` so we don't depend on the tz of the
    caller's clock any more than gh already does."""
    out = subprocess.run(["date", "-v", f"-{int(days)}d", "+%Y-%m-%d"],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    # GNU date fallback.
    out = subprocess.run(["date", "-d", f"-{int(days)} days", "+%Y-%m-%d"],
                        capture_output=True, text=True)
    return out.stdout.strip()


def _today():
    return subprocess.run(["date", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="owner/repo")
    ap.add_argument("--branch", default="dev")
    ap.add_argument("--since-days", type=int, default=1)
    ap.add_argument("--label", default=None, help="project name prefix, e.g. Phoenix")
    args = ap.parse_args()

    prs = merged_prs(args.slug, args.branch, args.since_days)
    text = compose_narrative(prs, _today(), args.branch, args.label)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
