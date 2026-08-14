"""The merge-queue gate: a PR the queue is validating is never pushed to (#1388).

`queue_state` used to be read LAST in `observe.decide`, below `fix`, `review` and `selfreview`. So a
PR already sitting in GitHub's merge queue could be dispatched into all three, and each of them
pushes a commit — which ejects it. Merge-queue entry is expensive (the PR is built against the queue
head), so an ejection re-pays that cost from scratch, and a PR that keeps acquiring findings cycles.

The rule these tests hold: **while `queue_state` is non-empty, wait.** `DIRTY` is the single
documented exception, because the queue cannot merge a conflicted PR either. The wait is not a
swallow — losing the entry drops `queue_state` back to `""`, and the same lane dispatches on the
next observation, which the last test here asserts directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, observe  # noqa: E402
from lemd.github import ChecksState  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = ChecksState(failed=0, pending=0, total=6)
RED = ChecksState(failed=1, pending=0, total=6, names_failed=("Unit Tests (Python 3.12)",))
RUNNING = ChecksState(failed=0, pending=3, total=6)

#: Every `MergeQueueEntryState` GitHub can report. Enumerated in the TEST rather than in `decide`:
#: the production rule is "any live entry", and parametrising over the enum is how that claim is
#: proved for each member without the code having to know them apart.
QUEUE_STATES = ("QUEUED", "AWAITING_CHECKS", "MERGEABLE", "UNMERGEABLE", "LOCKED")

#: The PR-lane conditions that dispatch a lane which PUSHES. Each is `(name, snapshot kwargs)`, and
#: each is asserted twice: it dispatches with no queue entry, and it does NOT with one.
PUSHING_LANES = [
    ("failed_check", dict(checks=RED), "fix"),
    ("unresolved_thread", dict(unresolved_threads=2), "review"),
    ("stale_review", dict(review_fresh=False), "selfreview"),
    ("revise_label", dict(labels=frozenset({"agent:working", "agent:revise"})), "revise"),
    ("phasefix_label", dict(labels=frozenset({"agent:working", "agent:phasefix"})), "phasefix"),
    ("depfix_label", dict(labels=frozenset({"agent:working", "agent:depfix"})), "depfix"),
    ("docfix_label", dict(labels=frozenset({"agent:working", "agent:docfix"})), "docfix"),
]


def pr(**kw) -> observe.Snapshot:
    """A mergeable PR snapshot, overridable per test."""
    base = dict(
        kind="pr", number=1, labels=frozenset({"agent:working"}), state="OPEN",
        branch="feature/x", head_sha="abc", checks=GREEN, review_fresh=True,
        merge_state="CLEAN",
    )
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ------------------------------------------------- the gate itself


@pytest.mark.parametrize("queue_state", QUEUE_STATES)
@pytest.mark.parametrize("name,extra,mode", PUSHING_LANES, ids=[c[0] for c in PUSHING_LANES])
def test_no_pushing_lane_is_dispatched_to_a_queued_pr(queue_state, name, extra, mode):
    """The defect, asserted over `queue_state` × every lane that pushes a commit."""
    got = d(pr(queue_state=queue_state, **extra))
    assert got.action == observe.ACT_NONE, f"{name} was dispatched to a PR in state {queue_state}"
    assert got.mode is None
    assert got.reason == "in_merge_queue"
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.wake_in == TTLS["ttl_queue"]
    assert got.details["queue_state"] == queue_state


@pytest.mark.parametrize("name,extra,mode", PUSHING_LANES, ids=[c[0] for c in PUSHING_LANES])
def test_the_same_lane_dispatches_once_the_entry_is_gone(name, extra, mode):
    """The gate must DELAY a lane, never swallow it.

    The other half of the parametrisation above: with no queue entry these are exactly the snapshots
    that dispatch, so the only thing the gate changes is *when*. An ejected or merged entry reports
    `""` on the next observation and this is what runs.
    """
    got = d(pr(queue_state="", **extra))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, mode)


@pytest.mark.parametrize("queue_state", QUEUE_STATES)
def test_pending_checks_on_a_queued_pr_report_the_queue_not_ci(queue_state):
    """`ci_running` on a queued PR misleads the operator reading the state.

    Nothing is being withheld here — the ladder would wait on pending checks too — so `withheld` is
    empty, and that is the distinction the field exists to draw.
    """
    got = d(pr(queue_state=queue_state, checks=RUNNING))
    assert got.reason == "in_merge_queue"
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.details["withheld"] == ""


def test_a_queued_pr_is_not_re_armed():
    """Re-arming a PR the queue already holds is how #1067 reached 154 re-enqueues."""
    got = d(pr(queue_state="QUEUED"))
    assert got.action == observe.ACT_NONE
    assert got.reason == "in_merge_queue"


