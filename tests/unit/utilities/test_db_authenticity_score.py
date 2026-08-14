"""Unit tests for the V57 authenticity-score DB helpers (issue #382)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestUpdateDbPostAuthenticityScore:
    def test_updates_score_column(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, 82) is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE posts SET authenticity_score" in sql
        assert params == (82, 7)

    def test_accepts_null_score(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, None) is True
        _, params = cur.execute.call_args[0]
        assert params == (None, 7)

    def test_false_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_authenticity_score
            assert update_db_post_authenticity_score(7, 50) is False


class TestGetPostAuthenticityScore:
    def test_returns_int_score(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(73,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) == 73
        sql, params = cur.execute.call_args[0]
        assert "SELECT authenticity_score FROM posts" in sql
        assert params == (7,)

    def test_none_when_unscored(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) is None

    def test_none_when_no_row(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(999) is None

    def test_none_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_authenticity_score
            assert get_post_authenticity_score(7) is None
