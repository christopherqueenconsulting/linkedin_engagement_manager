"""Tests for `scripts/agent-pipeline/v2/lemd/queue_read.py`.

The read-only helper `scripts/triage_issues.py` uses to read the daemon's own in-flight count
(Part A of the hourly-triage plan).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, queue_read  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """A fresh queue database, closed after the test."""
    c = db.connect(tmp_path / "queue.db")
    yield c
    c.close()


class TestReadInflightCount:
    def test_missing_database_returns_none(self, tmp_path):
        assert queue_read.read_inflight_count(tmp_path / "does" / "not" / "exist.db") is None

    def test_matches_the_daemons_own_wip_count(self, conn, tmp_path):
        db.upsert_item(conn, kind="pr", number=1, state=db.STATE_RUNNING)
        db.upsert_item(conn, kind="pr", number=2, state=db.STATE_WAIT_CI)
        db.upsert_item(conn, kind="pr", number=3, state=db.STATE_MERGED)  # terminal — not WIP
        db.upsert_item(conn, kind="issue", number=4, state=db.STATE_READY)  # an issue, not a PR

        db_path = _db_path_from_conn(conn)
        assert queue_read.read_inflight_count(db_path) == db.wip_count(conn) == 2

    def test_reads_read_only_and_never_writes(self, conn, tmp_path):
        db_path = _db_path_from_conn(conn)
        db.upsert_item(conn, kind="pr", number=1, state=db.STATE_CLAIMED)
        before = db_path.stat().st_mtime_ns
        queue_read.read_inflight_count(db_path)
        after = db_path.stat().st_mtime_ns
        assert before == after

    def test_a_corrupt_file_fails_closed_to_none(self, tmp_path):
        bad = tmp_path / "queue.db"
        bad.write_text("not a sqlite file", encoding="utf-8")
        assert queue_read.read_inflight_count(bad) is None


def _db_path_from_conn(conn) -> Path:
    """The on-disk path SQLite opened this connection against."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row["file"])
