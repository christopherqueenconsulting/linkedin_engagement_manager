"""Three `decide()` blind spots from the §5 field matrix (#1393).

Small, independent, and each one a place where behaviour was incidental rather than chosen: which
lane wins when a PR carries two labels, what a draft nobody parked actually means, and whether a
label v1 wrote is still a concept in v2.
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


def pr(**kw) -> observe.Snapshot:
    """A PR snapshot that reaches the lane branches."""
    base = dict(kind="pr", number=1, labels=frozenset({"agent:working"}), branch="feature/x",
                head_sha="abc", checks=GREEN, review_fresh=True, merge_state="CLEAN")
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


# ---------------------------------------------------------------- lane precedence


@pytest.mark.parametrize("label,mode", [("agent:revise", "revise"), ("agent:depfix", "depfix"),
                                        ("agent:docfix", "docfix")])
def test_each_lane_label_still_dispatches_its_own_mode(label, mode):
    """The rewrite must not change what a single label does."""
    got = d(pr(labels=frozenset({label})))
    assert (got.action, got.mode) == (observe.ACT_DISPATCH, mode)


def test_two_lane_labels_resolve_by_declared_priority():
    """PR #1313 carried `agent:revise` + `agent:docfix`.

    First match wins, as before — but the order is now declared rather than whatever source order
    happened to be, and the owner's own instruction (`revise`) outranks a lint failure.
    """
    got = d(pr(labels=frozenset({"agent:docfix", "agent:revise"})))
    assert got.mode == "revise"


def test_the_waiting_lane_is_recorded_so_the_hand_off_is_visible():
    """The second lane is not stranded — but nothing said so, and nothing proved it.

    It is picked up on the next observation once the first lane's action removes its own label.
    Putting the queue in the decision ledger lets an operator see that instead of inferring it.
    """
    got = d(pr(labels=frozenset({"agent:docfix", "agent:revise"})))
    assert got.details.get("lanes_pending") == ["agent:docfix"]


def test_the_second_lane_runs_once_the_first_clears_its_label():
    """The hand-off itself, which is the claim the `lanes_pending` field is making."""
    after = d(pr(labels=frozenset({"agent:docfix"})))
    assert (after.action, after.mode) == (observe.ACT_DISPATCH, "docfix")


def test_a_single_lane_records_nothing_extra():
    """`lanes_pending` on every dispatch would be noise; it means "another lane is waiting"."""
    assert "lanes_pending" not in d(pr(labels=frozenset({"agent:revise"}))).details


def test_the_priority_table_covers_every_lane_the_daemon_reconciles():
    """A lane label `reconcile` fetches but `decide` cannot route is a silent no-op.

    This is how `agent:phasefix` became unreachable — a mode with a budget, a timeout and a prompt
    that nothing ever emits.
    """
    reconciled = {lb for lb in ("agent:revise", "agent:depfix", "agent:docfix")}
    assert reconciled <= set(observe.LANE_LABEL_PRIORITY)
    assert set(observe.LANE_LABEL_PRIORITY) == set(observe.LANE_LABEL_MODES)


# ---------------------------------------------------------------- the unheld draft


def test_a_draft_nobody_parked_is_a_wait_not_a_park():
    """Whose draft it is decides what it means.

    `park.sh` drafts a PR as step one of parking, and those always carry a hold label — the hold
    branch handles them, where an owner answer can release them. A draft with NO hold is the
    human's own state: nobody asked them anything. Calling it `parked` made it unreachable by BOTH
    paths, because the answer lane lives inside the hold check.
    """
    got = d(pr(is_draft=True))
    assert got.next_state == db.STATE_WAIT_REVIEW
    assert got.wait_reason == "draft"
    assert got.reason == "pr_is_draft"


def test_a_parked_draft_is_still_the_owners():
    """`park.sh`'s own drafts carry a hold, so they never reach the draft branch at all."""
    got = d(pr(is_draft=True, labels=frozenset({"agent:working", "needs-human"})))
    assert got.reason.startswith("human_hold")
    assert got.next_state == db.STATE_PARKED


def test_the_draft_wake_still_exists():
    """A wait with no TTL is a wedge. `ready_for_review` is the fast path, not the only one."""
    assert d(pr(is_draft=True)).wake_in == TTLS["ttl_parked"]


def test_the_release_edge_is_a_subscribed_event():
    """`ready_for_review` is an ACTION of `pull_request`, which the receiver subscribes to.

    `extract()` is action-agnostic, so no receiver change is needed — but if `pull_request` were
    ever dropped from the subscription, an unheld draft would wait the full TTL instead of waking
    the moment it is marked ready.
    """
    from lemd import receiver  # noqa: PLC0415
    assert "pull_request" in receiver.SUBSCRIBED


def test_a_draft_is_still_checked_before_the_lanes():
    """A draft cannot merge however it is labelled — #1236 sat CLEAN with 21 green checks."""
    got = d(pr(is_draft=True, labels=frozenset({"agent:revise"})))
    assert got.action == observe.ACT_NONE


# ---------------------------------------------------------------- agent:merge-parked


def test_merge_parked_is_removal_only_and_says_so():
    """Half-alive was the complaint: removed by `unpark.sh`, written and read by nothing.

    The decision is to keep the removal and NOT start writing it. v2 has no separate merge park —
    every park is a budget park, reasoned about through `parked_reason` — so writing the label would
    add a concept the daemon does not have. The removal stays because a rollback to v1 must not
    inherit a label v1 would act on. What matters is that the reasoning is recorded where the code
    is, so the next reader does not have to rediscover it.
    """
    unpark = (_V2 / "actions" / "unpark.sh").read_text()
    assert "--remove-label \"agent:merge-parked\"" in unpark
    assert "v2 has no separate merge park" in unpark
    for py in (_V2 / "lemd").glob("*.py"):
        assert "merge-parked" not in py.read_text(), f"{py.name} started reading a v1-only label"
