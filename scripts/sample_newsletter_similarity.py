"""Measure newsletter self-similarity across accounts, so a threshold can be calibrated (issue #1433).

#1284 gave the newsletter surface its body-history reader, so editions now carry a `similarity`
value instead of NULL. The corpus that produced that first reading was ten editions from ONE account
(0.684-0.828 body cosine, 0.372-0.711 title cosine), which is enough to say the dimension is high
and nothing like enough to pick a ceiling from — a newsletter has ONE subject by design, so a
threshold set on one account's editorial line would fire on normal repetition.

This is the sampler that produces the corpus a calibration needs. It reports, per measure:

  * the distribution of each edition's leave-one-out similarity — min / p25 / median / mean / p75 /
    p90 / max — for BODIES and for TITLES separately,
  * the same split per account, so a single account's editorial line is visible rather than
    averaged away,
  * where that distribution sits against the POST surface's shipped ceilings, which are the only
    calibrated reference points that exist (`POST_EMBEDDING_SIMILARITY_MAX` 0.78,
    `POST_SIMILARITY_MAX` 0.55) — reported as a reference, never applied as a verdict.

It refuses to imply a calibration it cannot support: under `MIN_EDITIONS` editions or under
`MIN_ACCOUNTS` accounts the report prints `NOT ENOUGH` and `sufficient_corpus` is false. It never
recommends a number — picking one is a human read of this report.

Read-only: no browser, no writes, no generation. It re-uses `db.get_recent_newsletter_bodies` /
`db.get_recent_newsletter_titles` and `content_quality.similarity_reports` rather than issuing SQL
or embedding calls of its own, so it measures exactly what the nightly pass measures. The ONE cost
is `similarity_reports`' batched `lem-embedding` call, one per account per surface; with the proxy
unreachable it degrades to token overlap and says so.

Run it where a database is reachable (prod credentials to see prod editions):

    poetry run python scripts/sample_newsletter_similarity.py
    poetry run python scripts/sample_newsletter_similarity.py --users 1,2 --json > sample.json

Output goes to stdout because the report IS the product of this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Mapping, Optional, Sequence

# Runnable from anywhere (the checkout's src/ is not on sys.path for a standalone script).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cqc_lem.utilities.ai.content_framework import (  # noqa: E402
    COMMENT_HISTORY_LIMIT,
    post_embedding_similarity_max,
    post_similarity_max,
)
from cqc_lem.utilities.content_quality import (  # noqa: E402
    MEASURE_EMBEDDING,
    MEASURE_LEXICAL,
    MEASURE_NONE,
    similarity_reports,
)

# A corpus smaller than this cannot support a threshold. Both floors matter and neither substitutes
# for the other: 20 editions from one account measure ONE editorial line, and two accounts with
# three editions each measure nothing at all.
MIN_EDITIONS = 20
MIN_ACCOUNTS = 2
# How many editions to read per account — the nightly pass's own window
# (`run_scheduler` reads `get_recent_newsletter_bodies(user_id, limit=COMMENT_HISTORY_LIMIT)`), and
# also the hard ceiling `similarity_reports` puts on the history pool. Reading MORE than this does
# not widen the measurement: the pool is truncated to the newest `COMMENT_HISTORY_LIMIT` editions
# and every older edition is then scored against a window the telemetry never used.
EDITIONS_PER_USER = COMMENT_HISTORY_LIMIT


def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile. No numpy dependency for a report that runs a few dozen numbers, and
    nearest-rank never invents a value between two editions that does not exist in the corpus.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    # `ceil`, not `round(...+0.5)`: Python rounds halves to EVEN, so the rank of a percentile that
    # lands exactly on a boundary (p25 of four editions) would depend on the parity of the corpus.
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return round(ordered[rank - 1], 4)


def distribution(scores: Sequence[float]) -> dict:
    """The shape of one set of similarity scores. Empty in, all-None out — never zeros, which would
    read as "measured, and perfectly unique".
    """
    values = [float(score) for score in scores]
    if not values:
        return {"sample": 0, "min": None, "p25": None, "median": None, "mean": None,
                "p75": None, "p90": None, "max": None}
    return {
        "sample": len(values),
        "min": round(min(values), 4),
        "p25": _percentile(values, 0.25),
        "median": _percentile(values, 0.5),
        "mean": round(sum(values) / len(values), 4),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.9),
        "max": round(max(values), 4),
    }


def measure_texts(texts: Sequence[str]) -> list:
    """Every text's leave-one-out similarity against the rest of the SAME account's editions.

    `similarity_reports(items, history=items)` drops an item's own text from its own history by
    normalized text, so passing one list twice is exactly the leave-one-out maximum the nightly pass
    records — the same function, so this report and the telemetry can never disagree.
    """
    texts = [str(text or "") for text in texts if str(text or "").strip()]
    if len(texts) < 2:
        # One edition has nothing to be similar to. Reporting it as 0.0 would be an invented
        # reading; the nightly pass records None for the same case.
        return []
    return [report for report in similarity_reports(texts, texts)
            if report.get("score") is not None]


def sample_user(user_id: int, limit: int = EDITIONS_PER_USER) -> dict:
    """Read one account's edition bodies + titles and measure both."""
    from cqc_lem.utilities.db import get_recent_newsletter_bodies, get_recent_newsletter_titles

    bodies = list(get_recent_newsletter_bodies(user_id, limit=limit) or [])
    titles = list(get_recent_newsletter_titles(user_id, limit=limit) or [])
    return {
        "user_id": user_id,
        "editions": len(bodies),
        "body": _readings(measure_texts(bodies)),
        "title": _readings(measure_texts(titles)),
    }