# ------------------------------------------------- the ledger


@pytest.mark.parametrize("name,extra,mode", PUSHING_LANES, ids=[c[0] for c in PUSHING_LANES])
def test_the_withheld_lane_is_named_in_the_decision(name, extra, mode):
    """A silent queued PR must be legible.

    The gate's own failure mode is a queued PR quietly holding a `fix` nobody can see, so the
    decision carries the lane that will run once the entry clears.
    """
    got = d(pr(queue_state="QUEUED", **extra))
    assert got.details["withheld"] == mode


def test_a_failed_check_outranks_pending_ones_in_the_ledger():
    """`withheld` mirrors the ladder's order, where a failure beats a check still running."""
    mixed = ChecksState(failed=1, pending=2, total=6, names_failed=("Unit Tests (Python 3.12)",))
    assert d(pr(queue_state="QUEUED", checks=mixed)).details["withheld"] == "fix"


def test_unreadable_checks_hold_nothing():
    """`checks=None` is "could not tell", which the ladder waits on rather than acting on."""
    assert d(pr(queue_state="QUEUED", checks=None)).details["withheld"] == ""


def test_the_first_lane_label_by_priority_is_the_one_named():
    """Two lane labels resolve by `LANE_LABEL_PRIORITY` here as they do in the ladder."""
    got = d(pr(queue_state="QUEUED",
               labels=frozenset({"agent:working", "agent:docfix", "agent:revise"})))
    assert got.details["withheld"] == "revise"


# ------------------------------------------------- what still outranks the gate


@pytest.mark.parametrize("queue_state", QUEUE_STATES)
def test_dirty_is_the_documented_exception(queue_state):
    """A conflicted PR cannot merge from the queue either — GitHub ejects it regardless."""
    got = d(pr(queue_state=queue_state, merge_state="DIRTY", checks=RED))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "rebase")


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_terminal_facts_still_win(state):
    """A queued entry is not a reason to keep observing a PR that has already ended."""
    assert d(pr(queue_state="QUEUED", state=state)).action == observe.ACT_CLOSE


def test_an_unreadable_snapshot_still_wins():
    """A queue state read off a snapshot that failed elsewhere is not evidence of anything."""
    got = d(pr(queue_state="QUEUED", readable=False))
    assert got.reason == "github_unreadable"


@pytest.mark.parametrize("hold", ["needs-human", "agent:blocked"])
def test_a_human_hold_still_outranks_the_gate(hold):
    """The hold branch pushes nothing — `disarm` is a GitHub-side action — so it stays above."""
    got = d(pr(queue_state="QUEUED", labels=frozenset({"agent:working", hold})))
    assert got.next_state == db.STATE_PARKED
    assert got.reason.startswith("human_hold")


def test_an_armed_queued_pr_reports_the_queue_not_the_arm():
    """The gate sits above the armed-auto-merge wait, so the queue is what gets reported."""
    got = d(pr(queue_state="QUEUED", merge_state="BLOCKED", auto_merge=True))
    assert got.reason == "in_merge_queue"
    assert got.next_state == db.STATE_WAIT_QUEUE


def test_owner_review_required_is_never_claimed_for_a_queued_pr():
    """#1501's branch means "nothing left but a human" — a live entry disproves that."""
    got = d(pr(queue_state="QUEUED", merge_state="BLOCKED", auto_merge=True, checks=GREEN))
    assert got.next_state != db.STATE_WAIT_OWNER_REVIEW
    assert got.reason == "in_merge_queue"
