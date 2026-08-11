"""`parked` must mean a human was asked (#1386).

`ACT_PARK` was defined in `observe.py` for months and never returned. Four branches wrote
`state=parked` through `ACT_NONE` instead, which sets `pending_mode=None` — so `park.sh` never ran
for any of them and `daemon._observe_one`'s `ACT_PARK` branch was unreachable dead code.

The consequence was a queue that claimed something false. A fork PR, a release-please PR, an item
with no `agent:*` label and an issue with neither `agent:ready` nor `agent:working` all showed as
`parked` — the state that everywhere else means "the pipeline stopped and posted a Decision Comment,
labelled it `needs-human`, assigned the owner and disarmed auto-merge". None of that had happened.
Nobody had been asked anything.

For three of those four, silence really is correct: a fork PR and a release-please PR are not the
pipeline's to comment on. So the fix is not "make them park" — it is to stop calling them parked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)


def d(snap):
    """Run the decision under standard TTLs."""
    return observe.decide(snap, **TTLS)


@pytest.mark.parametrize("snap,reason", [
    (observe.Snapshot(kind="pr", number=1, labels=frozenset({"agent:working"}), upstream=False),
     "not_admissible:fork_pr"),
    (observe.Snapshot(kind="pr", number=2, labels=frozenset({"agent:working"}),
                      branch="release-please--branches--main"),
     "not_admissible:release_pr"),
    (observe.Snapshot(kind="pr", number=3, labels=frozenset({"priority:low"})),
     "not_admissible:no_agent_label"),
    (observe.Snapshot(kind="issue", number=4, labels=frozenset({"agent:revise"})),
     "issue_not_ready"),
])
def test_not_ours_is_ignored_not_parked(snap, reason):
    """The four branches that never asked anybody anything."""
    got = d(snap)
    assert got.action == observe.ACT_NONE
    assert got.reason == reason
    assert got.next_state == db.STATE_IGNORED, (
        f"{reason} wrote `{got.next_state}` — `parked` claims a Decision Comment that was "
        "never posted"
    )


def test_a_real_hold_is_still_parked():
    """The distinction only means something if `parked` keeps its meaning.

    A hold label IS an escalation — placed by `park.sh` with a comment, or by the owner by hand.
    """
    snap = observe.Snapshot(kind="pr", number=5,
                            labels=frozenset({"agent:working", "needs-human"}))
    got = d(snap)
    assert got.next_state == db.STATE_PARKED
    assert got.reason == "human_hold"


def test_a_draft_is_still_parked_for_now():
    """Unchanged here deliberately.

    A draft is a genuine "stuck and nobody knows" state rather than a "not ours" one, and making it
    reachable again is #1393. Pinning the current behaviour keeps that a deliberate follow-up rather
    than something this change quietly altered.
    """
    snap = observe.Snapshot(kind="pr", number=6, labels=frozenset({"agent:working"}),
                            is_draft=True)
    assert d(snap).next_state == db.STATE_PARKED


def test_decide_has_no_park_action_to_return():
    """The constant is gone, so the dead branch cannot come back by accident.

    Escalation happens at DISPATCH — `act()` finds the ledger spent and queues `park.sh` through
    `_park()`. If `decide()` ever needs to escalate, the constant is re-added and wired in the same
    change, which is exactly what did not happen last time.
    """
    assert not hasattr(observe, "ACT_PARK")
    assert "ACT_PARK" not in (_V2 / "lemd" / "daemon.py").read_text()


def test_the_real_park_path_is_still_reachable():
    """`park.sh` must still run for the one thing that genuinely escalates: a spent budget."""
    daemon_src = (_V2 / "lemd" / "daemon.py").read_text()
    assert 'pending_mode="park"' in daemon_src
    assert "_exhausted" in daemon_src


def test_ignored_items_are_not_dispatchable(tmp_path):
    """An ignored item must not be picked up, and must not hold a slot or a wake."""
    conn = db.connect(tmp_path / "q.db")
    db.upsert_item(conn, kind="pr", number=9, state=db.STATE_IGNORED, pending_mode="start")
    assert [r["number"] for r in db.dispatchable(conn)] == []
    assert [r["number"] for r in db.due_items(conn)] == []
    assert db.wip_count(conn) == 0


def test_ignored_is_not_a_wait_state():
    """A wait state re-polls; `ignored` has nothing to wait for.

    Including it in `WAIT_STATES` would put every fork PR back on the TTL sweep for ever, which is
    the cost this state exists to avoid.
    """
    assert db.STATE_IGNORED not in db.WAIT_STATES
    assert db.STATE_IGNORED not in db.WIP_STATES
    assert db.STATE_IGNORED not in db.DISPATCHABLE_STATES


def test_an_ignored_item_can_still_be_revived():
    """Not terminal: label it properly and the next observation picks it up.

    `ignored` is deliberately absent from `TERMINAL_STATES` — an item that gains `agent:ready`
    tomorrow is work, and a state nothing can leave is how #1382's stranded branches happened.
    """
    assert db.STATE_IGNORED not in db.TERMINAL_STATES
    snap = observe.Snapshot(kind="issue", number=4, labels=frozenset({"agent:ready"}),
                            work_exists=False)
    assert d(snap).action == observe.ACT_DISPATCH
