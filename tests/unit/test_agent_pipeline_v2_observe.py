"""Tests for the v2 state machine.

`decide` is pure, so every transition — including the ones that only occur when GitHub is lying or
unreachable — is testable exactly. The cases below are organised around the v1 incidents each rule
exists to prevent, so a regression fails on a test that names the incident rather than on a vague
invariant.
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


def pr(**kw) -> observe.Snapshot:
    """A mergeable PR snapshot, overridable per test."""
    base = dict(
        kind="pr", number=1, labels=frozenset({"agent:working"}), state="OPEN",
        branch="feature/x", head_sha="abc", checks=GREEN, review_fresh=True,
    )
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- terminal facts


def test_merged_is_terminal_even_with_holds():
    """A merged PR is done; a stale hold label must not resurrect it."""
    got = d(pr(state="MERGED", labels=frozenset({"agent:working", "needs-human"})))
    assert (got.action, got.next_state) == (observe.ACT_CLOSE, db.STATE_MERGED)


def test_closed_unmerged_is_terminal():
    assert d(pr(state="CLOSED")).next_state == db.STATE_CLOSED


# ---------------------------------------------------------------- unreadable


def test_unreadable_never_acts():
    """#1082: an unreadable state read as healthy produced 154 re-enqueues."""
    got = d(pr(readable=False))
    assert got.action == observe.ACT_NONE
    assert got.wake_in == 300  # ask again soon, do nothing now


def test_unknown_checks_do_not_merge():
    """A missing rollup must never be mistaken for a green one."""
    got = d(pr(checks=None))
    assert got.action == observe.ACT_NONE
    assert got.reason == "checks_unknown"


def test_zero_required_checks_is_not_green():
    """A head with no required checks yet is one where CI has not started."""
    got = d(pr(checks=ChecksState(failed=0, pending=0, total=0)))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_CI


# ---------------------------------------------------------------- admission


def test_fork_pr_is_never_admitted():
    """Attacker-controlled code must not reach a lane that checks it out and runs it."""
    got = d(pr(upstream=False))
    assert got.action == observe.ACT_NONE
    assert "fork_pr" in got.reason


def test_release_pr_excluded_by_branch_not_author():
    """Arming auto-merge on a release PR would bypass the 4x-daily release windows."""
    got = d(pr(branch="release-please--branches--main"))
    assert got.action == observe.ACT_NONE
    assert "release_pr" in got.reason


def test_release_pr_excluded_by_autorelease_label():
    got = d(pr(labels=frozenset({"agent:working", "autorelease: pending"})))
    assert "release_pr" in got.reason


def test_dependabot_pr_IS_admitted_when_labelled():
    """Dependabot PRs must stay admissible.

    Excluding them by AUTHOR would silently retire the depfix lane, which exists to run an agent ON
    a dependabot PR — the router workflow only applies the label; the agent does the fix.
    """
    got = d(pr(labels=frozenset({"agent:depfix"}), checks=RED))
    assert got.action == observe.ACT_DISPATCH
    assert got.mode == "depfix"


def test_unlabelled_pr_is_not_ours():
    got = d(pr(labels=frozenset()))
    assert got.action == observe.ACT_NONE
    assert "no_agent_label" in got.reason


# ---------------------------------------------------------------- human holds


@pytest.mark.parametrize("hold", ["needs-human", "agent:blocked"])
def test_human_hold_outranks_every_lane(hold):
    """A green, reviewed PR parked by a human must NOT be merged on the next pass."""
    got = d(pr(labels=frozenset({"agent:working", hold})))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_PARKED
    assert got.wake_in == TTLS["ttl_parked"]


def test_hold_beats_even_a_failing_lane():
    got = d(pr(labels=frozenset({"agent:working", "needs-human"}), checks=RED))
    assert got.action == observe.ACT_NONE


# ---------------------------------------------------------------- PR lanes, in priority order


def test_draft_is_parked_not_merged():
    """#1236 sat CLEAN with 21 green checks for hours because nothing noticed it was a draft."""
    got = d(pr(is_draft=True))
    assert got.next_state == db.STATE_PARKED
    assert got.park_reason == "draft"


def test_dirty_goes_to_rebase_before_anything_else():
    got = d(pr(merge_state="DIRTY", checks=RED))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "rebase")


def test_failing_checks_dispatch_fix_with_the_names():
    got = d(pr(checks=RED))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "fix")
    assert got.details["failed"] == ["Unit Tests (Python 3.12)"]


def test_ci_running_is_a_wait_state_not_a_poll():
    """The core v2 economy change: this costs zero scheduler attention until CI reports."""
    got = d(pr(checks=RUNNING))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_CI
    assert got.wake_in == TTLS["ttl_ci"]


def test_unresolved_threads_block_the_merge():
    got = d(pr(unresolved_threads=2))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "review")


def test_missing_review_dispatches_selfreview():
    got = d(pr(review_fresh=False))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "selfreview")


def test_green_and_reviewed_arms_the_merge():
    got = d(pr())
    assert got.action == observe.ACT_MERGE
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.wake_in == TTLS["ttl_queue"]


def test_already_queued_waits_instead_of_re_arming():
    """Re-arming a PR the queue already holds is how #1067 reached 154 re-enqueues."""
    got = d(pr(queue_state="QUEUED"))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_WAIT_QUEUE
    assert got.details["queue_state"] == "QUEUED"


def test_revise_outranks_selfreview():
    """The owner's feedback is more important than arranging a review of the old head."""
    got = d(pr(labels=frozenset({"agent:working", "agent:revise"}), review_fresh=False))
    assert got.mode == "revise"


# ---------------------------------------------------------------- issues


def test_ready_issue_dispatches_start():
    got = d(observe.Snapshot(kind="issue", number=9, labels=frozenset({"agent:ready"})))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "start")


def test_issue_without_ready_is_not_started():
    got = d(observe.Snapshot(kind="issue", number=9, labels=frozenset({"agent:working"})))
    assert got.action == observe.ACT_NONE


def test_held_issue_is_never_started():
    got = d(observe.Snapshot(
        kind="issue", number=9, labels=frozenset({"agent:ready", "needs-human"})))
    assert got.action == observe.ACT_NONE
    assert got.next_state == db.STATE_PARKED


# ---------------------------------------------------------------- checks helper


def test_checksstate_green_requires_reported_checks():
    assert ChecksState(failed=0, pending=0, total=6).green is True
    assert ChecksState(failed=0, pending=0, total=0).green is False
    assert ChecksState(failed=1, pending=0, total=6).green is False
    assert ChecksState(failed=0, pending=1, total=6).green is False


def test_every_decision_carries_a_reason():
    """`reason` is telemetry, not decoration: it is how a wrong dispatch gets diagnosed later."""
    for snap in (pr(), pr(checks=RED), pr(readable=False), pr(is_draft=True),
                 pr(state="MERGED"), pr(upstream=False)):
        assert d(snap).reason
