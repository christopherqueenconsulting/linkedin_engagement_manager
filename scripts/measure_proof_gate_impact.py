"""Measure what tightening the A2 proof detector (issue #1266) costs in regenerations.

The change stops a spelled quantity acting as a determiner ("one of the biggest challenges",
"dozens of our customers") from counting as concrete specificity. Every draft whose ONLY proof was
that shape now fails `has_first_person_proof`, and `_review_generated_post` spends one extra
`lem-complex` generation on it — so the flip rate IS the cost, and this script measures it against
SHIPPED post bodies instead of assuming it.

Read-only: it opens no browser, writes nothing, and calls no LLM. It re-uses
`db.get_shipped_content_for_quality` (the #630 nightly scorer's own reader) rather than issuing SQL
of its own, so it stays inside the repository seam. Run it where a database is reachable — an agent
worktree has no production credentials, which is the same limit `docs/content-quality-audits/text.md`
§1 records:

    poetry run python scripts/measure_proof_gate_impact.py --days 365
    poetry run python scripts/measure_proof_gate_impact.py --users 1 --show-flips

On the production VPS the credentials live in the app containers, not on the host, and `scripts/`
is deliberately NOT baked into the image (`compose/local/Dockerfile` copies `src/` only) — so the
production run pipes this file into a worker that already has the database environment:

    cd /opt/lem && sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml \
      exec -T celery_worker python - --days 365 --show-flips \
      < scripts/measure_proof_gate_impact.py

That is why the `src/` bootstrap below is conditional: read from stdin the interpreter reports
`__file__` as `<stdin>`, and `cqc_lem` is already installed in the image's venv.

Output goes to stdout because the report IS the product of this script.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional


def _bootstrap_src_path(script_path: str) -> Optional[str]:
    """Put the checkout's `src/` on `sys.path`, and return what was added.

    A standalone script does not get its repository root on `sys.path`. Piped into a container
    (`python - < scripts/…`) there is no script path at all — `__file__` is `<stdin>` — and the
    directory that would be derived from it points at nothing, so the guess is skipped rather than
    inserted: the image installs `cqc_lem` into its venv, and a made-up path is dead weight on
    `sys.path` at best and shadows that install at worst.
    """
    if not script_path or script_path.startswith("<"):
        return None
    src = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(script_path)), "..", "src"))
    if not os.path.isdir(src):
        return None
    sys.path.insert(0, src)
    return src


_bootstrap_src_path(globals().get("__file__") or "")

# Operator CLI, not the app: its documented "no database reachable here" failure must not file
# a production error-tracking issue (#1661, see `logger.telemetry_muted`). Set BEFORE cqc_lem is
# imported — the PostHog Logs handler is built at import time.
os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")

from cqc_lem.utilities.ai.content_framework import (  # noqa: E402
    _FIRST_PERSON_RE,
    _PROOF_MONTHS,
    _PROOF_SENTENCE_SPLIT,
    _PROOF_WEEKDAYS,
    has_first_person_proof,
)

SURFACE_POST = "post"

# The pre-#1266 specificity regex, kept here rather than in the module so the shipped detector stays
# ONE regex and the comparison is against what production actually ran.
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


def legacy_proof_sentences(text: Optional[str]) -> list:
    """The sentences the PRE-#1266 detector counted as first-person proof."""
    out = []
    for sentence in _PROOF_SENTENCE_SPLIT.split(text or ""):
        s = sentence.strip()
        if s and _FIRST_PERSON_RE.search(s) and _LEGACY_SPECIFICITY_RE.search(s):
            out.append(s)
    return out


def measure(posts: list) -> dict:
    """Flip counts for one corpus of `{ref_id, text}` rows.

    A flip is a post that HAD proof under the old detector and has none under the new one — that is
    the post that now costs one extra generation. A post that never had proof was already being
    regenerated and is not part of the change's cost.
    """
    rows = [{"ref_id": (p or {}).get("ref_id"), "text": (p or {}).get("text") or ""}
            for p in posts or []]
    had = [r for r in rows if legacy_proof_sentences(r["text"])]
    flipped = [r for r in had if not has_first_person_proof(r["text"])]
    return {
        "posts": len(rows),
        "had_proof": len(had),
        "flipped": len(flipped),
        "flip_rate": round(100.0 * len(flipped) / len(had), 1) if had else 0.0,
        "flips": [{"ref_id": r["ref_id"], "was_proof": legacy_proof_sentences(r["text"])}
                  for r in flipped],
    }


def collect(user_ids: list, days: int) -> list:
    """Shipped POST bodies for the given users, newest first, as `{ref_id, text}` rows."""
    from cqc_lem.utilities.db import get_shipped_content_for_quality

    rows = []
    for user_id in user_ids:
        for item in get_shipped_content_for_quality(user_id, days=days) or []:
            if str(item.get("surface") or "") == SURFACE_POST:
                rows.append({"ref_id": item.get("ref_id"), "text": item.get("text") or "",
                             "user_id": user_id})
    return rows


def _user_ids(raw: Optional[str]) -> list:
    """The users whose shipped posts are read (default: every user who could have shipped one).

    `get_active_user_ids` requires an UNEXPIRED LinkedIn token, which is a question about today and
    not about the bodies that shipped over the last year — an account whose 60-day authorization
    lapsed still has the posts this measures. So an empty active set falls back to the token-holder
    list rather than reporting "nothing was measured" for a reason that has nothing to do with the
    corpus.
    """
    if raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    from cqc_lem.utilities.db import get_active_user_ids, get_linkedin_token_user_ids

    return list(get_active_user_ids() or []) or list(get_linkedin_token_user_ids() or [])


def main() -> int:
    """Print the flip rate over shipped posts; returns a shell exit code."""
    parser = argparse.ArgumentParser(description="Measure the #1266 proof-detector flip rate.")
    parser.add_argument("--users", default=None,
                        help="Comma-separated user ids (default: every active user).")
    parser.add_argument("--days", type=int, default=365,
                        help="How far back to read shipped posts (default 365).")
    parser.add_argument("--show-flips", action="store_true",
                        help="Print the sentence that used to satisfy the gate for each flip.")
    args = parser.parse_args()

    result = measure(collect(_user_ids(args.users), args.days))
    print(f"{result['posts']} shipped posts read, {result['had_proof']} had proof under the old "
          f"detector, {result['flipped']} flip to NO proof "
          f"({result['flip_rate']}% of previously-proven posts) — one extra lem-complex generation "
          f"each")
    if args.show_flips:
        for flip in result["flips"]:
            print(f"  post {flip['ref_id']}:")
            for sentence in flip["was_proof"]:
                print(f"    - {sentence[:160]}")
    if not result["posts"]:
        # Exit non-zero: an empty corpus prints a perfectly well-formed "0.0%" that reads like a
        # measured answer, and the reader who most needs to notice is the one who ran this in the
        # wrong place (no credentials, or `--users` naming an account with nothing shipped).
        print("No shipped posts read — nothing was measured. Run this where production credentials "
              "are available, and check --users/--days.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
