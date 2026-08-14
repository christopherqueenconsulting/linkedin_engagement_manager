"""Unit tests for comment_followups DB helpers — issue #478."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestRecordAndRead:
    def test_record_upserts_and_latches_flags(self, fake_cursor):
        from cqc_lem.utilities.db import record_comment_followup
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert record_comment_followup(1, "feedurn://urn:li:activity:1", "rk", reacted=True) is True
        sql = cur.execute.call_args[0][0]
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "GREATEST(reacted" in sql and "GREATEST(replied" in sql

    def test_get_followup_returns_flags(self, fake_cursor):
        from cqc_lem.utilities.db import get_comment_followup
        conn, cur = fake_cursor(fetch_one={"reacted": 1, "replied": 0})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_comment_followup(1, "rk") == {"reacted": 1, "replied": 0}

    def test_get_followup_none_for_empty_key(self):
        from cqc_lem.utilities.db import get_comment_followup
        assert get_comment_followup(1, "") is None


class TestNavigableAndCap:
    def test_recent_navigable_filters_to_feedurn(self, fake_cursor):
        from cqc_lem.utilities.db import get_recent_navigable_commented_posts
        conn, cur = fake_cursor(fetch_all=[{"post_key": "feedurn://urn:li:activity:1"}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            rows = get_recent_navigable_commented_posts(1, days=3)
        assert rows and rows[0]["post_key"].startswith("feedurn://")
        assert "feedurn://%" in cur.execute.call_args[0][0]

    def test_count_followup_replies_today(self, fake_cursor):
        from cqc_lem.utilities.db import count_followup_replies_today
        conn, cur = fake_cursor(fetch_one=[4])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert count_followup_replies_today(1) == 4
        assert "replied=1" in cur.execute.call_args[0][0]


class TestUpdateKey:
    def test_upgrade_when_new_key_absent(self, fake_cursor):
        from cqc_lem.utilities.db import update_commented_post_key
        conn, cur = fake_cursor(fetch_one=[0])
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert update_commented_post_key(1, "feedpost://h", "feedurn://urn:li:activity:1") is True
        assert any("UPDATE commented_posts SET post_key" in c.args[0] for c in cur.execute.call_args_list)

    def test_deletes_stale_when_new_key_already_exists(self, fake_cursor):
        from cqc_lem.utilities.db import update_commented_post_key
        conn, cur = fake_cursor(fetch_one=[1])
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert update_commented_post_key(1, "feedpost://h", "feedurn://urn:li:activity:1") is True
        assert any("DELETE FROM commented_posts" in c.args[0] for c in cur.execute.call_args_list)

    def test_noop_for_identical_or_empty(self):
        from cqc_lem.utilities.db import update_commented_post_key
        assert update_commented_post_key(1, "x", "x") is False
        assert update_commented_post_key(1, "", "y") is False


import mysql.connector


class TestRowsWithTextAndErrorBranches:
    def test_recent_rows_with_text_happy(self, fake_cursor):
        from cqc_lem.utilities.db import get_recent_commented_rows_with_text
        conn, cur = fake_cursor(fetch_all=[{"post_key": "feedpost://h", "comment_text": "hi"}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            rows = get_recent_commented_rows_with_text(1, days=3)
        assert rows and rows[0]["comment_text"] == "hi"
        assert "flyway" not in cur.execute.call_args[0][0].lower()

    @pytest.mark.parametrize("fn,args,default", [
        ("get_recent_navigable_commented_posts", (1,), []),
        ("get_recent_commented_rows_with_text", (1,), []),
        ("get_comment_followup", (1, "rk"), None),
        ("record_comment_followup", (1, "pk", "rk"), False),
        ("count_followup_replies_today", (1,), 0),
        ("update_commented_post_key", (1, "a", "b"), False),
    ])
    def test_db_error_returns_safe_default(self, fn, args, default, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fn)(*args) == default


class TestGuards:
    def test_record_followup_empty_key(self):
        from cqc_lem.utilities.db import record_comment_followup
        assert record_comment_followup(1, "pk", "") is False

    def test_get_followup_empty_key(self):
        from cqc_lem.utilities.db import get_comment_followup
        assert get_comment_followup(1, "") is None
