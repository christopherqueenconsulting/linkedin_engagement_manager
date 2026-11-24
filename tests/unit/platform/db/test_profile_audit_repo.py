"""Profile audit repository SQL behaviour (issue #2631, Phase 1)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import mysql.connector

from cqc_lem.platform.db.repositories import profile_audit as repo

FINDING = {"check_id": "custom_url", "status": "fail", "weight": 10, "detail": "d", "fix_hint": "h"}


def _cursor_ctx(cursor):
    @contextmanager
    def ctx(**_kw):
        yield cursor
    return ctx


def test_insert_rejects_bad_kind():
    assert repo.insert_profile_audit(1, "daily", "k", "1", 50, [FINDING]) is None


def test_insert_writes_audit_and_findings():
    cur = MagicMock(rowcount=1, lastrowid=5)
    with patch.object(repo, "db_cursor", _cursor_ctx(cur)):
        assert repo.insert_profile_audit(1, "weekly", "2026-W48", "1", 50, [FINDING]) == 5
    assert cur.executemany.call_args[0][1][0][0] == 5


def test_insert_duplicate_period_is_none():
    cur = MagicMock(rowcount=0)
    with patch.object(repo, "db_cursor", _cursor_ctx(cur)):
        assert repo.insert_profile_audit(1, "weekly", "2026-W48", "1", 50, [FINDING]) is None
    cur.executemany.assert_not_called()


def test_faults_fail_soft():
    @contextmanager
    def boom(**_kw):
        raise mysql.connector.Error("down")
        yield
    with patch.object(repo, "db_cursor", boom):
        assert repo.insert_profile_audit(1, "weekly", "k", "1", 1, []) is None
        assert repo.get_profile_audit_findings(1) == []
        assert repo.get_latest_profile_audit(1) is None
        assert repo.profile_audit_exists(1, "weekly", "k") is True


def test_reads():
    cur = MagicMock()
    cur.fetchall.return_value = [FINDING]
    cur.fetchone.return_value = {"id": 3}
    with patch.object(repo, "db_cursor", _cursor_ctx(cur)):
        assert repo.get_profile_audit_findings(3) == [FINDING]
        assert repo.get_latest_profile_audit(1) == {"id": 3}
        assert repo.profile_audit_exists(1, "weekly", "k") is True
