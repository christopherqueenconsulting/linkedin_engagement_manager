"""The WIP gate's accounting: what it counts, what it excludes, and whether it says so (#1426).

On 2026-08-11 the pipeline dispatched nothing for over six hours. Three PRs (#1304, #1307, #1365)
were green on every required context with auto-merge armed and `mergeStateStatus: BLOCKED` — waiting
on a code-owner approval only the owner could give. At `LEMD_MAX_AGENTS=2` they filled the WIP gate,
so ~25 `agent:ready` issues each reached `action: dispatch, executed: false` and stopped, and the
only evidence was one log line reporting a count.

Two properties, both tested here rather than assumed:

* **A PR whose next move belongs to a HUMAN is not work in flight**, so it must not throttle starts.
  A PR blocked for a reason the pipeline CAN act on — a failing check, a conflict — still counts.
* **The exclusion is visible.** "The pipeline is not idle, it is unblocked" has to be readable off
  `lemd-decisions.ndjson`, because inferring it took hours the first time.

And one direction that must never invert: an UNREADABLE state counts as in flight. Failing closed
here costs a delayed start; failing open costs unbounded ones.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import daemon, db, github, observe  # noqa: E402
from lemd.config import load  # noqa: E402

TTLS = dict(ttl_ci=1800, ttl_review=3600, ttl_queue=900, ttl_parked=21600)
GREEN = github.ChecksState(failed=0, pending=0, total=6)


def _pr(**kw) -> observe.Snapshot:
    """A PR snapshot; only the fields under test vary."""
    base = dict(kind="pr", number=1, labels=frozenset({"agent:working"}), branch="feature/x",
                head_sha="abc", checks=GREEN, review_fresh=True, merge_state="CLEAN")
    base.update(kw)
    return observe.Snapshot(**base)


def _acting_daemon(tmp_path: Path, extra: str = "") -> daemon.Daemon:
    """A daemon allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n" + extra
    )
    return daemon.Daemon(load(tmp_path))


def _ledger(base: Path) -> list[dict]:
    """Every row the daemon has written to its decision ledger."""
    path = base / "logs" / "lemd-decisions.ndjson"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------- what the gate excludes

def test_the_excluded_states_are_exactly_the_ones_the_gate_does_not_count():
    """One set names the exclusion, the other the count — they may never overlap.

    Derived sets drift silently; this is the assertion that makes adding a state to `WIP_STATES`
    without removing it from `HUMAN_HELD_STATES` a build failure rather than a live regression.
    """
    assert db.HUMAN_HELD_STATES.isdisjoint(db.WIP_STATES)
    assert db.STATE_WAIT_OWNER_REVIEW in db.HUMAN_HELD_STATES
    assert db.STATE_PARKED in db.HUMAN_HELD_STATES
    # The omission direction, which disjointness alone does NOT catch and which is the exact shape
    # of #1426: a NEW wait state added to `WAIT_STATES` and to neither of these is silently not
    # counted by `wip_count()` AND not reported by `wip_excluded()` — a PR discounted invisibly,
    # which is the defect this file exists to close. Every waiting PR is either work the pipeline
    # is carrying or work a human owns; there is no third answer.
    assert db.WAIT_STATES - db.WIP_STATES == db.HUMAN_HELD_STATES


def test_wip_excluded_names_the_prs_and_the_state_that_explains_them(tmp_path):
    """The report is `(number, state)` — a bare count would still need a state dump to read."""
    conn = db.connect(tmp_path / "q.db")
    db.upsert_item(conn, kind="pr", number=1304, state=db.STATE_WAIT_OWNER_REVIEW)
    db.upsert_item(conn, kind="pr", number=1307, state=db.STATE_PARKED)
    # Counted, so NOT excluded: the pipeline's own work, and a queue entry that is not work at all.
    db.upsert_item(conn, kind="pr", number=1365, state=db.STATE_WAIT_CI)
    db.upsert_item(conn, kind="issue", number=1426, state=db.STATE_READY, pending_mode="start")

    assert db.wip_excluded(conn) == [
        {"number": 1304, "state": db.STATE_WAIT_OWNER_REVIEW},
        {"number": 1307, "state": db.STATE_PARKED},
    ]
    assert db.wip_count(conn) == 1
    conn.close()


