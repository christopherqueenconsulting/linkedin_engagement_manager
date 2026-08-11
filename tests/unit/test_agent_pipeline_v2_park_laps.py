"""The pipeline must be able to stop asking (#1390).

Every dead end was "park and ask the human", forever. Un-parking resets the ledger and buys N more
runs; if the work fails again it parks again with the same question. Nothing counted the laps —
`items.parked_reason` holds only the latest — which is why the #1380 treadmill stayed invisible for
a day while it cost a human decision and two model sessions per lap.

The two design choices worth arguing about, both tested here:

* A lap is keyed on (item, reason, HEAD). A re-park at the same head is the same park being
  re-observed, not a new lap — otherwise the 6-hourly `ttl_parked` re-decision inflates every
  counter on its own.
* The give-up test fires at the UN-PARK, not at the park. That is where the loop actually turns: the
  park is the symptom, the ledger reset is what starts the next lap.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import answers, db, observe  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
ANSWER = answers.Answer("a1", "answer", "1B")


def held(**kw) -> observe.Snapshot:
    """A parked PR with an actionable owner answer waiting."""
    base = dict(kind="pr", number=1, labels=frozenset({"needs-human", "agent:blocked"}),
                branch="feature/x", head_sha="abc", answer=ANSWER,
                parked_reason="selfreview_exhausted")
    base.update(kw)
    return observe.Snapshot(**base)


def d(snap, laps=3):
    """Run the decision with the give-up rule enabled."""
    return observe.decide(snap, max_park_laps=laps, **TTLS)


# ---------------------------------------------------------------- counting


def test_a_lap_is_counted_once_per_head(tmp_path):
    """A re-park at the same head is the same park being re-observed."""
    conn = db.connect(tmp_path / "q.db")
    for _ in range(4):
        db.record_park(conn, "pr", 1, "selfreview_exhausted", "sha-a")
    assert db.park_laps(conn, "pr", 1, "selfreview_exhausted") == 1


def test_a_new_head_is_a_new_lap(tmp_path):
    """A different head means the work moved and the question was genuinely asked again."""
    conn = db.connect(tmp_path / "q.db")
    for sha in ("sha-a", "sha-b", "sha-c"):
        db.record_park(conn, "pr", 1, "selfreview_exhausted", sha)
    assert db.park_laps(conn, "pr", 1, "selfreview_exhausted") == 3


def test_laps_are_counted_per_reason(tmp_path):
    """Two different problems are not a loop.

    A PR that parks once for a lint failure and once for an exhausted review has not gone round; it
    has had two things wrong with it.
    """
    conn = db.connect(tmp_path / "q.db")
    db.record_park(conn, "pr", 1, "selfreview_exhausted", "sha-a")
    db.record_park(conn, "pr", 1, "docfix_exhausted", "sha-b")
    assert db.park_laps(conn, "pr", 1, "selfreview_exhausted") == 1
    assert db.park_laps(conn, "pr", 1, "docfix_exhausted") == 1


def test_laps_survive_an_unpark(tmp_path):
    """The release must not reset the counter, or the loop can never be detected."""
    conn = db.connect(tmp_path / "q.db")
    db.record_park(conn, "pr", 1, "selfreview_exhausted", "sha-a")
    db.record_unpark(conn, "pr", 1, "selfreview_exhausted", "sha-a")
    db.record_park(conn, "pr", 1, "selfreview_exhausted", "sha-b")
    assert db.park_laps(conn, "pr", 1, "selfreview_exhausted") == 2


def test_clearing_history_resets_the_counter(tmp_path):
    """The owner's escape hatch."""
    conn = db.connect(tmp_path / "q.db")
    db.record_park(conn, "pr", 1, "selfreview_exhausted", "sha-a")
    db.clear_park_history(conn, "pr", 1)
    assert db.park_laps(conn, "pr", 1, "selfreview_exhausted") == 0


# ---------------------------------------------------------------- the decision


def test_below_the_limit_an_answer_still_un_parks():
    """The rule must not change anything for work that is making progress."""
    assert d(held(park_laps=2)).action == observe.ACT_UNPARK


