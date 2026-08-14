"""Unit tests for the recent-post-content DB helper feeding the post dedup steering + review gate."""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestGetRecentPostTexts:
    def test_returns_contents_most_recent_first(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("post three",), ("post two",), ("post one",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            out = get_recent_post_texts(7)
        assert out == ["post three", "post two", "post one"]
        sql = cur.execute.call_args[0][0]
        assert "ORDER BY id DESC" in sql

    def test_filters_to_pending_approved_posted_only(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            get_recent_post_texts(7)
        sql = cur.execute.call_args[0][0]
        assert "status IN ('pending', 'approved', 'posted')" in sql
        # planning/rejected/error drafts must never pollute the dedup history
        assert "planning" not in sql and "rejected" not in sql and "error" not in sql

    def test_default_limit_and_override(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            get_recent_post_texts(7)
            assert cur.execute.call_args[0][1] == (7, 20)
            get_recent_post_texts(7, limit=5)
            assert cur.execute.call_args[0][1] == (7, 5)

    def test_excludes_empty_content_in_sql(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            get_recent_post_texts(7)
        sql = cur.execute.call_args[0][0]
        assert "content IS NOT NULL" in sql and "content <> ''" in sql

    def test_db_error_returns_empty_list(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            assert get_recent_post_texts(7) == []

    def test_closes_cursor_and_connection(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("a",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            get_recent_post_texts(7)
        cur.close.assert_called_once()
        conn.close.assert_called_once()