# ------------------------------------------------------- the 2026-08-11 shape, end to end

def test_n_prs_parked_on_a_human_at_max_agents_n_still_dispatches(tmp_path, monkeypatch):
    """The incident, reproduced at its own numbers: 3 parked PRs, `LEMD_MAX_AGENTS=3`.

    The regression this guards is not the count in `wip_count()` but the end of the chain — that
    `act()` actually spawns the start. A gate that discounts the right PRs and still refuses to
    dispatch is the same six-hour outage with better bookkeeping.
    """
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=3\n")
    for number in (1304, 1307, 1365):
        db.upsert_item(dm.conn, kind="pr", number=number, state=db.STATE_WAIT_OWNER_REVIEW,
                       branch=f"feature/x{number}")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)
    monkeypatch.setattr(dm.sup, "dispatch_agent", lambda **kw: object())

    assert dm.act() == 1
    executed = [r for r in _ledger(dm.cfg.base) if r.get("executed")]
    assert [(r["kind"], r["number"], r["mode"]) for r in executed] == [("issue", 1416, "start")]
    dm.conn.close()


def test_prs_the_pipeline_can_act_on_still_close_the_gate(tmp_path, monkeypatch):
    """The other half of the criterion: only a HUMAN-owned next move is discounted.

    `awaiting_ci` is a PR the pipeline is carrying — a failing check it will fix, a queue it is
    waiting on. Excluding those would uncouple starts from merge throughput and turn a 25-issue
    backlog into 25 open PRs against a queue that merges one at a time.
    """
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=3\n")
    for number, state in ((1304, db.STATE_WAIT_CI), (1307, db.STATE_WAIT_QUEUE),
                          (1365, db.STATE_WAIT_REVIEW)):
        db.upsert_item(dm.conn, kind="pr", number=number, state=state,
                       branch=f"feature/x{number}")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)
    monkeypatch.setattr(dm.sup, "dispatch_agent", lambda **kw: object())

    assert dm.act() == 0
    assert [r["refused_by"] for r in _ledger(dm.cfg.base) if r.get("stage") == "act"] \
        == ["wip_limit"]
    dm.conn.close()


# ------------------------------------------------------------------------ visibility

def test_the_hold_records_which_prs_it_is_not_counting(tmp_path, monkeypatch):
    """The ledger must distinguish "saturated" from "unblocked but throttled by its own count"."""
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=1\n")
    db.upsert_item(dm.conn, kind="pr", number=1365, state=db.STATE_WAIT_CI, branch="feature/a")
    db.upsert_item(dm.conn, kind="pr", number=1304, state=db.STATE_WAIT_OWNER_REVIEW,
                   branch="feature/b")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)

    assert dm.act() == 0
    gate = [r for r in _ledger(dm.cfg.base) if r.get("stage") == "wip_gate"]
    assert len(gate) == 1
    assert gate[0]["wip"] == 1 and gate[0]["max_agents"] == 1
    assert gate[0]["excluded"] == [{"number": 1304, "state": db.STATE_WAIT_OWNER_REVIEW}]
    dm.conn.close()


def test_a_standing_hold_is_written_once_and_a_changed_shape_is_written_again(tmp_path,
                                                                              monkeypatch):
    """A gate that stays shut is a fact, not an event — but a gate that shuts again is."""
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=1\n")
    db.upsert_item(dm.conn, kind="pr", number=1365, state=db.STATE_WAIT_CI, branch="feature/a")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)

    for _ in range(4):
        dm.act()
    assert len([r for r in _ledger(dm.cfg.base) if r.get("stage") == "wip_gate"]) == 1

    # A PR the gate now discounts changes the answer, so the row is re-written.
    db.upsert_item(dm.conn, kind="pr", number=1304, state=db.STATE_WAIT_OWNER_REVIEW,
                   branch="feature/b")
    dm.act()
    gate = [r for r in _ledger(dm.cfg.base) if r.get("stage") == "wip_gate"]
    assert len(gate) == 2
    assert gate[-1]["excluded"] == [{"number": 1304, "state": db.STATE_WAIT_OWNER_REVIEW}]
    dm.conn.close()


