#!/usr/bin/env python3
"""Fail if CLAUDE.md exceeds the 40,000-character harness cap.

CLAUDE.md is the context window every Claude Code session loads. The 40k cap
is a hard ceiling on the SOURCE file (release-please does not rewrite it, so
the rule is enforceable). A regression silently bloats every future session
until the harness 413s, which is what this guard prevents.

Stdlib only — invoked from CI and from the optional pre-commit hook.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Cap is enforced on the FILE — not on the harness's input after slicing,
# not on a per-PR diff. The whole file goes into every session.
MAX_CHARS = 40_000

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "CLAUDE.md"


def main() -> int:
    if not TARGET.exists():
        print(f"error: {TARGET} not found", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    size = len(text)

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


if __name__ == "__main__":
    sys.exit(main())
