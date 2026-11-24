"""Profile audit read side and finding actions SQL behaviour (issue #2753)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import profile_audit as repo

pytestmark = pytest.mark.unit


def _ctx(cursor):
    @contextmanager
    def ctx(**_kw):
        yield cursor
    return ctx


@contextmanager
def _boom(**_kw):
    raise mysql.connector.Error("down")
    yield  # pragma: no cover


def test_reads_return_rows():
    cur = MagicMock()
    cur.fetchall.return_value = [{"id": 1}]
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.list_profile_audits(1) == [{"id": 1}]
        assert repo.read_profile_audit_findings(1) == [{"id": 1}]


def test_open_counts_exclude_ignored_checks():
    cur = MagicMock()
    cur.fetchall.return_value = [(5, 2)]
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.get_profile_audit_open_counts([5, 6], ["custom_url"]) == {5: 2}
    sql, params = cur.execute.call_args[0]
    assert "NOT IN" in sql and params == [5, 6, "custom_url"]
    assert repo.get_profile_audit_open_counts([]) == {}


def test_actions_map_by_check():
    cur = MagicMock()
    cur.fetchall.return_value = [{"check_id": "custom_url", "action": "ignored", "audit_id": 3}]
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.get_profile_finding_actions(1) == {
            "custom_url": {"action": "ignored", "audit_id": 3}}


def test_set_action_upserts_and_clears():
    cur = MagicMock()
    with patch.object(repo, "db_cursor", _ctx(cur)):
        assert repo.set_profile_finding_action(1, "custom_url", "done", 3) is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]
        assert repo.set_profile_finding_action(1, "custom_url", None, 3) is True
        assert cur.execute.call_args[0][0].startswith("DELETE")
    assert repo.set_profile_finding_action(1, "custom_url", "bogus", 3) is False


def test_faults_are_none_or_false_never_empty():
    with patch.object(repo, "db_cursor", _boom):
        assert repo.list_profile_audits(1) is None
        assert repo.read_profile_audit_findings(1) is None
        assert repo.get_profile_audit_open_counts([1]) is None
        assert repo.get_profile_finding_actions(1) is None
        assert repo.set_profile_finding_action(1, "custom_url", "done", 3) is False
