"""Read the slop-lint retry clear-rate off the `slop_retry` event (issue #1530).

#1434 shipped the instrument because the number cannot be recovered from finished drafts: a stored
edition keeps only the checks that were STILL firing when the budget ran out, and LiteLLM redacted
the intermediate draft (`turn_off_message_logging: true` until 2026-09-01), so a retry that cleared
`contrastive_frame` and came back with `banned_lexicon` is indistinguishable from one that never
moved. This script is the reading half — the Verifier #1530 names.

The one formula guard, from #1434's adversarial review: the newsletter loop shares its single
regeneration with the structural floor (#1435), so a slop-clean edition that is too short spends a
retry with no HARD check to fix. Those rows grade `unsteered` and are EXCLUDED from the clear-rate
denominator; folding them in would inflate exactly the number this exists to read. Their share is
reported on its own, because it is how much of the budget the other grader is spending.

`traded` is reported apart from `persisted` for the same reason it is graded apart: they point at
different fixes. `traded` dominating means no attempt budget helps — the retry directive has to edit
the offending sentences instead of re-authoring the whole draft.

Read-only: it issues one HogQL query and writes nothing. It needs a PostHog personal API key with
`query:read` — `POSTHOG_OPERATOR_API_KEY`, falling back to `POSTHOG_PERSONAL_API_KEY`
(`posthog_keys.py` owns the precedence) — which an agent worktree does not have; run it where one is
available, or paste the SQL from `--print-sql` into the PostHog UI:

    POSTHOG_OPERATOR_API_KEY=phx_… poetry run python scripts/slop_retry_clear_rate.py --days 30
    poetry run python scripts/slop_retry_clear_rate.py --print-sql

Output goes to stdout because the report IS the product of this script. Exit 1 when the window holds
fewer steered rows than `--min-rows`: an empty window prints a perfectly well-formed `0.0%` that
reads like a measured answer, and the reader who most needs to notice is the one who ran it before
the editions existed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Optional

# Reached by path, not by installation: this is run by hand from a checkout, the same way
# posthog_key_check.py reaches the resolver. posthog_keys.py is stdlib-only, so this costs nothing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cqc_lem.utilities.posthog_keys import missing_key_message, operator_api_key  # noqa: E402

DEFAULT_APP_HOST = "https://us.posthog.com"
DEFAULT_PROJECT_ID = "475262"  # "CQC LEM"
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 10000
QUERY_TIMEOUT_SECONDS = 60

EVENT = "slop_retry"
# The five graded outcomes. `unsteered` is deliberately NOT here — it is the excluded sixth.
GRADED_OUTCOMES = ("cleared", "traded", "worsened", "persisted", "lost")
UNSTEERED = "unsteered"
_SURFACE_RE = re.compile(r"[A-Za-z0-9_-]+")

# Below this many steered rows the report states the sample and refuses to read a rate off it.
# Newsletter editions are authored in small bursts, not weekly — five drafting days in the 45 to
# 2026-08-17, 1-6 editions each (F10, second read) — so a fortnight is single digits and only the
# editions that trip a HARD check are steered at all. The floor is what stops a two-edition window
# being quoted as a clear-rate.
DEFAULT_MIN_ROWS = 10


def build_query(days: int = DEFAULT_DAYS, surface: Optional[str] = None,
                limit: int = DEFAULT_LIMIT) -> str:
    """The HogQL behind one run — the raw retry rows, newest first.

    Aggregation is done in Python rather than in SQL so the clear-rate formula (and its `unsteered`
    exclusion) is unit-testable without a PostHog round trip.

    `--surface` is the one value that reaches the query as text rather than as an int, so it is
    validated instead of quoted: a surface name is an identifier (`newsletter`, `post_image`), and
    anything else is a typo the reader should see here rather than as a HogQL parse error — or as a
    predicate that silently means something other than what was typed.

    Raises:
        ValueError: if `surface` is not a bare identifier.
    """
    where = [f"event = '{EVENT}'", f"timestamp > now() - INTERVAL {int(days)} DAY"]
    if surface:
        if not _SURFACE_RE.fullmatch(surface):
            raise ValueError(f"surface must be a bare identifier, got {surface!r}")
        where.append(f"properties.surface = '{surface}'")
    return (
        "SELECT properties.surface AS surface, properties.outcome AS outcome, "
        "properties.kept AS kept, properties.hard_before AS hard_before, "
        "properties.hard_after AS hard_after, properties.attempt AS attempt, "
        "properties.max_attempts AS max_attempts, properties.before_checks AS before_checks, "
        "properties.after_checks AS after_checks, timestamp\n"
        f"FROM events\nWHERE {' AND '.join(where)}\n"
        f"ORDER BY timestamp DESC\nLIMIT {int(limit)}"
    )


def _as_int(value) -> Optional[int]:
    """An int property that survives PostHog handing it back as a string, or None if unreadable."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> bool:
    """`kept` rides as a `label()`, so it ingests as the STRING 'True' rather than a boolean."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1")


def _as_list(value) -> list:
    """`before_checks` / `after_checks` come back as a list, or as its JSON text."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return _as_list(json.loads(value))
        except ValueError:
            return []
    return []


