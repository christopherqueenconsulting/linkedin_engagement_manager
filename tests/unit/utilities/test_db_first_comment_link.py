"""Unit tests for the link-in-first-comment DB layer (issue #392 — C3)."""

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


class TestUpdateDbPostFirstCommentLink:
    def test_stores_link(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_first_comment_link
            assert update_db_post_first_comment_link(7, "https://example.com/a") is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE posts SET first_comment_link" in sql
        assert params == ("https://example.com/a", 7)

    def test_clears_with_none(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_first_comment_link
            assert update_db_post_first_comment_link(7, None) is True
        assert cur.execute.call_args[0][1] == (None, 7)

    def test_false_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_first_comment_link
            assert update_db_post_first_comment_link(7, "https://example.com/a") is False


class TestGetPostFirstCommentLink:
    def test_returns_stored_links(self):
        conn, cur = _mock_conn(fetch_one=("https://example.com/a\nhttps://example.com/b",))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_first_comment_link
            assert get_post_first_comment_link(7) == "https://example.com/a\nhttps://example.com/b"
        sql, params = cur.execute.call_args[0]
        assert "SELECT first_comment_link FROM posts" in sql
        assert params == (7,)

    def test_none_when_empty_or_missing(self):
        for row in [(None,), ("",), None]:
            conn, _ = _mock_conn(fetch_one=row)
            with patch(f"{_DB}.get_db_connection", return_value=conn):
                from cqc_lem.utilities.db import get_post_first_comment_link
                assert get_post_first_comment_link(7) is None

    def test_none_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_first_comment_link
            assert get_post_first_comment_link(7) is None


class TestLinkInFirstCommentPref:
    def test_default_true(self):
        conn, _ = _mock_conn()
        conn.cursor.return_value.fetchone.return_value = None
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["link_in_first_comment"] is True

    def test_decodes_as_bool(self):
        conn, _ = _mock_conn(fetch_one={"link_in_first_comment": 0})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["link_in_first_comment"] is False

    def test_persists_as_int(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"link_in_first_comment": False})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        by_col = dict(zip(cols, cur.execute.call_args[0][1][1:]))
        assert by_col["link_in_first_comment"] == 0
