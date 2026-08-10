"""Regression tests for defects found by an unbiased adversarial review of the v2 plan.

Each test pins one confirmed finding. They live apart from the main v2 state tests because each is
a specific bug with a specific failure sequence, and a future refactor that reintroduces one should
fail on a test that names it rather than on a vague invariant.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_V2 = REPO / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import daemon, db, observe  # noqa: E402
from lemd.config import load  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """A fresh queue database."""
    c = db.connect(tmp_path / "queue.db")
    yield c
    c.close()


# ---------------------------------------------------------------- H6a: double dispatch


def test_observation_cannot_walk_a_running_item_back_to_ready(conn):
    """The finding: `upsert_item` always wrote `state`, so an event could un-run a running item.

    Sequence that produced a second agent on one branch: item is `running` → webhook arrives →
    `observe()` upserts it as `ready` → next pass claims it → `start_run()` spawns a SECOND
    `claude -p` on the same branch and worktree. The branch index cannot catch this: it constrains
    two ROWS sharing a branch, not one row being recycled.
    """
    i = db.upsert_item(conn, kind="pr", number=100, state=db.STATE_READY, branch="feature/x")
    assert db.claim_item(conn, i) is True
    db.start_run(conn, item_id=i, mode="fix", pid=os.getpid())
    assert db.get_item(conn, "pr", 100)["state"] == db.STATE_RUNNING

    db.upsert_item(conn, kind="pr", number=100, state=db.STATE_READY, head_sha="newsha")

    row = db.get_item(conn, "pr", 100)
    assert row["state"] == db.STATE_RUNNING, "observation must not move an owned item"
    assert row["head_sha"] == "newsha", "...but the observation itself must still be recorded"
    assert db.claim_item(conn, i) is False


def test_observation_cannot_move_a_claimed_item_either(conn):
    """`claimed` is the window between winning the claim and spawning — equally not free."""
    i = db.upsert_item(conn, kind="issue", number=101, state=db.STATE_READY, branch="feature/y")
    db.claim_item(conn, i)
    db.upsert_item(conn, kind="issue", number=101, state=db.STATE_PARKED)
    assert db.get_item(conn, "issue", 101)["state"] == db.STATE_CLAIMED


def test_force_state_is_the_explicit_escape_hatch(conn):
    """The run lifecycle legitimately moves a running item; it must say so out loud."""
    i = db.upsert_item(conn, kind="pr", number=102, state=db.STATE_READY, branch="feature/z")
    db.claim_item(conn, i)
    db.start_run(conn, item_id=i, mode="start", pid=os.getpid())
    db.force_state(conn, i, db.STATE_WAIT_CI, wake_at=123)
    row = db.get_item(conn, "pr", 102)
    assert row["state"] == db.STATE_WAIT_CI and row["wake_at"] == 123


def test_upsert_still_moves_items_that_nothing_owns(conn):
    """The guard must not freeze ordinary transitions."""
    db.upsert_item(conn, kind="pr", number=103, state=db.STATE_WAIT_CI)
    db.upsert_item(conn, kind="pr", number=103, state=db.STATE_MERGED)
    assert db.get_item(conn, "pr", 103)["state"] == db.STATE_MERGED


# ---------------------------------------------------------------- H6b: permanent wedge


def test_pid_liveness_uses_start_time_not_bare_pid(conn):
    """A recycled pid must read as dead, or the run it replaced stays 'alive' forever."""
    live = os.getpid()
    assert db._pid_alive(live, db.pid_starttime(live)) is True
    # Same pid, a start time that cannot match: this is the recycled-pid case.
    assert db._pid_alive(live, "999999999999") is False


def test_pid_starttime_is_none_for_a_dead_process():
    assert db.pid_starttime(999_999_999) is None


def test_startup_closes_a_run_whose_pid_was_recycled(conn):
    """Without the start-time pairing this run stays open and its item never leaves `running`."""
    i = db.upsert_item(conn, kind="pr", number=110, state=db.STATE_READY, branch="feature/r")
    db.claim_item(conn, i)
    db.start_run(conn, item_id=i, mode="fix", pid=os.getpid())
    # Simulate reuse: the pid is alive (it is us) but is a different process than the one recorded.
    conn.execute("UPDATE runs SET pid_start='1' WHERE item_id=?", (i,))
    stats = db.startup_recover(conn)
    assert stats["runs_closed"] == 1
    assert db.get_item(conn, "pr", 110)["state"] == db.STATE_READY


def test_running_has_a_ttl_edge(conn):
    """`running` had neither an event edge nor a TTL edge — a dead end holding its branch."""
    i = db.upsert_item(conn, kind="pr", number=111, state=db.STATE_READY, branch="feature/t")
    db.claim_item(conn, i)
    db.start_run(conn, item_id=i, mode="start", pid=os.getpid(), timeout_s=600, now=1000)
    assert db.get_item(conn, "pr", 111)["wake_at"] == 1600
    assert db.due_items(conn, now=1500) == []
    assert [r["number"] for r in db.due_items(conn, now=1700)] == [111]


def test_ttl_sweep_still_excludes_finished_items(conn):
    """`wake_at` is not cleared on exit, so an unscoped sweep would re-poll merged PRs forever."""
    db.upsert_item(conn, kind="pr", number=112, state=db.STATE_MERGED, wake_at=1)
    assert db.due_items(conn, now=9999) == []


# ---------------------------------------------------------------- L4: dedupe bypass


def test_delivery_id_cannot_be_null(conn):
    """SQLite treats NULLs as distinct, so a nullable UNIQUE column is not a dedupe key at all."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events(delivery_id, event, received_at) VALUES (NULL, 'issues', 1)"
        )


