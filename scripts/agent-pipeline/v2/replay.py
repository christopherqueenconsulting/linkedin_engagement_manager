#!/usr/bin/env python3
"""Replay v1's recorded ticks through v2's decision function — the cutover's acceptance gate.

The migration is NOT accepted on an agreement percentage. v1 takes one snapshot every five minutes
and acts once; v2 observes continuously and acts on everything it can. Those two streams are not
comparable as streams, so "they agreed 91% of the time" would be a number with no meaning attached
(finding M9). What IS comparable is a single snapshot: given the same facts about one item, what
would each runner do?

So the criterion is two properties, both checkable and neither of them a percentage:

**A. Zero safety violations.** v2 must never propose an action on a snapshot where v1's gate
refuses. Concretely: never dispatch where the trust boundary said no, and never merge where v1's
gate was red. A single violation blocks cutover — this is the property that would let a scheduling
bug become a merged PR nobody reviewed.

**B. v2's action set is a SUPERSET of v1's.** v2 doing MORE is the entire point (v1 spent 75-84% of
its ticks re-polling). v2 doing LESS on the same facts is a regression to explain. Every extra
action is listed for a human to read before cutover; this tool does not judge them.

Input is v1's `logs/tick-outcomes.ndjson` — what v1 actually observed and did, already on the box.
Each row becomes a `Snapshot`, which goes through the SAME pure `observe.decide` the daemon runs.
No daemon, no webhook, no network: the split between `decide` (pure) and `observe` (I/O) exists
precisely so this file can be honest.

    ./replay.py /home/lem/agent-pipeline/logs/tick-outcomes.ndjson
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lemd import observe  # noqa: E402
from lemd.config import load  # noqa: E402

#: v1 tick reasons that mean "v1 dispatched an agent in this mode".
_V1_DISPATCH = {
    "mode_start": "start", "mode_fix": "fix", "mode_review": "review",
    "mode_selfreview": "selfreview", "mode_rebase": "rebase", "mode_revise": "revise",
    "mode_depfix": "depfix", "mode_docfix": "docfix", "mode_phasefix": "phasefix",
}
#: v1 reasons that mean "v1 asked the merge queue to take this PR".
_V1_MERGE = {"mode_merge", "mode_merge_fastpath"}
#: v1 reasons that mean "v1 REFUSED to act" — the rows property A is tested against.
#:
#: `merge_not_taken` / `merge_queue_stuck` / `merge_queue_unmergeable` are the #1120 class: v1
#: DETECTED the wedge and changed no state, so every following tick re-fed the PR to the queue.
#: Counting them as refusals is what makes property A meaningful — v2 proposing a merge on one of
#: those snapshots would be reproducing the incident, not improving on it.
_V1_REFUSED = {"paused", "both_lanes_exhausted", "all_slots_busy", "merge_not_taken",
               "merge_queue_stuck", "merge_queue_unmergeable", "trust_refused",
               "degraded_hold_new_start", "phase_guard_parked"}


@dataclass
class Finding:
    """One disagreement worth a human's attention."""

    kind: str          # "safety" | "extra" | "missing"
    number: int
    v1: str
    v2: str
    detail: str


def snapshot_from_row(row: dict) -> observe.Snapshot | None:
    """Rebuild the facts v1 saw on one tick.

    v1's telemetry records what it DID, not everything it read, so a replayed snapshot is a floor
    on what v2 knew — which is the safe direction for property A: a snapshot missing facts makes
    v2 more conservative (unreadable/absent inputs park or wait), never less.
    """
    # 0 is v1's "absent", not issue zero — `or` handles both that and a missing key.
    number = row.get("pr_number") or row.get("issue_number") or 0
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    kind = "pr" if row.get("pr_number") else "issue"
    labels = set(row.get("labels") or [])
    if kind == "issue" and row.get("reason") == "mode_start":
        labels.add("agent:ready")
    if kind == "pr":
        labels.add("agent:working")
    return observe.Snapshot(
        kind=kind, number=number, labels=frozenset(labels),
        state=(row.get("pr_state") or "OPEN").upper(),
        upstream=bool(row.get("upstream", True)),
        branch=row.get("branch") or None,
        head_sha=row.get("head_sha") or None,
        merge_state=(row.get("merge_state") or "").upper(),
        queue_state=row.get("queue_state") or "",
        readable=True,
    )


