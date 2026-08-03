#!/usr/bin/env python3
"""Fail if CLAUDE.md exceeds the 40,000-character harness cap.

CLAUDE.md is the context window every Claude Code session loads. The 40k cap
is a hard ceiling on the SOURCE file (release-please does not rewrite it, so
the rule is enforceable). A regression silently bloats every future session
until the harness 413s, which is what this guard prevents.

Two invocation shapes (issue #1000):

* Default (no flags) — the original strict check: print the size, exit 1
  over the cap. Used by the PR gate and for a local pre-push check.
* `--warn-at N` — soft mode for the `main`-push drift watch: never fails the
  build (a docs-cap regression on `main` shouldn't redden the branch), instead
  writes `status`/`size` to `$GITHUB_OUTPUT` so the calling workflow can file
  or update a tracking issue once the file crosses the warn threshold, well
  before it reaches the hard cap that would block the next unrelated PR.

`--baseline-ref REF` additionally compares against `CLAUDE.md` at another git
ref (e.g. a PR's base branch), so a PR that inherits an already-over-cap
`main` can tell that apart from one that pushed it over itself.

Stdlib only — invoked from CI and can be run locally before pushing.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Cap is enforced on the FILE — not on the harness's input after slicing,
# not on a per-PR diff. The whole file goes into every session.
MAX_CHARS = 40_000

# Runway before the hard cap, so drift is visible while there's still room to
# trim deliberately instead of only finding out when a PR goes red.
DEFAULT_WARN_CHARS = 38_000

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "CLAUDE.md"


def _read_size() -> Optional[int]:
    if not TARGET.exists():
        print(f"error: {TARGET} not found", file=sys.stderr)
        return None
    return len(TARGET.read_text(encoding="utf-8"))


def _baseline_size(ref: str) -> Optional[int]:
    """Size of CLAUDE.md at `ref`, or None if it can't be read (missing ref,
    file didn't exist there yet, etc). Never raises — a baseline comparison
    is informational, not a reason to fail the check."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:CLAUDE.md"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return len(result.stdout)


def _report_baseline(ref: str, baseline: int, size: int) -> None:
    if baseline > MAX_CHARS and size > MAX_CHARS:
        delta = size - baseline
        sign = "+" if delta >= 0 else ""
        print(
            f"note: CLAUDE.md was already {baseline:,} chars (over the {MAX_CHARS:,} cap) "
            f"on {ref} — this diff changed it by {sign}{delta:,} chars (now {size:,}). "
            f"The overage is inherited, not caused by this diff."
        )
    elif baseline <= MAX_CHARS < size:
        print(
            f"note: this diff pushed CLAUDE.md over the {MAX_CHARS:,}-char cap "
            f"({ref}: {baseline:,} chars, now {size:,})."
        )


def _write_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def _strict_check(size: int) -> int:
    if size > MAX_CHARS:
        print(
            f"error: CLAUDE.md is {size:,} chars (cap: {MAX_CHARS:,}). "
            f"Move detail to docs/*.md and leave the map + invariants here. "
            f"Run `wc -c CLAUDE.md` to see the current size.",
            file=sys.stderr,
        )
        return 1

    print(f"ok: CLAUDE.md is {size:,} / {MAX_CHARS:,} chars")
    return 0


def _soft_check(size: int, warn_at: int) -> int:
    if size > MAX_CHARS:
        status = "over"
        print(f"::warning::CLAUDE.md is {size:,} chars — OVER the {MAX_CHARS:,}-char cap on main.")
    elif size >= warn_at:
        status = "warn"
        print(
            f"::warning::CLAUDE.md is {size:,} chars — within {MAX_CHARS - size:,} of the "
            f"{MAX_CHARS:,}-char cap on main (warn threshold: {warn_at:,})."
        )
    else:
        status = "ok"
        print(f"ok: CLAUDE.md is {size:,} / {MAX_CHARS:,} chars (warn at {warn_at:,})")

    _write_output("status", status)
    _write_output("size", str(size))
    # Never fails the build — this is the early-warning path, not the gate.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-at", type=int, nargs="?", const=DEFAULT_WARN_CHARS, default=None,
        help=f"soft-warn threshold (default {DEFAULT_WARN_CHARS:,} when passed with no value); "
             f"never fails the build, writes status/size to $GITHUB_OUTPUT",
    )
    parser.add_argument(
        "--baseline-ref", default=None,
        help="git ref (e.g. a PR's base branch) to compare against, to tell an inherited "
             "overage apart from one this diff caused",
    )
    args = parser.parse_args()

    size = _read_size()
    if size is None:
        return 1

    if args.baseline_ref:
        baseline = _baseline_size(args.baseline_ref)
        if baseline is not None:
            _report_baseline(args.baseline_ref, baseline, size)
        else:
            print(f"note: could not read CLAUDE.md at {args.baseline_ref} — skipping "
                  f"inherited/caused comparison.", file=sys.stderr)

    if args.warn_at is not None:
        return _soft_check(size, args.warn_at)

    return _strict_check(size)


if __name__ == "__main__":
    sys.exit(main())