def test_an_empty_backlog_under_a_still_shut_gate_is_not_a_second_hold(tmp_path, monkeypatch):
    """Holding nothing is not the same as holding nobody back — the gate is what changed or didn't.

    A pass with no dispatchable start refuses nothing, so keying the reset on "did we hold anything"
    forgets a hold that is still standing: the backlog empties and refills, and an identical row is
    written again for one uninterrupted hold. The gate, not the backlog, is the event.
    """
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=1\n")
    db.upsert_item(dm.conn, kind="pr", number=1365, state=db.STATE_WAIT_CI, branch="feature/a")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)

    assert dm.act() == 0
    # The backlog drains away while the PR is still in flight — nothing to hold, gate still shut.
    db.force_state(dm.conn, db.get_item(dm.conn, "issue", 1416)["id"], db.STATE_IGNORED, dirty=0)
    assert dm.act() == 0
    # ...and refills against the same unchanged gate.
    db.upsert_item(dm.conn, kind="issue", number=1417, state=db.STATE_READY, pending_mode="start")
    assert dm.act() == 0

    assert len([r for r in _ledger(dm.cfg.base) if r.get("stage") == "wip_gate"]) == 1
    dm.conn.close()


def test_nothing_is_written_when_the_gate_is_open(tmp_path, monkeypatch):
    """Telemetry about a hold that did not happen is noise that hides the holds that did."""
    dm = _acting_daemon(tmp_path, extra="LEMD_MAX_AGENTS=3\n")
    db.upsert_item(dm.conn, kind="issue", number=1416, state=db.STATE_READY, pending_mode="start")
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)
    monkeypatch.setattr(dm.sup, "dispatch_agent", lambda **kw: object())

    assert dm.act() == 1
    assert not [r for r in _ledger(dm.cfg.base) if r.get("stage") == "wip_gate"]
    dm.conn.close()


# ------------------------------------------------------------------------ fail closed

def test_an_unreadable_merge_state_counts_as_in_flight():
    """Toward the throttle, never toward unbounded starts.

    `merge_state_unknown` waits in `awaiting_ci`, which IS in `WIP_STATES`. If a future change routed
    it to a human-held state instead, an unreadable GitHub would silently uncap the start lane.
    """
    for state in ("UNKNOWN", ""):
        got = observe.decide(_pr(merge_state=state), **TTLS)
        assert got.next_state in db.WIP_STATES
        assert got.next_state not in db.HUMAN_HELD_STATES


def test_an_unrecognised_merge_state_counts_as_in_flight():
    """A value GitHub adds later must not read as "nothing to count"."""
    got = observe.decide(_pr(merge_state="SOMETHING_NEW"), **TTLS)
    assert got.reason == "merge_state_unrecognised"
    assert got.next_state in db.WIP_STATES


def test_an_unreadable_snapshot_counts_as_in_flight():
    """`readable=False` is the whole-item version of the same rule."""
    got = observe.decide(_pr(readable=False), **TTLS)
    assert got.reason == "github_unreadable"
    assert got.next_state in db.WIP_STATES


def test_armed_and_blocked_with_unreadable_checks_is_not_discounted():
    """The owner-review branch requires checks it could READ and that were green.

    `checks=None` means the rollup could not be read, which must not be mistaken for "everything
    reported, so the only thing left is a human" — that would discount a PR the pipeline may still
    owe a fix.
    """
    got = observe.decide(_pr(merge_state="BLOCKED", auto_merge=True, checks=None), **TTLS)
    assert got.next_state in db.WIP_STATES
    assert got.next_state not in db.HUMAN_HELD_STATES
