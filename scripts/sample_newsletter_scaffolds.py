"""Measure the canned-scaffold hit rate across SHIPPED newsletter editions (issue #1285).

#1142 added `NEWSLETTER_BANNED_SCAFFOLDS` and extended the `canned_scaffold` slop check to
newsletters, but the severity call needed a corpus that did not exist yet. This is the sampler that
produces it: it re-lints every published edition's stored body with the CURRENT list and reports

  * how many editions carry at least one scaffold (the hit rate the issue asks for),
  * which phrases actually fire and how often (so a dead entry can be spotted),
  * how the #630 telemetry has been scoring those same editions, and
  * CANDIDATE phrases — repeated word runs that are NOT yet banned and appear in two or more
    editions, which is the only provenance the list accepts (sampled from LEM's own output, never
    speculative).

Read-only: it opens no browser, writes nothing to the database, and calls no LLM. It re-uses
`db.get_shipped_content_for_quality` and `db.get_content_quality_scores` rather than issuing SQL of
its own, so it stays inside the repository seam.

Run it where a database is reachable (it needs prod credentials to see prod editions):

    poetry run python scripts/sample_newsletter_scaffolds.py --days 3650
    poetry run python scripts/sample_newsletter_scaffolds.py --users 1 --json > sample.json

Output goes to stdout because the report IS the product of this script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Iterable, Mapping, Optional, Sequence

# Runnable from anywhere (the checkout's src/ is not on sys.path for a standalone script).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.utilities.ai.content_framework import (  # noqa: E402
    NEWSLETTER_BANNED_SCAFFOLDS,
    POST_BANNED_SCAFFOLDS,
)
from cqc_lem.utilities.ai.slop_lint import (  # noqa: E402
    CHECK_SCAFFOLD,
    banned_scaffolds,
    check_severity,
    find_canned_scaffolds,
    lint_report,
)

SURFACE_NEWSLETTER = "newsletter"
# A corpus smaller than this cannot support a severity decision — the issue asks for 20+.
MIN_CORPUS = 20
# Candidate mining: word-run lengths to count, and how many DISTINCT editions a run must appear in
# before it is worth a human look. Two is deliberate — a phrase repeated across two independently
# generated editions is already a template, and the list's provenance rule wants evidence, not
# frequency.
CANDIDATE_NGRAMS: tuple = (4, 5, 6)
CANDIDATE_MIN_EDITIONS = 2
CANDIDATE_LIMIT = 25

_WORD_RE = re.compile(r"[a-z0-9']+")
_SMART_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def _words(text: Optional[str]) -> list:
    return _WORD_RE.findall(str(text or "").translate(_SMART_PUNCTUATION).lower())


def edition_report(edition: Mapping[str, Any]) -> dict:
    """Re-lint ONE stored edition body and report what the current lists find in it.

    `severity` is what the linter would say TODAY for this surface, so a run before and after a
    severity change is directly comparable.
    """
    body = str(edition.get("text") or "")
    hits = find_canned_scaffolds(body)
    report = lint_report(body, SURFACE_NEWSLETTER)
    violation = next((v for v in report.get("violations") or []
                      if v.get("check") == CHECK_SCAFFOLD), None)
    return {
        "ref_id": str(edition.get("ref_id") or ""),
        "user_id": edition.get("user_id"),
        "shipped_on": str(edition.get("shipped_on") or ""),
        "chars": len(body),
        "hits": hits,
        "newsletter_hits": [h for h in hits if h in NEWSLETTER_BANNED_SCAFFOLDS],
        "post_hits": [h for h in hits if h in POST_BANNED_SCAFFOLDS],
        "severity": (violation or {}).get("severity"),
        "lint_passes": bool(report.get("passes")),
    }


def candidate_phrases(bodies: Sequence[str]) -> list:
    """Repeated word runs that are NOT already banned, ranked by how many editions carry them.

    This is a shortlist for a human, never an auto-extension of the list: a run can repeat because
    it is the author's real vocabulary ("the billing importer"), which is the opposite of a
    scaffold. Longer runs are reported ahead of the shorter runs they contain, so a 6-word template
    does not arrive split into three 4-word fragments.
    """
    banned = tuple(banned_scaffolds())
    per_phrase: dict = {}
    for index, body in enumerate(bodies):
        words = _words(body)
        seen_here = set()
        for size in CANDIDATE_NGRAMS:
            for start in range(0, max(0, len(words) - size + 1)):
                phrase = " ".join(words[start:start + size])
                if phrase in seen_here or any(b in phrase or phrase in b for b in banned):
                    continue
                seen_here.add(phrase)
                per_phrase.setdefault(phrase, set()).add(index)
    ranked = [{"phrase": phrase, "editions": len(editions), "words": len(phrase.split())}
              for phrase, editions in per_phrase.items()
              if len(editions) >= CANDIDATE_MIN_EDITIONS]
    ranked.sort(key=lambda c: (-c["editions"], -c["words"], c["phrase"]))
    # Drop a shorter run fully contained in a longer one that was seen in the same number of
    # editions — it is the same template, reported twice.
    kept: list = []
    for candidate in ranked:
        if any(candidate["phrase"] in k["phrase"] and k["editions"] == candidate["editions"]
               for k in kept):
            continue
        kept.append(candidate)
    return kept[:CANDIDATE_LIMIT]


def summarize(editions: Iterable[Mapping[str, Any]], scores: Optional[Iterable[Mapping[str, Any]]] = None) -> dict:
    """Aggregate per-edition reports into the numbers the severity decision needs.

    `hit_rate` is the fraction of editions carrying at least one scaffold — the measure #1285 asks
    for. `sufficient` says whether the corpus is big enough to act on; a hit rate over three
    editions is an anecdote, and reporting it without that flag is how a calibration gets made up.
    """
    reports = [edition_report(e) for e in editions]
    total = len(reports)
    with_hits = [r for r in reports if r["hits"]]
    phrase_counts = Counter(h for r in reports for h in dict.fromkeys(r["hits"]))
    rows = [s for s in (scores or []) if str(s.get("surface") or "") == SURFACE_NEWSLETTER]
    return {
        "editions": total,
        "editions_with_scaffold": len(with_hits),
        "hit_rate": (len(with_hits) / total) if total else None,
        "sufficient_corpus": total >= MIN_CORPUS,
        "min_corpus": MIN_CORPUS,
        "current_severity": check_severity(CHECK_SCAFFOLD, SURFACE_NEWSLETTER),
        "would_hold": sum(1 for r in with_hits if r["severity"] == "hard"),
        "phrase_counts": dict(phrase_counts.most_common()),
        "unused_phrases": [p for p in NEWSLETTER_BANNED_SCAFFOLDS if p not in phrase_counts],
        "telemetry_rows": len(rows),
        "telemetry_with_hard": sum(1 for s in rows if (s.get("slop_hard") or 0) > 0),
        "telemetry_with_warn": sum(1 for s in rows if (s.get("slop_warn") or 0) > 0),
        "candidates": candidate_phrases([str(e.get("text") or "") for e in editions]),
        "per_edition": reports,
    }


def _render(summary: Mapping[str, Any]) -> str:
    lines = ["Newsletter canned-scaffold sample (issue #1285)", ""]
    total = summary["editions"]
    rate = summary["hit_rate"]
    lines.append(f"Editions sampled          : {total}"
                 + ("" if summary["sufficient_corpus"]
                    else f"  (NOT ENOUGH — {summary['min_corpus']}+ needed to calibrate)"))
    lines.append(f"Editions with a scaffold  : {summary['editions_with_scaffold']}"
                 + (f"  ({rate:.0%})" if rate is not None else ""))
    lines.append(f"Severity today (newsletter): {summary['current_severity']}"
                 f"  — {summary['would_hold']} edition(s) would be regenerated at HARD")
    lines.append(f"#630 telemetry rows       : {summary['telemetry_rows']} "
                 f"(hard>0: {summary['telemetry_with_hard']}, warn>0: {summary['telemetry_with_warn']})")
    lines.append("")
    lines.append("Phrase hits (phrase: editions carrying it)")
    for phrase, count in (summary["phrase_counts"] or {"— none —": 0}).items():
        lines.append(f"  {count:>4}  {phrase}")
    if summary["unused_phrases"]:
        lines.append("")
        lines.append("Banned newsletter phrases never seen in this corpus:")
        for phrase in summary["unused_phrases"]:
            lines.append(f"        {phrase}")
    lines.append("")
    lines.append("Candidate phrases (repeated, not yet banned — REVIEW before adding, a repeated "
                 "phrase can be the author's real vocabulary)")
    for candidate in summary["candidates"] or []:
        lines.append(f"  {candidate['editions']:>4}  {candidate['phrase']}")
    return "\n".join(lines)


def collect(user_ids: Sequence[int], days: int) -> dict:
    """Read every published edition and quality score for `user_ids` through the db facade."""
    from cqc_lem.utilities.db import get_content_quality_scores, get_shipped_content_for_quality

    editions: list = []
    scores: list = []
    for user_id in user_ids:
        for item in get_shipped_content_for_quality(user_id, days=days) or []:
            if str(item.get("surface") or "") == SURFACE_NEWSLETTER:
                editions.append({**item, "user_id": user_id})
        scores += list(get_content_quality_scores(user_id, days=days) or [])
    return summarize(editions, scores)


def _user_ids(raw: Optional[str]) -> list:
    if raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    from cqc_lem.utilities.db import get_active_user_ids

    return list(get_active_user_ids() or [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns 0 even on an empty corpus — "nothing shipped yet" is an answer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", help="comma-separated user ids (default: all active users)")
    parser.add_argument("--days", type=int, default=3650,
                        help="how far back to look (default: 3650, i.e. everything)")
    parser.add_argument("--json", action="store_true", help="emit the raw summary as JSON")
    args = parser.parse_args(argv)

    summary = collect(_user_ids(args.users), max(1, args.days))
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
