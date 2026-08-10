"""The state machine: GitHub facts in, one decision out.

Split deliberately into a PURE function (`decide`) and an impure wrapper (`observe`). Everything
worth arguing about lives in `decide`, which takes a plain snapshot and returns a `Decision` — no
network, no clock, no database — so every transition is testable exactly, including the ones that
only happen when GitHub is lying or unreachable. `observe` does the I/O and applies the result.

That split is also what makes the migration's acceptance criterion possible: v1's per-tick observed
inputs can be replayed through `decide` offline and compared, without a daemon or a webhook.

The rules that shaped this, each from a measured v1 failure:

* **Unreadable is a decision to do NOTHING**, never a decision to proceed. v1 merged on an
  unreadable state once (#1082) and re-enqueued 154 times.
* **Waiting is a state, not a poll.** Every terminal-ish answer sets a `wake_at`, so the item leaves
  the scheduler's attention entirely until an event or that deadline.
* **Nothing dispatches without a fresh trust check.** Admission happens here; the actual dispatch
  re-verifies against the timeline, because webhook ordering is not guaranteed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import db, github

LOG = logging.getLogger("lemd.observe")

# Actions a decision can ask for. The scheduler owns HOW; this module owns WHETHER and WHICH.
ACT_NONE = "none"
ACT_DISPATCH = "dispatch"        # run an agent in some MODE
ACT_MERGE = "merge"              # gh-only: arm auto-merge
ACT_PARK = "park"                # gh-only: escalate to the owner
ACT_CLOSE = "close"              # terminal bookkeeping


@dataclass(frozen=True)
class Snapshot:
    """Everything `decide` is allowed to know. Built by `observe`, or by a replay harness."""

    kind: str                       # 'issue' | 'pr'
    number: int
    labels: frozenset[str] = frozenset()
    state: str = "OPEN"             # GitHub's OPEN/CLOSED/MERGED
    is_draft: bool = False
    upstream: bool = True
    branch: str | None = None
    head_sha: str | None = None
    merge_state: str = ""           # GitHub mergeStateStatus
    queue_state: str = ""           # mergeQueueEntry state, "" = no entry
    checks: github.ChecksState | None = None
    review_fresh: bool = False      # a review marker at or after the current head
    unresolved_threads: int = 0
    readable: bool = True           # False when any required read failed


@dataclass(frozen=True)
class Decision:
    """What to do, what state to move to, and why — the 'why' is telemetry, not decoration."""

    action: str
    next_state: str
    reason: str
    mode: str | None = None         # for ACT_DISPATCH
    wait_reason: str | None = None
    wake_in: int | None = None      # seconds; None = no TTL (event-only)
    park_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


#: Human holds. An item carrying either is the owner's, not the pipeline's — v1 learned this when a
#: green PR parked with `needs-human` (but still labelled `agent:working`) was merged on the next tick.
HOLD_LABELS = frozenset({"needs-human", "agent:blocked"})


def admissible(snap: Snapshot) -> tuple[bool, str]:
    """Is this item the pipeline's to act on at all?

    Admission is by LABEL and PROVENANCE, never by author. Excluding "dependabot PRs" by author
    would silently retire the depfix lane, which exists precisely to run an agent ON a Dependabot
    PR — the router workflow only applies the label, the agent does the fix.
    """
    if not snap.readable:
        return False, "unreadable"
    if snap.kind == "pr" and not snap.upstream:
        return False, "fork_pr"
    # Release-please PRs are excluded by BRANCH, not author: arming auto-merge on one would bypass
    # the owner's 4x-daily release windows entirely.
    if snap.branch and snap.branch.startswith("release-please--"):
        return False, "release_pr"
    if "autorelease: pending" in snap.labels:
        return False, "release_pr"
    if not any(ll.startswith("agent:") for ll in snap.labels):
        return False, "no_agent_label"
    return True, "ok"


def decide(snap: Snapshot, *, ttl_ci: int, ttl_review: int, ttl_queue: int,
           ttl_parked: int) -> Decision:
    """Pure state-machine step. No I/O, no clock — every input is in `snap`.

    Returns the single next action for this item. Order matters and encodes priority: terminal
    facts first, then human holds, then blockers, then progress.
    """
    # ---- terminal facts win over everything, including holds -------------------------------
    if snap.state == "MERGED":
        return Decision(ACT_CLOSE, db.STATE_MERGED, "merged")
    if snap.state == "CLOSED":
        return Decision(ACT_CLOSE, db.STATE_CLOSED, "closed_unmerged")

    # ---- an unreadable snapshot is never a licence to act ------------------------------------
    if not snap.readable:
        # Short TTL: this is "ask again soon", not a wait state with a real event behind it.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "github_unreadable",
                        wait_reason="unreadable", wake_in=300)

    ok, why = admissible(snap)
    if not ok:
        return Decision(ACT_NONE, db.STATE_PARKED, f"not_admissible:{why}",
                        park_reason=why, wake_in=None)

    # ---- the owner's holds outrank every lane -------------------------------------------------
    if snap.labels & HOLD_LABELS:
        # Event-driven (an owner comment or a label removal), with a slow safety re-check so a
        # missed webhook costs hours, not forever.
        return Decision(ACT_NONE, db.STATE_PARKED, "human_hold",
                        park_reason="needs_human", wake_in=ttl_parked)

    if snap.kind == "issue":
        # An issue with agent:ready and no hold is simply work to start.
        if "agent:ready" in snap.labels:
            return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "issue_ready", mode="start")
        return Decision(ACT_NONE, db.STATE_PARKED, "issue_not_ready", park_reason="not_ready")

    # ---- PR lanes, cheapest-to-unblock first --------------------------------------------------
    if snap.is_draft:
        # A draft cannot enter the queue however green it is. #1236 sat CLEAN with 21 passing
        # checks for hours because nothing noticed it was a draft.
        return Decision(ACT_NONE, db.STATE_PARKED, "pr_is_draft", park_reason="draft",
                        wake_in=ttl_parked)

    if snap.merge_state == "DIRTY":
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "conflicts_with_main", mode="rebase")

    if "agent:revise" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "owner_requested_changes", mode="revise")
    if "agent:depfix" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "dependabot_ci_failure", mode="depfix")
    if "agent:docfix" in snap.labels:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "lint_gate_failure", mode="docfix")

    checks = snap.checks
    if checks is None:
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "checks_unknown",
                        wait_reason="ci", wake_in=300)

    if checks.failed:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "required_checks_failing", mode="fix",
                        details={"failed": list(checks.names_failed)})

    if checks.pending or checks.total == 0:
        # Waiting on CI is a STATE. v1 spent a whole tick per PR per 5 minutes re-asking this.
        return Decision(ACT_NONE, db.STATE_WAIT_CI, "ci_running",
                        wait_reason="ci", wake_in=ttl_ci)

    if snap.unresolved_threads > 0:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "unresolved_review_threads", mode="review",
                        details={"threads": snap.unresolved_threads})

    if not snap.review_fresh:
        return Decision(ACT_DISPATCH, db.STATE_CLAIMED, "no_fresh_review", mode="selfreview")

    # ---- green, reviewed, threads clear: the queue's problem now ------------------------------
    if snap.queue_state:
        return Decision(ACT_NONE, db.STATE_WAIT_QUEUE, "in_merge_queue",
                        wait_reason="merge_queue", wake_in=ttl_queue,
                        details={"queue_state": snap.queue_state})

    return Decision(ACT_MERGE, db.STATE_WAIT_QUEUE, "gate_satisfied",
                    wait_reason="merge_queue", wake_in=ttl_queue)


def snapshot_pr(slug: str, number: int, *, review_fresh: bool = False,
                unresolved: int = 0) -> Snapshot:
    """Build a `Snapshot` for a PR from live GitHub reads.

    Any failed read yields `readable=False` rather than a partial snapshot, because a snapshot
    that is half-true is worse than one that admits ignorance: `decide` can handle "I don't know"
    and cannot handle "green" that was actually "unreadable".
    """
    try:
        facts = github.pr_facts(slug, number)
        checks = github.checks_for(slug, number)
        queue = github.merge_queue_state(slug, number)
    except github.GitHubUnavailable as exc:
        LOG.warning("PR #%s unreadable: %s", number, exc)
        return Snapshot(kind="pr", number=number, readable=False)

    return Snapshot(
        kind="pr",
        number=number,
        labels=frozenset(github.label_names(facts)),
        state=(facts.get("state") or "OPEN").upper(),
        is_draft=bool(facts.get("isDraft")),
        upstream=github.is_upstream(facts, slug),
        branch=facts.get("headRefName"),
        head_sha=facts.get("headRefOid"),
        merge_state=(facts.get("mergeStateStatus") or "").upper(),
        queue_state=queue,
        checks=checks,
        review_fresh=review_fresh,
        unresolved_threads=unresolved,
        readable=True,
    )


def snapshot_issue(slug: str, number: int) -> Snapshot:
    """Build a `Snapshot` for an issue."""
    try:
        facts = github.gh_json(
            ["issue", "view", str(number), "--repo", slug, "--json",
             "number,state,labels,updatedAt"]
        ) or {}
    except github.GitHubUnavailable as exc:
        LOG.warning("issue #%s unreadable: %s", number, exc)
        return Snapshot(kind="issue", number=number, readable=False)
    return Snapshot(
        kind="issue",
        number=number,
        labels=frozenset(github.label_names(facts)),
        state=(facts.get("state") or "OPEN").upper(),
        readable=True,
    )
