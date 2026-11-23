#!/usr/bin/env python3
"""Shell front for `lemd.codex_lane`, called by `lib/run_lane.sh`.

Subcommands:
  binary                    absolute Codex path, or exit 1
  classify <rc> <file>      failure class, or empty for rc=0
  family <harness> <model>  exit 0 when the pairing is allowed
  usage <file>              `tokens_in=N` / `tokens_out=N`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lemd import codex_lane  # noqa: E402


def _read(path: str) -> str:
    """The file's text, or empty when it is absent."""
    target = Path(path)
    return target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""


def main(argv: list[str]) -> int:
    """Dispatch one subcommand."""
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "binary":
        path = codex_lane.resolve_binary()
        if path:
            sys.stdout.write(path + "\n")
        return 0 if path else 1
    if cmd == "classify" and len(argv) == 4:
        sys.stdout.write(codex_lane.classify_failure(int(argv[2]), _read(argv[3])) + "\n")
        return 0
    if cmd == "family" and len(argv) == 4:
        return 0 if codex_lane.family_ok(argv[2], argv[3]) else 1
    if cmd == "usage" and len(argv) == 3:
        t_in, t_out = codex_lane.parse_usage(_read(argv[2]))
        sys.stdout.write(f"tokens_in={t_in}\ntokens_out={t_out}\n")
        return 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
