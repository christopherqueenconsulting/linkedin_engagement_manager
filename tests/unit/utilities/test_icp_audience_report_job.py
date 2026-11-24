"""Monthly ICP-audience report beat: idempotence, unreadable never stored, per-user isolation (#2758)."""

import datetime as dt
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import icp_audience_reports as repo
from cqc_lem.utilities import icp_audience_report_job as job

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, 6, 30, tzinfo=dt.timezone.utc)
_DB = "cqc_lem.utilities.db."
_REPORT = "cqc_lem.utilities.icp_audience_report.load_icp_audience_report"


def test_month_key():
    assert job.month_key(NOW) == "2026-12"


def test_stores_readable_report():
    with patch(_DB + "icp_audience_report_exists", return_value=False), \
         patch(_REPORT, return_value={"readable": True}), \
         patch(_DB + "insert_icp_audience_report", return_value=True) as ins:
        assert job.store_user_report(4, NOW) == "stored"
    assert ins.call_args.args == (4, "2026-12", {"readable": True})


def test_existing_month_is_not_rebuilt():
    with patch(_DB + "icp_audience_report_exists", return_value=True), \
         patch(_REPORT) as load:
        assert job.store_user_report(4, NOW) == "exists"
    load.assert_not_called()


def test_unreadable_report_stores_nothing():
    with patch(_DB + "icp_audience_report_exists", return_value=False), \
         patch(_REPORT, return_value={"readable": False}), \
         patch(_DB + "insert_icp_audience_report") as ins:
        assert job.store_user_report(4, NOW) == "unreadable"
    ins.assert_not_called()


def test_failed_insert_reads_failed():
    with patch(_DB + "icp_audience_report_exists", return_value=False), \
         patch(_REPORT, return_value={"readable": True}), \
         patch(_DB + "insert_icp_audience_report", return_value=False):
        assert job.store_user_report(4, NOW) == "failed"


def test_beat_isolates_users_and_honours_flag():
    def fake_store(uid, _now):
        if uid == 2:
            raise RuntimeError("boom")
        return "stored"

    with patch(_DB + "get_active_user_ids", return_value=[1, 2, 3, 4]), \
         patch("cqc_lem.utilities.flags.flag_enabled", side_effect=lambda _f, uid: uid != 4), \
         patch.object(job, "store_user_report", side_effect=fake_store):
        counts = job.run_monthly_icp_audience_reports(NOW)
    assert counts == {"stored": 2, "exists": 0, "unreadable": 0, "failed": 1, "skipped": 1}


def test_task_wrapper_reports_counts():
    from cqc_lem.app.run_scheduler import auto_icp_audience_report_monthly
    with patch.object(job, "run_monthly_icp_audience_reports", return_value={"stored": 1}):
        assert "stored" in auto_icp_audience_report_monthly()


def _ctx(cursor):
    @contextmanager
    def ctx(**_kw):
        yield cursor
    return ctx


def test_repo_insert_and_exists():
    cur = MagicMock(rowcount=1)
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.insert_icp_audience_report(1, "2026-12", {"a": 1}) is True
    cur.rowcount = 0
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.insert_icp_audience_report(1, "2026-12", {}) is False
    cur.fetchone.return_value = (1,)
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.icp_audience_report_exists(1, "2026-12") is True


def test_repo_latest_and_faults():
    cur = MagicMock()
    cur.fetchone.return_value = {"period_key": "2026-12", "created_at": NOW,
                                 "report_json": json.dumps({"readable": True})}
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.get_latest_icp_audience_report(1)["report"] == {"readable": True}
    cur.fetchone.return_value = None
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.get_latest_icp_audience_report(1) is None

    def boom(**_kw):
        raise mysql.connector.Error("x")
    with patch.object(repo, "db_cursor", boom):
        assert repo.insert_icp_audience_report(1, "k", {}) is False
        assert repo.icp_audience_report_exists(1, "k") is True
        assert repo.get_latest_icp_audience_report(1) is None
