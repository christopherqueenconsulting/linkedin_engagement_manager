"""Tests for two v2 daemon defects that both made the pipeline lie about its own health.

* `rc=-9` was written by three different endings — a deadline kill, an adopted orphan, and crash
  recovery — so "we stopped it" and "it was already gone" were indistinguishable (#1359).
* The webhook-staleness detector treated silence as a fault, so a quiet repository polled 5x
  harder than a busy one (#1352).

Neither broke the pipeline. Both made the numbers an operator reads to decide whether to lift the
start-lane throttle (#1311 §1) wrong, which is worse.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db  # noqa: E402


def _open_run(conn, *, pid: int = 999999) -> int:
    """An open run row whose pid is not alive."""
    db.upsert_item(conn, kind="pr", number=1, state=db.STATE_RUNNING, branch="feature/x")
    item = db.get_item(conn, "pr", 1)
    return db.start_run(conn, item_id=item["id"], mode="start", pid=pid)


# ---------------------------------------------------------------- #1359


def test_the_two_endings_have_different_codes():
    """The whole point: a reader must be able to tell them apart at all."""
    assert db.RC_KILLED != db.RC_VANISHED


def test_a_deadline_kill_keeps_the_documented_code():
    """`RC_KILLED` stays -9 — status tooling and existing rows already read that value."""
    assert db.RC_KILLED == -9


def test_crash_recovery_does_not_report_a_kill(tmp_path):
    """`startup_recover` closes runs nobody killed.

    This was the third writer of -9, and it fires on EVERY restart — 16 of them on 2026-08-10
    while `config.env` was being edited, which is where the phantom timeouts came from.
    """
    conn = db.connect(tmp_path / "q.db")
    run_id = _open_run(conn)
    db.startup_recover(conn)
    rc = conn.execute("SELECT rc FROM runs WHERE id=?", (run_id,)).fetchone()["rc"]
    assert rc == db.RC_VANISHED
    assert rc != db.RC_KILLED


def test_recovery_still_closes_the_run_and_frees_the_item(tmp_path):
    """Changing the code must not change the repair — the slot still has to come back."""
    conn = db.connect(tmp_path / "q.db")
    _open_run(conn)
    stats = db.startup_recover(conn)
    assert stats["runs_closed"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM runs WHERE ended_at IS NULL").fetchone()["n"] == 0


def test_an_adopted_orphan_is_not_counted_as_a_timeout():
    """`_reap_adopted` is the second writer; asserted at source since it needs a live supervisor."""
    src = (_V2 / "lemd" / "dispatch.py").read_text()
    assert "db.finish_run(self.conn, row[\"id\"], db.RC_VANISHED" in src
    assert "db.finish_run(self.conn, row[\"id\"], -9" not in src


# ---------------------------------------------------------------- #1352


class _Cfg:
    """Just the knobs `reconcile_interval` reads."""

    reconcile_interval = 600
    reconcile_interval_degraded = 120
    webhook_stale_seconds = 1800


class _FakeDaemon:
    """`reconcile_interval` lifted off a real daemon — it touches only conn, cfg and two flags."""

    from lemd.daemon import Daemon  # noqa: N815

    reconcile_interval = Daemon.reconcile_interval

    def __init__(self, conn, *, drift: int) -> None:
        self.conn = conn
        self.cfg = _Cfg()
        self._degraded = False
        self._reconcile_drift = drift


def _stale(conn) -> None:
    """Record a webhook delivery far enough in the past to trip the staleness bound."""
    db.kv_set(conn, "last_webhook_at", str(int(time.time()) - 5000))


def test_a_quiet_repository_keeps_the_normal_cadence(tmp_path):
    """The defect. Silence with nothing undelivered behind it is quiet, not broken.

    The 2026-08-10 15:50–17:17 window produced a 78-minute warning with ZERO workflow runs created
    in it, on a receiver that was healthy throughout and resumed on its own.
    """
    conn = db.connect(tmp_path / "q.db")
    _stale(conn)
    assert _FakeDaemon(conn, drift=0).reconcile_interval() == 600


def test_silence_plus_an_undelivered_change_does_degrade(tmp_path):
    """...and the real fault still trips it: something changed and nobody told us."""
    conn = db.connect(tmp_path / "q.db")
    _stale(conn)
    assert _FakeDaemon(conn, drift=2).reconcile_interval() == 120


def test_a_fresh_delivery_is_never_degraded(tmp_path):
    """Drift with a healthy event path is just work arriving — reconcile found it first."""
    conn = db.connect(tmp_path / "q.db")
    db.kv_set(conn, "last_webhook_at", str(int(time.time())))
    assert _FakeDaemon(conn, drift=5).reconcile_interval() == 600


def test_no_delivery_ever_recorded_is_not_a_fault(tmp_path):
    """A daemon that has never seen a webhook (fresh install) must not start degraded."""
    conn = db.connect(tmp_path / "q.db")
    assert _FakeDaemon(conn, drift=3).reconcile_interval() == 600


def test_an_unparseable_timestamp_falls_back_to_normal(tmp_path):
    """Garbage in kv is not evidence of a broken event path."""
    conn = db.connect(tmp_path / "q.db")
    db.kv_set(conn, "last_webhook_at", "not-a-number")
    assert _FakeDaemon(conn, drift=9).reconcile_interval() == 600


def test_reconcile_counts_drift_from_both_head_and_labels():
    """Labels matter as much as the head: a label edit is the event the answer lane rides on."""
    src = (_V2 / "lemd" / "daemon.py").read_text()
    assert 'existing["head_sha"] != obj.get("headRefOid")' in src
    assert 'existing["labels_json"] != labels_json' in src