# ---------------------------------------------------------------- M6: concurrency cap


def test_agent_cap_does_not_inherit_v1s_max_agents(tmp_path):
    """v1 runs MAX_AGENTS=5; v2 runs its agents genuinely concurrently in one cgroup."""
    (tmp_path / "config.env").write_text("MAX_AGENTS=5\n")
    assert load(tmp_path).max_agents == 3


def test_lemd_max_agents_is_the_knob(tmp_path):
    (tmp_path / "config.env").write_text("MAX_AGENTS=5\nLEMD_MAX_AGENTS=4\n")
    assert load(tmp_path).max_agents == 4


# ---------------------------------------------------------------- H4 / M6 / M7: units


def _unit(name: str) -> str:
    return (_V2 / "systemd" / name).read_text()


def test_daemon_stop_does_not_sigkill_agent_children():
    """KillMode=mixed SIGKILLs the cgroup at the stop timeout; agent runs reach 45 minutes."""
    text = _unit("lem-agentd.service")
    # Match DIRECTIVES at line start — the rationale comment names the rejected mode, and a naive
    # substring check reads that explanation as the setting itself.
    directives = re.findall(r"^KillMode=(\w+)", text, re.M)
    assert directives == ["process"]


def test_daemon_has_one_memory_envelope_not_two():
    """A service MemoryMax under a slice makes the daemon+agents share the smaller ceiling."""
    text = _unit("lem-agentd.service")
    assert "Slice=lem-agent.slice" in text
    assert not re.search(r"^MemoryMax=", text, re.M)


def test_watchdog_runs_as_root():
    """A non-root `systemctl restart` of a system unit goes through polkit and fails silently."""
    assert "User=root" in _unit("lem-agentd-watchdog.service")
    assert "User=lem" not in _unit("lem-agentd-watchdog.service")


# ---------------------------------------------------------------- L3


def test_heartbeat_tempfile_does_not_replace_the_suffix(tmp_path):
    """`with_suffix('.new')` would write `lemd.new`, colliding with any future lemd.* file."""
    from lemd import capacity

    p = tmp_path / "lemd.heartbeat"
    capacity.heartbeat(p, now=5)
    assert p.read_text() == "5"
    assert not (tmp_path / "lemd.new").exists()


# ---------------------------------------------------------------- #1348: the ledger lied