def test_at_the_limit_the_pipeline_stops_asking():
    """The whole point: the fourth identical question is not asked."""
    got = d(held(park_laps=3))
    assert (got.action, got.mode, got.reason) == (
        observe.ACT_ABANDON, "abandon", "park_laps_exhausted")
    assert got.details["laps"] == 3


def test_the_abandon_names_the_reason_it_kept_parking_for():
    """An abandon that cannot say what looped is not actionable."""
    assert d(held(park_laps=3)).park_reason == "selfreview_exhausted"


def test_the_rule_is_off_by_default():
    """`max_park_laps=0` disables it, which is what any caller that has not opted in gets."""
    assert observe.decide(held(park_laps=99), max_park_laps=0, **TTLS).action == observe.ACT_UNPARK


def test_an_ambiguous_answer_does_not_trip_the_limit():
    """`hold` and `question` leave the work parked; they are not a lap and not a give-up.

    Abandoning on a reply that said "wait" would punish the owner for answering carefully.
    """
    got = d(held(park_laps=5, answer=answers.Answer("a1", "hold", "not yet")))
    assert got.action == observe.ACT_NONE
    assert got.reason == "human_hold:hold"


def test_no_answer_at_the_limit_just_stays_parked():
    """The give-up fires at the UN-PARK. With nobody answering, there is no lap to refuse."""
    got = d(held(park_laps=9, answer=None))
    assert got.action == observe.ACT_NONE
    assert got.reason == "human_hold"


def test_an_armed_pr_is_still_disarmed_first():
    """Safety outranks the give-up: an abandoned PR must not merge on a gate that clears later."""
    assert d(held(park_laps=9, auto_merge=True)).action == observe.ACT_DISARM


# ---------------------------------------------------------------- the state


def test_abandoned_is_terminal_for_the_pipeline(tmp_path):
    """Not dispatchable, not waited on, not counted as work in flight."""
    conn = db.connect(tmp_path / "q.db")
    db.upsert_item(conn, kind="pr", number=9, state=db.STATE_ABANDONED, pending_mode="start")
    assert db.dispatchable(conn) == []
    assert db.due_items(conn) == []
    assert db.wip_count(conn) == 0
    assert db.STATE_ABANDONED in db.TERMINAL_STATES


def test_the_action_never_closes_anything():
    """Closing is a judgement about the WORK; abandoning is only about the pipeline's progress.

    Conflating them would let a loop in the runner quietly discard someone's issue.
    """
    src = (_V2 / "actions" / "abandon.sh").read_text()
    assert "gh issue close" not in src
    assert "gh pr close" not in src
    assert "NEVER CLOSES ANYTHING" in src


def test_the_action_creates_its_label_before_using_it():
    """A missing label makes `gh --add-label` fail the WHOLE edit, silently (#1228).

    For this action that would mean no label, no assignee, and an abandon nobody can see — the one
    failure mode it cannot have.
    """
    src = (_V2 / "actions" / "abandon.sh").read_text()
    assert src.index("gh label create") < src.index('--add-label "agent:abandoned"')


def test_the_action_keeps_the_hold_label_on():
    """Dropping `needs-human` would take the item out of every "waiting on me" query."""
    assert '--add-label "needs-human"' in (_V2 / "actions" / "abandon.sh").read_text()


def test_status_surfaces_abandoned_items():
    """An item that stops asking is invisible unless something says it exists.

    Silence is a worse failure than the treadmill it replaces, so this must WARN, not just count.
    """
    src = (_V2.parent / "status.sh").read_text()
    assert "state='abandoned'" in src
    assert 'warn "$V2_ABANDONED item(s) ABANDONED' in src


def test_the_daemon_revives_an_item_whose_label_was_removed():
    """The escape hatch the abandon comment promises, and the history clear that makes it real.

    Leaving the laps behind would abandon the item again on its very next park.
    """
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert '("agent:abandoned", "pr")' in src
    assert "clear_park_history" in src
    assert 'existing["state"] == db.STATE_ABANDONED' in src