def v1_action(row: dict) -> tuple[str, str]:
    """What v1 did on this tick, as `(action, mode)`."""
    reason = row.get("reason") or ""
    if reason in _V1_DISPATCH:
        return observe.ACT_DISPATCH, _V1_DISPATCH[reason]
    if reason in _V1_MERGE:
        return observe.ACT_MERGE, "merge"
    if reason in _V1_REFUSED:
        return "refused", reason
    return observe.ACT_NONE, reason


def replay(path: str | Path, cfg=None) -> tuple[list[Finding], Counter]:
    """Run every recorded tick through `observe.decide`.

    Returns:
        `(findings, counts)`. Cutover is allowed only when no finding has `kind == "safety"`.
    """
    cfg = cfg or load()
    findings: list[Finding] = []
    counts: Counter = Counter()
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        snap = snapshot_from_row(row)
        if snap is None:
            continue
        counts["rows"] += 1
        act1, mode1 = v1_action(row)
        d2 = observe.decide(snap, ttl_ci=cfg.ttl_ci, ttl_review=cfg.ttl_review,
                            ttl_queue=cfg.ttl_queue, ttl_parked=cfg.ttl_parked)
        counts[f"v1:{act1}"] += 1
        counts[f"v2:{d2.action}"] += 1

        if act1 == "refused" and d2.action in (observe.ACT_DISPATCH, observe.ACT_MERGE):
            # Property A. The one class that blocks cutover.
            findings.append(Finding("safety", snap.number, f"refused({mode1})",
                                    f"{d2.action}({d2.mode or ''})", d2.reason))
            counts["safety_violations"] += 1
        elif act1 in (observe.ACT_DISPATCH, observe.ACT_MERGE) and d2.action == observe.ACT_NONE:
            # Distinguish "v2 would not have acted" from "the REPLAY could not tell it enough to
            # decide". v1's telemetry records what it DID, never the CI facts it read, so a PR row
            # arrives with no check state and `decide` correctly answers "I don't know" — which is
            # the fail-closed branch working, not a regression. Counting those as regressions would
            # bury the handful that are real under a thousand that are not.
            blind = d2.reason in ("checks_unknown", "github_unreadable")
            findings.append(Finding("blind" if blind else "missing", snap.number,
                                    f"{act1}({mode1})", "none", d2.reason))
            counts["blind" if blind else "missing"] += 1
        elif act1 == observe.ACT_NONE and d2.action in (observe.ACT_DISPATCH, observe.ACT_MERGE):
            # Property B: expected, and exactly what v2 exists to do. Reported, never failed on.
            findings.append(Finding("extra", snap.number, "none",
                                    f"{d2.action}({d2.mode or ''})", d2.reason))
            counts["extra"] += 1
    return findings, counts


def main(argv: list[str]) -> int:
    """CLI: print the report and exit non-zero on any safety violation."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    findings, counts = replay(argv[1])
    print(f"replayed {counts['rows']} recorded tick(s)")
    print(f"  safety violations   : {counts['safety_violations']}   <-- must be 0 to cut over")
    print(f"  v2 acts, v1 idled   : {counts['extra']}   (expected — review the list)")
    print(f"  v1 acted, v2 idles  : {counts['missing']}   (explain each before cutover)")
    print(f"  snapshot too thin   : {counts['blind']}   (v1 never recorded the CI facts; v2 waits)")
    by_reason: Counter = Counter(f.detail for f in findings if f.kind == "missing")
    for reason, n in by_reason.most_common():
        print(f"    missing/{reason}: {n}")
    for f in findings:
        if f.kind == "safety":
            print(f"  [SAFETY] #{f.number}: v1={f.v1} v2={f.v2} ({f.detail})")
    if counts["blind"] and not counts["missing"]:
        print("\nNOTE: every disagreement is a THIN SNAPSHOT, not a decision difference. v1's")
        print("tick-outcome log records what it did, never the check state it read, so replayed PR")
        print("rows carry no CI facts and `decide` answers 'unreadable -> wait'. That proves the")
        print("fail-closed branch and property A; property B on PR lanes is not observable from")
        print("this input and is covered instead by the live shadow window.")
    return 1 if counts["safety_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
