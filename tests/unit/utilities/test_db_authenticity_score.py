"""Unit tests for the V57 authenticity-score DB helpers (issue #382)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_one=None, rowcount=1):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


class TestUpdateDbPostAuthenticityScore:
    def test_updates_score_column(self):
        conn, cur = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, 82) is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE posts SET authenticity_score" in sql
        assert params == (82, 7)

    def test_accepts_null_score(self):
        conn, cur = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, None) is True
        _, params = cur.execute.call_args[0]
        assert params == (None, 7)

    def test_false_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, 50) is False


class TestGetPostAuthenticityScore:
    def test_returns_int_score(self):
        conn, cur = _mock_conn(fetch_one=(73,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) == 73
        sql, params = cur.execute.call_args[0]
        assert "SELECT authenticity_score FROM posts" in sql
        assert params == (7,)

    def test_none_when_unscored(self):
        conn, cur = _mock_conn(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) is None

    def test_none_when_no_row(self):
        conn, cur = _mock_conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(999) is None

    def test_none_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) is None
