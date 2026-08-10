"""Tests for the v2 daemon's state layer (`scripts/agent-pipeline/v2/lemd`).

These cover the invariants the v2 design leans on, each of which corresponds to a measured v1
failure: atomic claims (v1 needed flocks and still re-armed auto-merge on a PR another slot was
parking), wait states being invisible to the scheduler (v1 burned 75-84% of ticks re-polling),
delivery dedup (GitHub retries webhooks), and crash recovery (v1 left `agent:working` claims
stranded for hours).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import capacity, db  # noqa: E402
from lemd.config import load, parse_env_file  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """A fresh queue database."""
    c = db.connect(tmp_path / "queue.db")
    yield c
    c.close()


# ---------------------------------------------------------------- schema / items


def test_connect_is_idempotent_and_records_schema_version(tmp_path):
    p = tmp_path / "q.db"
    c1 = db.connect(p)
    db.upsert_item(c1, kind="pr", number=1, state=db.STATE_READY)
    c1.close()
    c2 = db.connect(p)  # reopening must not wipe or fail
    assert db.kv_get(c2, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.get_item(c2, "pr", 1) is not None
    c2.close()


def test_upsert_creates_then_patches_without_blanking(conn):
    db.upsert_item(conn, kind="pr", number=7, state=db.STATE_READY, branch="feature/x",
                   head_sha="aaa")
    # A webhook carrying only a head SHA must not erase the branch a reconcile established.
    db.upsert_item(conn, kind="pr", number=7, state=db.STATE_WAIT_CI, head_sha="bbb")
    row = db.get_item(conn, "pr", 7)
    assert row["branch"] == "feature/x"
    assert row["head_sha"] == "bbb"
    assert row["state"] == db.STATE_WAIT_CI


def test_unknown_fields_are_ignored_not_injected(conn):
    """Column allow-listing keeps an event payload from becoming an UPDATE statement."""
    db.upsert_item(conn, kind="pr", number=8, state=db.STATE_READY, bogus="; DROP TABLE items")
    assert db.get_item(conn, "pr", 8) is not None


# ---------------------------------------------------------------- claims


def test_claim_succeeds_once_then_refuses(conn):
    i = db.upsert_item(conn, kind="issue", number=10, state=db.STATE_READY)
    assert db.claim_item(conn, i) is True
    assert db.claim_item(conn, i) is False  # already claimed


def test_claim_refuses_a_dirty_item(conn):
    """An event arrived: the item must be re-observed before anything acts on it."""
    i = db.upsert_item(conn, kind="pr", number=11, state=db.STATE_READY, dirty=1)
    assert db.claim_item(conn, i) is False


def test_one_active_run_per_branch(conn):
    """The partial unique index makes two workers on one branch unrepresentable."""
    a = db.upsert_item(conn, kind="issue", number=12, state=db.STATE_READY, branch="feature/same")
    b = db.upsert_item(conn, kind="pr", number=13, state=db.STATE_READY, branch="feature/same")
    assert db.claim_item(conn, a) is True
    assert db.claim_item(conn, b) is False


def test_branch_frees_when_the_first_item_leaves_active_states(conn):
    a = db.upsert_item(conn, kind="issue", number=14, state=db.STATE_READY, branch="feature/f")
    b = db.upsert_item(conn, kind="pr", number=15, state=db.STATE_READY, branch="feature/f")
    assert db.claim_item(conn, a) is True
    db.upsert_item(conn, kind="issue", number=14, state=db.STATE_MERGED)
    assert db.claim_item(conn, b) is True


def test_null_branch_items_do_not_block_each_other(conn):
    """A partial index over NULL branches must not serialise unrelated issues."""
    a = db.upsert_item(conn, kind="issue", number=16, state=db.STATE_READY)
    b = db.upsert_item(conn, kind="issue", number=17, state=db.STATE_READY)
    assert db.claim_item(conn, a) is True
    assert db.claim_item(conn, b) is True


# ---------------------------------------------------------------- scheduling views


def test_wait_states_are_invisible_to_the_scheduler(conn):
    """The core v2 economy: waiting costs zero scheduler attention."""
    db.upsert_item(conn, kind="pr", number=20, state=db.STATE_READY)
    for n, s in ((21, db.STATE_WAIT_CI), (22, db.STATE_WAIT_QUEUE), (23, db.STATE_WAIT_REVIEW),
                 (24, db.STATE_PARKED), (25, db.STATE_MERGED)):
        db.upsert_item(conn, kind="pr", number=n, state=s)
    assert [r["number"] for r in db.dispatchable(conn)] == [20]


def test_dispatchable_orders_by_priority_then_age(conn):
    db.upsert_item(conn, kind="issue", number=30, state=db.STATE_READY, priority=2, ready_since=100)
    db.upsert_item(conn, kind="issue", number=31, state=db.STATE_READY, priority=0, ready_since=500)
    db.upsert_item(conn, kind="issue", number=32, state=db.STATE_READY, priority=2, ready_since=50)
    assert [r["number"] for r in db.dispatchable(conn)] == [31, 32, 30]


def test_due_items_returns_only_expired_ttls(conn):
    db.upsert_item(conn, kind="pr", number=40, state=db.STATE_WAIT_CI, wake_at=100)
    db.upsert_item(conn, kind="pr", number=41, state=db.STATE_WAIT_QUEUE, wake_at=9_999_999_999)
    db.upsert_item(conn, kind="pr", number=42, state=db.STATE_READY)  # no TTL at all
    assert [r["number"] for r in db.due_items(conn, now=200)] == [40]


def test_due_items_drops_an_item_that_left_its_wait_state(conn):
    """Nothing clears `wake_at` on a transition, so the TTL sweep must be scoped by STATE.

    Without that scope a merged PR keeps its expired `wake_at` forever and is re-polled on every
    pass — the unconditional re-polling v2 exists to remove, growing by one row per merged PR.
    """
    db.upsert_item(conn, kind="pr", number=43, state=db.STATE_WAIT_CI, wake_at=100)
    db.upsert_item(conn, kind="pr", number=43, state=db.STATE_MERGED)  # wake_at deliberately kept
    assert db.get_item(conn, "pr", 43)["wake_at"] == 100
    assert db.due_items(conn, now=200) == []
    # A ready item carrying a stale TTL is the scheduler's business, not the TTL sweep's.
    db.upsert_item(conn, kind="pr", number=44, state=db.STATE_READY, wake_at=100)
    assert db.due_items(conn, now=200) == []


# ---------------------------------------------------------------- events


def test_duplicate_delivery_is_ignored(conn):
    kw = dict(event="pull_request", action="opened", number=1, head_sha="a", payload="{}")
    assert db.record_event(conn, delivery_id="guid-1", **kw) is True
    assert db.record_event(conn, delivery_id="guid-1", **kw) is False  # GitHub retry / replay
    assert len(db.unprocessed_events(conn)) == 1


def test_events_process_in_arrival_order_and_clear(conn):
    for n in range(3):
        db.record_event(conn, delivery_id=f"g{n}", event="issues", action="labeled",
                        number=n, head_sha=None, payload=json.dumps({"n": n}))
    rows = db.unprocessed_events(conn)
    assert [r["number"] for r in rows] == [0, 1, 2]
    db.mark_events_processed(conn, [r["id"] for r in rows])
    assert db.unprocessed_events(conn) == []


# ---------------------------------------------------------------- crash recovery


def test_startup_releases_stranded_claims(conn):
    i = db.upsert_item(conn, kind="issue", number=50, state=db.STATE_READY, branch="feature/c")
    db.claim_item(conn, i)  # daemon died here, before spawning
    stats = db.startup_recover(conn)
    assert stats["claims_released"] == 1
    row = db.get_item(conn, "issue", 50)
    assert row["state"] == db.STATE_READY and row["dirty"] == 1
    # ...and the branch is usable again.
    assert db.claim_item(conn, i) is False  # still dirty: must be re-observed first


def test_startup_closes_runs_whose_process_is_gone(conn):
    i = db.upsert_item(conn, kind="pr", number=51, state=db.STATE_READY, branch="feature/d")
    db.claim_item(conn, i)
    db.start_run(conn, item_id=i, mode="fix", pid=999_999_999)  # certainly dead
    stats = db.startup_recover(conn)
    assert stats["runs_closed"] == 1
    assert conn.execute("SELECT rc FROM runs WHERE item_id=?", (i,)).fetchone()["rc"] == -9
    assert db.get_item(conn, "pr", 51)["state"] == db.STATE_READY


def test_startup_leaves_a_live_run_alone(conn):
    import os

    i = db.upsert_item(conn, kind="pr", number=52, state=db.STATE_READY, branch="feature/e")
    db.claim_item(conn, i)
    db.start_run(conn, item_id=i, mode="start", pid=os.getpid())  # this test process is alive
    stats = db.startup_recover(conn)
    assert stats["runs_closed"] == 0
    assert db.get_item(conn, "pr", 52)["state"] == db.STATE_RUNNING


def test_finish_run_records_rc(conn):
    i = db.upsert_item(conn, kind="pr", number=53, state=db.STATE_READY)
    r = db.start_run(conn, item_id=i, mode="docfix")
    db.finish_run(conn, r, rc=0)
    row = conn.execute("SELECT rc, ended_at FROM runs WHERE id=?", (r,)).fetchone()
    assert row["rc"] == 0 and row["ended_at"] is not None


def test_counts_by_state(conn):
    db.upsert_item(conn, kind="pr", number=60, state=db.STATE_READY)
    db.upsert_item(conn, kind="pr", number=61, state=db.STATE_READY)
    db.upsert_item(conn, kind="pr", number=62, state=db.STATE_WAIT_CI)
    assert db.counts_by_state(conn) == {db.STATE_READY: 2, db.STATE_WAIT_CI: 1}


# ---------------------------------------------------------------- config


def test_parse_env_file_handles_the_shapes_config_env_actually_uses(tmp_path):
    f = tmp_path / "config.env"
    f.write_text(
        "# a comment\n"
        "MAX_AGENTS=5\n"
        "export SCALE_PER_ISSUES=8\n"
        'BUSY_HOURS="10-17"\n'
        "AGENT_LABEL_TRUSTED_ACTORS='gitchrisqueen'\n"
        "CLAUDE_CAPACITY_PCT=\n"          # empty is the "no override" idiom
        "NOT AN ASSIGNMENT\n"
        "TRAILING=value # with comment\n"
    )
    env = parse_env_file(f)
    assert env["MAX_AGENTS"] == "5"
    assert env["SCALE_PER_ISSUES"] == "8"
    assert env["BUSY_HOURS"] == "10-17"
    assert env["AGENT_LABEL_TRUSTED_ACTORS"] == "gitchrisqueen"
    assert env["CLAUDE_CAPACITY_PCT"] == ""
    assert env["TRAILING"] == "value"
    assert "NOT" not in env


def test_parse_env_file_missing_is_empty_not_fatal(tmp_path):
    assert parse_env_file(tmp_path / "nope.env") == {}


def test_load_prefers_config_env_then_defaults(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text("MAX_AGENTS=4\nBUSY_HOURS=10-17\nBUSY_TZ=America/New_York\n")
    monkeypatch.delenv("MAX_AGENTS", raising=False)
    cfg = load(tmp_path)
    assert cfg.max_agents == 4
    assert cfg.busy_tz == "America/New_York"
    assert cfg.gh_slots == 2                      # default
    assert cfg.paused_file == tmp_path / "PAUSED"  # v1's switch, shared


def test_shadow_defaults_on(tmp_path):
    """A daemon that is installed but unconfigured must observe, never act."""
    (tmp_path / "config.env").write_text("")
    assert load(tmp_path).shadow is True
    (tmp_path / "config.env").write_text("LEMD_SHADOW=0\n")
    assert load(tmp_path).shadow is False


# ---------------------------------------------------------------- capacity


def test_cap_scales_with_backlog_and_ceilings_at_max():
    assert capacity.compute(ready_count=0, max_agents=5, scale_per_issues=8, gh_slots=2).agents == 1
    assert capacity.compute(ready_count=16, max_agents=5, scale_per_issues=8, gh_slots=2).agents == 3
    assert capacity.compute(ready_count=999, max_agents=5, scale_per_issues=8, gh_slots=2).agents == 5


def test_busy_window_clamps_and_is_zone_aware():
    inside = datetime(2026, 8, 10, 14, 0, tzinfo=ZoneInfo("America/New_York"))  # Monday
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            busy_hours="10-17", busy_tz="America/New_York", busy_days="1-5",
                            busy_max_agents=2, at=inside)
    assert caps.agents == 2 and caps.busy_window is True


def test_busy_window_converts_the_clock_into_the_configured_zone():
    """The same instant must give the same answer whatever zone the caller's clock carries.

    A scheduler naturally holds a UTC clock; reading its `.hour` against a window written in
    `America/New_York` wall-clock hours is wrong by the whole offset, and DST makes it wrong by a
    different amount half the year.
    """
    ny_1pm = datetime(2026, 8, 10, 13, 0, tzinfo=ZoneInfo("America/New_York"))  # inside 10-17
    kw = dict(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
              busy_hours="10-17", busy_tz="America/New_York", busy_days="1-5", busy_max_agents=2)
    assert capacity.compute(**kw, at=ny_1pm).busy_window is True
    assert capacity.compute(**kw, at=ny_1pm.astimezone(timezone.utc)).busy_window is True

    ny_8am = datetime(2026, 8, 10, 8, 0, tzinfo=ZoneInfo("America/New_York"))  # outside
    assert capacity.compute(**kw, at=ny_8am).busy_window is False
    assert capacity.compute(**kw, at=ny_8am.astimezone(timezone.utc)).busy_window is False


def test_busy_window_treats_a_naive_clock_as_already_local():
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            busy_hours="10-17", busy_tz="America/New_York", busy_days="1-5",
                            at=datetime(2026, 8, 10, 13, 0))
    assert caps.busy_window is True


def test_busy_window_ignores_weekends_when_days_are_set():
    sunday = datetime(2026, 8, 9, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            busy_hours="10-17", busy_tz="America/New_York", busy_days="1-5", at=sunday)
    assert caps.busy_window is False and caps.agents == 5


def test_degraded_when_both_lanes_constrained():
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            claude_pct=25, ollama_pct=30)
    assert caps.degraded is True and caps.agents == 1 and caps.reason == "degraded"


def test_one_healthy_lane_is_not_degraded():
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            claude_pct=80, ollama_pct=20)
    assert caps.degraded is False


def test_gh_slots_are_independent_of_agent_slots():
    """Cheap gh mutations must never queue behind long agent runs."""
    caps = capacity.compute(ready_count=0, max_agents=1, scale_per_issues=8, gh_slots=2)
    assert caps.agents == 1 and caps.gh == 2


def test_malformed_busy_window_is_ignored_not_fatal():
    caps = capacity.compute(ready_count=99, max_agents=5, scale_per_issues=8, gh_slots=2,
                            busy_hours="not-a-window")
    assert caps.busy_window is False


def test_heartbeat_is_atomic_and_readable(tmp_path):
    p = tmp_path / "state" / "lemd.heartbeat"
    capacity.heartbeat(p, now=1_700_000_000)
    assert p.read_text() == "1700000000"
    capacity.heartbeat(p, now=1_700_000_060)
    assert p.read_text() == "1700000060"
    assert not list(p.parent.glob("*.new"))  # no temp file left behind
