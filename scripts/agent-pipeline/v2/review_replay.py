#!/usr/bin/env python3
"""Review-quality replay report (#2562, acceptance box 4): is Codex good enough to review?

The replay itself is a live model run, so it is owner-run; this module is the part that turns the
recorded runs into the numbers the owner decides on. It reads two files and states numbers only. The
recall bar for Codex to become the merge-gate reviewer is the owner's, set in a PR comment, so this
tool never prints a pass/fail.

    ./review_replay.py seeds.json results.ndjson [--baseline claude-sonnet-5-5] [--today-mix mix.json]

``seeds.json`` maps a PR number to what is known about it::

    {"2522": {"defects": ["d1", "d2"], "risk_high": false},
     "2600": {"clean": true}}

A PR with neither ``defects`` nor ``clean: true`` is refused: an empty defect list would silently
score every reviewer as perfect on it.

``results.ndjson`` has one row per (PR, model) review::

    {"run_id": "r1", "pr": 2522, "model": "codex-auto-review", "verdict": "request_changes",
     "found": ["d1"], "unmatched": 1, "tokens_in": 9000, "tokens_out": 700, "cost_usd": 0.04}

``found`` is the seeded defect ids the review named; ``unmatched`` counts findings that matched no
seeded defect (the false positives). ``cost_usd`` is an estimate and may be absent: a missing one is
reported as unmeasured, never as zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

#: The review model today's pipeline runs; every other model is compared against it.
DEFAULT_BASELINE = "claude-sonnet-5-5"
#: The model that reviews first under the router, and the one stacked on top for risky PRs.
ROUTER_PRIMARY = "codex-auto-review"


def load_seeds(raw: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Validate the seeds file; refuse an entry that would score every reviewer as perfect."""
    seeds: dict[int, dict[str, Any]] = {}
    for key, entry in raw.items():
        defects = list(entry.get("defects") or [])
        if not defects and entry.get("clean") is not True:
            raise ValueError(f"PR {key}: give `defects` or mark it `clean: true`")
        seeds[int(key)] = {"defects": defects, "risk_high": bool(entry.get("risk_high"))}
    return seeds


def load_results(lines: list[str]) -> list[dict[str, Any]]:
    """Parse the ndjson rows, skipping blank lines."""
    return [json.loads(line) for line in lines if line.strip()]


def _blocking(verdict: str) -> bool:
    return verdict.strip().lower() in {"request_changes", "changes_requested", "reject", "block"}


def summarize(seeds: dict[int, dict[str, Any]], rows: list[dict[str, Any]],
              baseline: str = DEFAULT_BASELINE) -> dict[str, dict[str, Any]]:
    """Per-model verdict agreement with the baseline, seeded-defect recall, false positives, spend."""
    by_model: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_model[row["model"]][int(row["pr"])] = row
    base_rows = by_model.get(baseline, {})
    out: dict[str, dict[str, Any]] = {}
    for model, per_pr in sorted(by_model.items()):
        shared = [pr for pr in per_pr if pr in base_rows]
        agree = sum(1 for pr in shared if _blocking(per_pr[pr]["verdict"]) == _blocking(base_rows[pr]["verdict"]))
        possible = hit = 0
        for pr, row in per_pr.items():
            defects = set(seeds.get(pr, {}).get("defects", []))
            possible += len(defects)
            hit += len(defects & set(row.get("found") or []))
        costs = [r["cost_usd"] for r in per_pr.values() if r.get("cost_usd") is not None]
        out[model] = {
            "reviews": len(per_pr),
            "agreement": (agree / len(shared)) if shared and model != baseline else None,
            "agreement_n": len(shared),
            "recall": (hit / possible) if possible else None,
            "defects_found": hit, "defects_seeded": possible,
            "false_positives": sum(int(r.get("unmatched") or 0) for r in per_pr.values()),
            "tokens": sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in per_pr.values()),
            "cost_usd": sum(costs) if len(costs) == len(per_pr) else None,
            "run_ids": sorted(str(r.get("run_id", "")) for r in per_pr.values()),
        }
    return out


def model_mix(seeds: dict[int, dict[str, Any]], rows: list[dict[str, Any]], baseline: str = DEFAULT_BASELINE,
              primary: str = ROUTER_PRIMARY) -> dict[str, Any]:
    """Estimated review spend today (all baseline) against the router (primary, plus baseline on risk_high)."""
    cost = {(int(r["pr"]), r["model"]): r.get("cost_usd") for r in rows}
    prs = sorted({int(r["pr"]) for r in rows})
    today = [cost.get((pr, baseline)) for pr in prs]
    routed: list[float | None] = []
    mix: dict[str, int] = defaultdict(int)
    for pr in prs:
        mix[primary] += 1
        routed.append(cost.get((pr, primary)))
        if seeds.get(pr, {}).get("risk_high"):
            mix[baseline] += 1
            routed.append(cost.get((pr, baseline)))
    complete = lambda xs: sum(xs) if xs and all(x is not None for x in xs) else None  # noqa: E731
    return {"prs": len(prs), "router_reviews": dict(mix), "today_cost_usd": complete(today),
            "router_cost_usd": complete(routed)}


def render(summary: dict[str, dict[str, Any]], mix: dict[str, Any], today_mix: dict[str, Any] | None = None) -> str:
    """The report as markdown. Unmeasured values print `n/a`, never 0."""
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.0f}%"

    def usd(v: float | None) -> str:
        return "n/a" if v is None else f"${v:.2f}"

    lines = ["| model | reviews | agreement w/ baseline | recall | false positives | tokens | est. cost | run ids |",
             "|---|---|---|---|---|---|---|---|"]
    for model, s in summary.items():
        recall = f"{pct(s['recall'])} ({s['defects_found']}/{s['defects_seeded']})"
        lines.append(f"| {model} | {s['reviews']} | {pct(s['agreement'])} (n={s['agreement_n']}) | {recall} | "
                     f"{s['false_positives']} | {s['tokens']} | {usd(s['cost_usd'])} | {', '.join(s['run_ids'])} |")
    lines += ["", f"Model mix over {mix['prs']} PRs — router reviews: {mix['router_reviews']}; "
              f"estimated review cost today {usd(mix['today_cost_usd'])} vs router {usd(mix['router_cost_usd'])}."]
    if today_mix:
        lines.append(f"Today's recorded mix: {today_mix}")
    lines.append("The recall bar for Codex as merge-gate reviewer is the owner's; this report states numbers only.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seeds")
    ap.add_argument("results")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--today-mix", help="JSON file of today's recorded review model mix")
    args = ap.parse_args(argv)
    seeds = load_seeds(json.loads(Path(args.seeds).read_text(encoding="utf-8")))
    rows = load_results(Path(args.results).read_text(encoding="utf-8").splitlines())
    today = json.loads(Path(args.today_mix).read_text(encoding="utf-8")) if args.today_mix else None
    report = render(summarize(seeds, rows, args.baseline), model_mix(seeds, rows, args.baseline), today)
    sys.stdout.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