def parse_rows(results: Optional[list]) -> list:
    """PostHog result tuples → the row dicts `measure` reads."""
    rows = []
    for result in results or []:
        if isinstance(result, dict):
            row = dict(result)
        else:
            keys = ("surface", "outcome", "kept", "hard_before", "hard_after", "attempt",
                    "max_attempts", "before_checks", "after_checks", "timestamp")
            row = dict(zip(keys, list(result) + [None] * len(keys)))
        rows.append({
            "surface": str(row.get("surface") or "unknown"),
            "outcome": str(row.get("outcome") or "").strip().lower(),
            "kept": _as_bool(row.get("kept")),
            "hard_before": _as_int(row.get("hard_before")),
            "hard_after": _as_int(row.get("hard_after")),
            "attempt": _as_int(row.get("attempt")),
            "max_attempts": _as_int(row.get("max_attempts")),
            "before_checks": _as_list(row.get("before_checks")),
            "after_checks": _as_list(row.get("after_checks")),
            "timestamp": row.get("timestamp"),
        })
    return rows


def _rate(part: int, whole: int) -> Optional[float]:
    """A percentage, or None when there is no denominator — never a 0.0 standing in for silence."""
    return round(100.0 * part / whole, 1) if whole else None


def _tally(rows: list) -> dict:
    """Outcome counts and the rates read off them, for one set of rows.

    `steered` is the denominator #1434's review fixed: the five graded outcomes, `unsteered`
    excluded. `unsteered_share` is stated against ALL rows, because the question it answers is what
    fraction of the regeneration budget the OTHER grader is spending.

    An outcome this script does not know (a sixth verdict added to `slop_lint.retry_outcome`, a
    property that failed to ingest) counts as `unrecognised` and is REPORTED. It cannot be folded
    into either denominator without deciding what it means, and dropping it silently would shrink
    the denominator the clear-rate is read off — i.e. raise the number — with nothing on screen
    saying so.
    """
    counts = {name: 0 for name in GRADED_OUTCOMES}
    counts[UNSTEERED] = 0
    kept = 0
    unrecognised: dict = {}
    graded_without_hard_before = 0
    for row in rows:
        outcome = row["outcome"]
        if outcome not in counts:
            unrecognised[outcome] = unrecognised.get(outcome, 0) + 1
            continue
        counts[outcome] += 1
        if outcome != UNSTEERED:
            if row["kept"]:
                kept += 1
            if row["hard_before"] == 0:
                # `lost` (the regeneration returned nothing, so there is no `after` report to grade
                # against a clean `before`) or a `worsened` row whose draft was clean going in.
                # Reported so the outcome-based denominator and the `hard_before > 0` one can be
                # reconciled rather than argued about. An UNREADABLE count is None, not 0, and is
                # deliberately not counted here — it is not evidence of a clean draft.
                graded_without_hard_before += 1
    steered = sum(counts.get(name, 0) for name in GRADED_OUTCOMES)
    return {
        "rows": len(rows),
        "steered": steered,
        "outcomes": counts,
        "unrecognised": unrecognised,
        "clear_rate": _rate(counts["cleared"], steered),
        "traded_share": _rate(counts["traded"], steered),
        "persisted_share": _rate(counts["persisted"], steered),
        "worsened_share": _rate(counts["worsened"], steered),
        "lost_share": _rate(counts["lost"], steered),
        "unsteered": counts[UNSTEERED],
        "unsteered_share": _rate(counts[UNSTEERED], len(rows)),
        "kept_share": _rate(kept, steered),
        "graded_without_hard_before": graded_without_hard_before,
    }


def measure(rows: list) -> dict:
    """The whole reading: overall, per surface, and per HARD check the retry was steered on.

    A row is counted under EVERY check it carried going in, so the per-check shares do not sum to
    the overall one — a draft tripping two checks is one regeneration steered on both, and the
    question each row answers ("does a retry clear THIS check?") is per check.
    """
    rows = list(rows or [])
    result = _tally(rows)
    surfaces = {}
    for surface in sorted({row["surface"] for row in rows}):
        surfaces[surface] = _tally([row for row in rows if row["surface"] == surface])
    checks = {}
    for check in sorted({name for row in rows for name in row["before_checks"]}):
        checks[check] = _tally([row for row in rows if check in row["before_checks"]])
    result["by_surface"] = surfaces
    result["by_check"] = checks
    return result


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value}%"


def _line(name: str, tally: dict, min_rows: int = DEFAULT_MIN_ROWS) -> str:
    """One breakdown row — COUNTS below the floor, rates only once the sample earns them.

    A per-check split divides an already-small sample further, so this is where a "100.0%" read off
    one regeneration would otherwise appear.
    """
    counts = tally["outcomes"]
    tail = (f"cleared {_pct(tally['clear_rate']):>7}  traded {_pct(tally['traded_share']):>7}  "
            f"persisted {_pct(tally['persisted_share']):>7}"
            if tally["steered"] >= min_rows else
            f"cleared {counts['cleared']}  traded {counts['traded']}  "
            f"persisted {counts['persisted']}  (under the {min_rows}-row floor: counts only)")
    return (f"  {name:<22} steered {tally['steered']:>4}  {tail}"
            f"  worsened {counts['worsened']}  lost {counts['lost']}")