def _ledger(base) -> list[dict]:
    """Every row the daemon has written to its decision ledger."""
    path = base / "logs" / "lemd-decisions.ndjson"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _acting_daemon(tmp_path, extra: str = ""):
    """A daemon that is allowed to act (not shadow), on a throwaway queue."""
    (tmp_path / "config.env").write_text(
        f"LEMD_DB={tmp_path}/queue.db\nLEMD_SHADOW=0\n"
        "SLUG=christopherqueenconsulting/linkedin_engagement_manager\n" + extra
    )
    return daemon.Daemon(load(tmp_path))


def test_a_held_start_is_never_recorded_as_a_dispatch(tmp_path):
    """The defect: `_emit` logged `decide`'s INTENT, and `act`'s gates run afterwards.

    Under `LEMD_HOLD_STARTS=1` with a 48-issue backlog the ledger gained ~40 `action=dispatch`
    rows a pass while the pipeline spawned nothing, for as long as the hold was set. Any
    dispatch-rate or success-rate figure computed from the ledger was inflated by the entire
    backlog, and the one true explanation appeared only as a single human-readable log line.
    """
    dm = _acting_daemon(tmp_path, extra="LEMD_HOLD_STARTS=1\n")
    for number in (2001, 2002, 2003):
        i = db.upsert_item(dm.conn, kind="issue", number=number, state=db.STATE_READY)
        db.upsert_item(dm.conn, kind="issue", number=number, state=db.STATE_READY,
                       pending_mode="start")
        assert i

    assert dm.act() == 0
    rows = _ledger(dm.cfg.base)
    assert rows, "a refusal must leave evidence, not silence"
    assert not [r for r in rows if r.get("executed")], "nothing ran, so nothing may read as run"
    assert {r["number"] for r in rows if r.get("refused_by") == "hold_starts"} == {2001, 2002, 2003}
    dm.conn.close()


def test_a_standing_refusal_is_recorded_once_not_every_pass(tmp_path):
    """Bounded volume is half the fix.

    A refusal that has not changed is a fact about the gate, not an event. Re-stating it every
    pass reproduces exactly the noise the once-per-pass hold log line was introduced to remove —
    only in the file consumers parse.
    """
    dm = _acting_daemon(tmp_path, extra="LEMD_HOLD_STARTS=1\n")
    db.upsert_item(dm.conn, kind="issue", number=2004, state=db.STATE_READY, pending_mode="start")

    dm.act()
    after_first = len(_ledger(dm.cfg.base))
    for _ in range(5):
        dm.act()
    assert len(_ledger(dm.cfg.base)) == after_first
    dm.conn.close()


def test_an_executed_dispatch_is_marked_executed(tmp_path, monkeypatch):
    """The other half: a real launch must be distinguishable from an intent."""
    dm = _acting_daemon(tmp_path)
    db.upsert_item(dm.conn, kind="pr", number=2005, state=db.STATE_READY,
                   branch="feature/x", pending_mode="merge")
    monkeypatch.setattr(dm.sup, "dispatch_gh", lambda **kw: object())
    monkeypatch.setattr(dm.sup, "v1_slots_busy", lambda: 0)

    assert dm.act() == 1
    executed = [r for r in _ledger(dm.cfg.base) if r.get("executed")]
    assert [(r["kind"], r["number"], r["mode"]) for r in executed] == [("pr", 2005, "merge")]
    assert executed[0]["stage"] == "act"
    dm.conn.close()


def test_observation_rows_are_intent_only(tmp_path):
    """`stage=observe` is where `action=dispatch` lives, and it is never an execution."""
    dm = _acting_daemon(tmp_path, extra="LEMD_HOLD_STARTS=1\n")
    dm._emit(
        {"kind": "issue", "number": 2006, "state": db.STATE_READY},
        observe.Snapshot(kind="issue", number=2006, work_exists=False,
                         labels=frozenset({"agent:ready"})),
        observe.decide(
            observe.Snapshot(kind="issue", number=2006, work_exists=False,
                             labels=frozenset({"agent:ready"})),
            ttl_ci=1, ttl_review=1, ttl_queue=1, ttl_parked=1),
        db.STATE_READY,
    )
    row = _ledger(dm.cfg.base)[-1]
    assert (row["action"], row["stage"], row["executed"]) == ("dispatch", "observe", False)
    dm.conn.close()