def _readings(reports: Sequence[Mapping[str, Any]]) -> dict:
    """Group one account's reports by the MEASURE that produced them.

    Cosine and token overlap are different scales (the post surface gates them at 0.78 and 0.55),
    so a run where one account's embedding call failed must not pool with the accounts whose did.
    """
    by_measure: dict = {}
    for report in reports:
        measure = str(report.get("measure") or MEASURE_NONE)
        by_measure.setdefault(measure, []).append(float(report["score"]))
    return {measure: scores for measure, scores in by_measure.items() if measure != MEASURE_NONE}


def summarize(per_user: Sequence[Mapping[str, Any]]) -> dict:
    """Fold the per-account readings into the distribution the calibration needs.

    `sufficient_corpus` is the load-bearing field: it is what stops this report being read as a
    calibration. It requires BOTH floors, counted on editions that were actually measurable (an
    account with a single edition contributes a corpus of zero comparisons, however many rows it
    has).
    """
    per_user = [dict(entry) for entry in per_user]
    editions = sum(int(entry.get("editions") or 0) for entry in per_user)
    measured_users = [entry for entry in per_user
                      if any((entry.get("body") or {}).values())]
    accounts = len(measured_users)
    surfaces: dict = {}
    for field in ("body", "title"):
        by_measure: dict = {}
        for entry in per_user:
            for measure, scores in (entry.get(field) or {}).items():
                by_measure.setdefault(measure, []).extend(scores)
        surfaces[field] = {measure: distribution(scores)
                           for measure, scores in by_measure.items()}
    return {
        "accounts_read": len(per_user),
        "accounts_with_a_comparison": accounts,
        "editions": editions,
        "min_editions": MIN_EDITIONS,
        "min_accounts": MIN_ACCOUNTS,
        "sufficient_corpus": editions >= MIN_EDITIONS and accounts >= MIN_ACCOUNTS,
        "surfaces": surfaces,
        # Reference only. These are the POST surface's calibrated ceilings; a newsletter sitting
        # above them is NOT thereby over a threshold — it is the reason this sampler exists.
        "post_reference": {MEASURE_EMBEDDING: post_embedding_similarity_max(),
                           MEASURE_LEXICAL: post_similarity_max()},
        "per_user": per_user,
    }


def _render_distribution(name: str, by_measure: Mapping[str, Any],
                         reference: Mapping[str, Any]) -> list:
    lines = [f"{name}"]
    if not by_measure:
        return lines + ["    (no comparable editions — an account needs two before anything "
                        "can be measured)"]
    for measure, shape in sorted(by_measure.items()):
        ceiling = reference.get(measure)
        lines.append(f"    {measure:<10} n={shape['sample']:<4} "
                     f"min={shape['min']} p25={shape['p25']} median={shape['median']} "
                     f"mean={shape['mean']} p75={shape['p75']} p90={shape['p90']} "
                     f"max={shape['max']}"
                     + (f"   (post ceiling {ceiling} — reference, not a verdict)"
                        if ceiling is not None else ""))
    return lines


def render(summary: Mapping[str, Any]) -> str:
    """The human report. Says what it cannot support before it says any number."""
    lines = ["Newsletter self-similarity sample (issue #1433)", ""]
    enough = summary["sufficient_corpus"]
    lines.append(f"Editions read             : {summary['editions']}"
                 + ("" if enough else f"  (need {summary['min_editions']}+)"))
    lines.append(f"Accounts with a comparison: {summary['accounts_with_a_comparison']}"
                 f" of {summary['accounts_read']} read"
                 + ("" if enough else f"  (need {summary['min_accounts']}+)"))
    if not enough:
        lines.append("")
        lines.append("NOT ENOUGH — this corpus cannot calibrate a threshold. The numbers below "
                     "describe it; they do not justify a ceiling.")
    lines.append("")
    for field, name in (("body", "Body self-similarity (leave-one-out max)"),
                        ("title", "Title self-similarity (leave-one-out max)")):
        lines += _render_distribution(name, summary["surfaces"].get(field) or {},
                                      summary["post_reference"])
        lines.append("")
    lines.append("Per account")
    for entry in summary["per_user"]:
        body = entry.get("body") or {}
        title = entry.get("title") or {}
        lines.append(f"    user {entry['user_id']:<5} editions={entry['editions']:<4} "
                     f"body={ {m: distribution(s)['mean'] for m, s in body.items()} } "
                     f"title={ {m: distribution(s)['mean'] for m, s in title.items()} }")
    return "\n".join(lines)


def collect(user_ids: Sequence[int], limit: int = EDITIONS_PER_USER) -> dict:
    """Read every listed account through the db facade and summarize."""
    return summarize([sample_user(user_id, limit=limit) for user_id in user_ids])


def _user_ids(raw: Optional[str]) -> list:
    if raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    from cqc_lem.utilities.db import get_active_user_ids

    return list(get_active_user_ids() or [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns 0 on a corpus too small to calibrate — "not enough yet" is the
    answer this issue expects most of the time, not a failure.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", help="comma-separated user ids (default: all active users)")
    parser.add_argument("--limit", type=int, default=EDITIONS_PER_USER,
                        help=f"editions to read per account (default and maximum: "
                             f"{EDITIONS_PER_USER} — the history pool `similarity_reports` compares "
                             f"against is capped there)")
    parser.add_argument("--json", action="store_true", help="emit the raw summary as JSON")
    args = parser.parse_args(argv)

    # Clamped, not honoured-then-truncated: a run asked for 500 editions would report 500 rows in
    # `editions` while every score came from a 50-edition window, which is the one way this report
    # could overstate the corpus it measured.
    summary = collect(_user_ids(args.users), limit=min(max(1, args.limit), EDITIONS_PER_USER))
    print(json.dumps(summary, indent=2, default=str) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