def format_report(result: dict, days: int = DEFAULT_DAYS,
                  min_rows: int = DEFAULT_MIN_ROWS, limit: Optional[int] = None) -> str:
    """The stdout report — the sample and the window first, so no rate is quoted without them.

    `limit` is the row cap the query ran with: a window that filled it was TRUNCATED to its newest
    rows, and the rate below is then read off a slice of the window the header claims. Said out
    loud rather than left for the reader to infer from a round number.
    """
    lines = [
        f"slop_retry — {result['rows']} rows over the last {days} days "
        f"({result['steered']} steered, {result['unsteered']} unsteered — "
        "a regeneration the structural floor spent with no HARD slop check to fix)",
    ]
    if result["steered"] < min_rows:
        lines.append(f"  NOT ENOUGH DATA — {result['steered']} steered rows, floor is {min_rows}. "
                     "No rate is stated off this sample; the counts are "
                     + ", ".join(f"{name} {result['outcomes'][name]}" for name in GRADED_OUTCOMES)
                     + ".")
    else:
        lines.append(f"  clear-rate {_pct(result['clear_rate'])} "
                     f"(cleared {result['outcomes']['cleared']}/{result['steered']}), "
                     f"traded {_pct(result['traded_share'])}, "
                     f"persisted {_pct(result['persisted_share'])}, "
                     f"worsened {_pct(result['worsened_share'])}, "
                     f"lost {_pct(result['lost_share'])}; "
                     f"kept {_pct(result['kept_share'])}; "
                     f"unsteered {_pct(result['unsteered_share'])} of all rows")
    if limit and result["rows"] >= limit:
        lines.append(f"  TRUNCATED — the query returned its {limit}-row cap, so the window holds "
                     "more rows than were read and every rate above is off its newest slice. "
                     "Re-run with a larger --limit or a shorter --days.")
    if result.get("unrecognised"):
        lines.append("  UNRECOGNISED OUTCOMES — counted in neither denominator, so the rates above "
                     "are read off the rest: "
                     + ", ".join(f"{name or '(empty)'} {count}"
                                 for name, count in sorted(result["unrecognised"].items())) + ".")
    if result["graded_without_hard_before"]:
        lines.append(f"  ({result['graded_without_hard_before']} graded rows carried no HARD check "
                     "going in — a regeneration that returned nothing, or one whose draft was "
                     "clean going in and came back worse)")
    if result["by_surface"]:
        lines.append("by surface:")
        lines += [_line(name, tally, min_rows) for name, tally in result["by_surface"].items()]
    if result["by_check"]:
        lines.append("by HARD check steered on (a row counts under every check it carried):")
        lines += [_line(name, tally, min_rows) for name, tally in result["by_check"].items()]
    return "\n".join(lines)


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

class PostHogQueryClient:
    """Minimal client for the HogQL query endpoint — one POST, no state."""

    def __init__(self, api_key: str, project_id: str, app_host: str = DEFAULT_APP_HOST) -> None:
        self.api_key = api_key
        self.project_id = project_id
        self.app_host = app_host.rstrip("/")

    def query(self, hogql: str) -> list:
        """Run one HogQL query and return its result rows."""
        import requests
        response = requests.post(
            f"{self.app_host}/api/projects/{self.project_id}/query/",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("results") or []


def main(argv: Optional[list] = None) -> int:
    """Print the clear-rate report; returns a shell exit code."""
    parser = argparse.ArgumentParser(description="Read the slop-lint retry clear-rate (#1530).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Window in days (default {DEFAULT_DAYS}).")
    parser.add_argument("--surface", default=None,
                        help="Read one surface only (newsletter, post, comment, …).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Row cap on the query (default {DEFAULT_LIMIT}).")
    parser.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS,
                        help=f"Steered rows required before a rate is stated (default "
                             f"{DEFAULT_MIN_ROWS}).")
    parser.add_argument("--print-sql", action="store_true",
                        help="Print the HogQL and exit (no network, no key needed).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the reading as JSON instead of the report.")
    args = parser.parse_args(argv)

    try:
        hogql = build_query(days=args.days, surface=args.surface, limit=args.limit)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.print_sql:
        print(hogql)
        return 0

    api_key = operator_api_key()
    if not api_key:
        print(f"{missing_key_message('operator')} — nothing was measured. Run this where the key "
              "is available, or use --print-sql and run the query in the PostHog UI.")
        return 1
    client = PostHogQueryClient(api_key, os.getenv("POSTHOG_PROJECT_ID", DEFAULT_PROJECT_ID),
                                os.getenv("POSTHOG_APP_HOST", DEFAULT_APP_HOST))
    result = measure(parse_rows(client.query(hogql)))
    print(json.dumps(result, indent=2) if args.as_json
          else format_report(result, days=args.days, min_rows=args.min_rows, limit=args.limit))
    return 0 if result["steered"] >= args.min_rows else 1


if __name__ == "__main__":
    sys.exit(main())
