#!/usr/bin/env python3
"""Measure what tightening the A2 proof detector (issue #1266) costs in regenerations.

The change stops a spelled quantity acting as a determiner ("one of the biggest challenges",
"dozens of our customers") from counting as concrete specificity. Every post whose ONLY proof was
that shape now fails `has_first_person_proof`, and `_review_generated_post` spends one extra
`lem-complex` generation on it — so the flip rate IS the cost.

This script measures the flip rate against real shipped post bodies instead of assuming it. It is
READ-ONLY: it reads post content through the existing `db.get_recent_post_texts` reader and writes
nothing. Run it where production credentials already live (the app container), never from an agent
worktree::

    python scripts/measure_proof_gate_impact.py --limit 50
    python scripts/measure_proof_gate_impact.py --user-id 1 --show-flips

Output is a per-user and total count of posts that flip from "has proof" to "no proof", plus (with
`--show-flips`) the sentence that used to carry the post so a reader can judge whether the old
verdict was ever real proof.
"""

from __future__ import annotations

import argparse
import re
import sys

from cqc_lem.utilities.ai.content_framework import (
    _FIRST_PERSON_RE,
    _PROOF_MONTHS,
    _PROOF_SENTENCE_SPLIT,
    _PROOF_WEEKDAYS,
    has_first_person_proof,
)

# The pre-#1266 specificity regex, kept here (not in the module) so the comparison is against what
# production actually ran and the shipped module carries only ONE detector.
_LEGACY_SPECIFICITY_RE = re.compile(
    r"\d"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"dozens?|hundreds?|thousands?|millions?|billions?)\b"
    r"|\b(?:years?|months?|weeks?|weekends?|days?|hours?|decades?)\s+ago\b"
    r"|\b(?:last|past|next|first|second|third)\s+"
    r"(?:year|month|week|weekend|quarter|decade|time|day|night|morning)\b"
    r"|\bback\s+in\b"
    r"|\[\[[^\]]+\]\]"
    rf"|\b(?:{_PROOF_MONTHS})\b"
    rf"|\b(?:{_PROOF_WEEKDAYS})\b",
    re.IGNORECASE)


def legacy_proof_sentences(text: str) -> list:
    """The sentences the PRE-#1266 detector counted as first-person proof."""
    out = []
    for sentence in _PROOF_SENTENCE_SPLIT.split(text or ""):
        s = sentence.strip()
        if s and _FIRST_PERSON_RE.search(s) and _LEGACY_SPECIFICITY_RE.search(s):
            out.append(s)
    return out


def measure(texts: list) -> dict:
    """Flip counts for one corpus: how many posts had proof before and lose it under the change."""
    had = [t for t in texts if legacy_proof_sentences(t)]
    flipped = [t for t in had if not has_first_person_proof(t)]
    return {"posts": len(texts), "had_proof": len(had), "flipped": len(flipped),
            "flipped_texts": flipped}


def _user_ids(explicit: int = None) -> list:
    from cqc_lem.utilities.db import get_active_user_ids

    return [explicit] if explicit else list(get_active_user_ids() or [])


def main() -> int:
    """Print the flip rate per user and in total; returns a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, default=None,
                        help="Measure one user instead of every active user.")
    parser.add_argument("--limit", type=int, default=50,
                        help="Most-recent posts to read per user (default 50).")
    parser.add_argument("--show-flips", action="store_true",
                        help="Print the sentence that used to satisfy the gate for each flip.")
    args = parser.parse_args()

    from cqc_lem.utilities.db import get_recent_post_texts

    totals = {"posts": 0, "had_proof": 0, "flipped": 0}
    for user_id in _user_ids(args.user_id):
        result = measure(get_recent_post_texts(user_id, limit=args.limit))
        for key in totals:
            totals[key] += result[key]
        print(f"user {user_id}: {result['posts']} posts, {result['had_proof']} had proof, "
              f"{result['flipped']} flip to NO proof")
        if args.show_flips:
            for text in result["flipped_texts"]:
                for sentence in legacy_proof_sentences(text):
                    print(f"    - {sentence[:160]}")

    rate = (100.0 * totals["flipped"] / totals["had_proof"]) if totals["had_proof"] else 0.0
    print(f"TOTAL: {totals['posts']} posts, {totals['had_proof']} had proof, "
          f"{totals['flipped']} flip ({rate:.1f}% of previously-proven posts) — "
          f"one extra lem-complex generation each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
