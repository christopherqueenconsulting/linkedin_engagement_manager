"""Unit tests for LinkedIn-session DB helpers."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _conn(fetch_row):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_row
    conn.cursor.return_value = cur
    return conn, cur


class TestHasLinkedInSession:
    def test_true_when_li_at_present(self):
        conn, cur = _conn((1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_linkedin_session
            assert has_linkedin_session(7) is True
        assert "li_at" in cur.execute.call_args[0][0]

    def test_false_when_absent(self):
        conn, _ = _conn(None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_linkedin_session
            assert has_linkedin_session(7) is False


class TestSessionEmailTimestamp:
    def test_get_returns_value(self):
        import datetime as dt
        when = dt.datetime(2026, 6, 30, 9, 0, 0)
        conn, _ = _conn((when,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linkedin_session_email_sent_at
            assert get_linkedin_session_email_sent_at(7) == when

    def test_get_returns_none_when_no_row(self):
        conn, _ = _conn(None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linkedin_session_email_sent_at
            assert get_linkedin_session_email_sent_at(7) is None

    def test_set_commits(self):
        conn, cur = _conn(None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_linkedin_session_email_sent_at
            assert set_linkedin_session_email_sent_at(7) is True
        conn.commit.assert_called_once()
        assert cur.execute.call_args[0][1] == (7,)
