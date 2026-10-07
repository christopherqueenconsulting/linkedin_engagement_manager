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


class TestCooldownWindow:
    """Showcase round 4: the story cooldown reads the last N posts OR the last D days."""

    def test_the_window_is_the_last_n_or_the_last_d_days_whichever_is_more(self, fake_cursor):
        from datetime import datetime, timedelta
        now = datetime.now()
        rows = [("p1", now), ("p2", now - timedelta(days=1)),
                ("p3", now - timedelta(days=3)), ("p4", now - timedelta(days=30)),
                ("p5", None)]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            assert get_recent_post_texts(7, limit=2, within_days=14) == ["p1", "p2", "p3"]
        assert cur.execute.call_args[0][1] == (7, 200)

    def test_without_days_the_limit_alone_applies(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("a", None)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            assert get_recent_post_texts(7, limit=4) == ["a"]
        assert cur.execute.call_args[0][1] == (7, 4)


class TestRecentPostTopics:
    def test_reads_topic_and_content_newest_first(self, fake_cursor):
        rows = [{"topic": "payroll", "content": "x"}, {"topic": None, "content": "y"}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_topics
            assert get_recent_post_topics(7, limit=3, exclude_post_id=9) == rows
        sql, params = cur.execute.call_args[0]
        assert "SELECT topic, content FROM posts" in sql and "id <> %s" in sql
        assert params == (7, 9, 3)

    def test_without_an_exclusion_and_on_error(self, fake_cursor):
        from cqc_lem.utilities.db import get_recent_post_topics
        conn, cur = fake_cursor(fetch_all=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_recent_post_topics(7) == []
        assert cur.execute.call_args[0][1] == (7, 3)
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_recent_post_topics(7) == []
