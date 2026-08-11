"""Every `mergeStateStatus` value must be named, not just `DIRTY` (#1392).

`decide()` tested one value. Every other member of the enum fell through to the checks ladder, and
for `UNKNOWN` that is the shape of #1082: GitHub computes mergeability asynchronously, so an
unreadable field was being read as a healthy one and could reach `ACT_MERGE`.

The rest were right by accident rather than by decision, which is the same defect with a luckier
outcome — `UNSTABLE` reaching the merge gate is correct only because `checks_for` happens to filter
to required contexts, and nothing recorded that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, github, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = github.ChecksState(failed=0, pending=0, total=6)

#: Every value GitHub's enum can hold, as of 2026-08.
ALL_STATES = ("CLEAN", "DIRTY", "BLOCKED", "UNSTABLE", "BEHIND", "HAS_HOOKS", "UNKNOWN", "")


def pr(**kw) -> observe.Snapshot:
    """A green, reviewed PR — so the only thing deciding the outcome is `merge_state`."""
    base = dict(kind="pr", number=1, labels=frozenset({"agent:working"}), branch="feature/x",
                head_sha="abc", checks=GREEN, review_fresh=True, merge_state="CLEAN")
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


def test_every_enum_value_is_classified():
    """No value may be unnamed. This is the assertion the whole issue is about."""
    for state in ALL_STATES:
        assert state == "DIRTY" or state in observe.MERGE_STATE_PROCEED \
            or state in observe.MERGE_STATE_UNREADABLE, f"{state!r} is not named anywhere"


@pytest.mark.parametrize("state", ["UNKNOWN", ""])
def test_unreadable_mergeability_never_reaches_the_merge_gate(state):
    """The defect. A field GitHub is still computing must not read as healthy.

    Named for #1082, where v1 merged on an unreadable state and then re-enqueued 154 times.
    """
    got = d(pr(merge_state=state))
    assert got.action == observe.ACT_NONE
    assert got.reason == "merge_state_unknown"
    assert got.wake_in == 120


@pytest.mark.parametrize("state", ["CLEAN", "BLOCKED", "UNSTABLE", "BEHIND", "HAS_HOOKS"])
def test_proceeding_states_still_reach_the_ladder(state):
    """The four that were right by accident are now right by decision, and still behave the same."""
    assert d(pr(merge_state=state)).action == observe.ACT_MERGE


def test_dirty_still_rebases_before_anything_else():
    """`DIRTY` is handled above this block and must stay there — a conflicted PR cannot merge."""
    got = d(pr(merge_state="DIRTY"))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, "rebase")


def test_behind_does_not_dispatch_a_rebase():
    """Deliberate, and it depends on a fact worth re-checking if branch protection changes.

    `main` does not require branches to be up to date (`required_status_checks.strict` is false) and
    the merge queue builds against the queue head, so `BEHIND` resolves itself. Dispatching an agent
    to rebase would spend a model session on something GitHub does for free.
    """
    got = d(pr(merge_state="BEHIND"))
    assert got.mode != "rebase"


def test_an_unrecognised_value_waits_rather_than_guessing():
    """The enum is closed and fully named, so a new member means the world changed."""
    got = d(pr(merge_state="SOMETHING_NEW"))
    assert got.action == observe.ACT_NONE
    assert got.reason == "merge_state_unrecognised"
    assert got.details.get("merge_state") == "SOMETHING_NEW"


@pytest.mark.parametrize("lane,mode", [("agent:revise", "revise"), ("agent:depfix", "depfix"),
                                       ("agent:docfix", "docfix")])
def test_unknown_mergeability_does_not_hold_the_non_merging_lanes(lane, mode):
    """Placement matters as much as the branch.

    `revise`, `depfix` and `docfix` do not merge anything, so holding them on a mergeability GitHub
    has not finished computing would stall work for no safety gain. Only the merge path is gated.
    """
    got = d(pr(merge_state="UNKNOWN", labels=frozenset({lane})))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, mode)


def test_a_hold_still_outranks_an_unreadable_merge_state():
    """Ordering above this block is unchanged."""
    got = d(pr(merge_state="UNKNOWN", labels=frozenset({"agent:working", "needs-human"})))
    assert got.action != observe.ACT_NONE or got.reason.startswith("human_hold")


def test_a_draft_is_still_checked_first():
    """A draft cannot merge however its mergeability reads."""
    assert d(pr(merge_state="UNKNOWN", is_draft=True)).next_state == db.STATE_PARKED
