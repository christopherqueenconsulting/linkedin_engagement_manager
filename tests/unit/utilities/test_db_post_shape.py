"""Unit tests for the V51 post shape history DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestUpdateDbPostShape:
    def test_updates_shape_columns(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_shape
            assert update_db_post_shape(7, "tactical_list", "bold_claim") is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE posts SET archetype" in sql and "hook_style" in sql and "topic" in sql
        assert params == ("tactical_list", "bold_claim", None, 7)

    def test_persists_topic(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_shape
            assert update_db_post_shape(7, "tactical_list", "bold_claim", topic="AI hiring") is True
        _, params = cur.execute.call_args[0]
        assert params == ("tactical_list", "bold_claim", "AI hiring", 7)

    def test_false_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_shape
            assert update_db_post_shape(7, "tactical_list", "bold_claim") is False


class TestGetRecentPostShapeHistory:
    def test_returns_recent_shapes(self, fake_cursor):
        rows = [{"archetype": "tactical_list", "hook_style": "bold_claim"},
                {"archetype": "personal_lesson", "hook_style": "question"}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_shape_history
            out = get_recent_post_shape_history(1, limit=5)
        assert [h["archetype"] for h in out] == ["tactical_list", "personal_lesson"]
        sql, params = cur.execute.call_args[0]
        assert "archetype IS NOT NULL" in sql and "ORDER BY id DESC" in sql
        assert params == (1, 5)

    def test_empty_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_shape_history
            assert get_recent_post_shape_history(1) == []
