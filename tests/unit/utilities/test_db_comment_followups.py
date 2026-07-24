"""Unit tests for comment_followups DB helpers — issue #478."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

DB = "cqc_lem.utilities.db"


def _conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestRecordAndRead:
    def test_record_upserts_and_latches_flags(self):
        from cqc_lem.utilities.db import record_comment_followup
        conn, cur = _conn()
        with patch(f"{DB}.get_db_connection", return_value=conn):
            assert record_comment_followup(1, "feedurn://urn:li:activity:1", "rk", reacted=True) is True
        sql = cur.execute.call_args[0][0]
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "GREATEST(reacted" in sql and "GREATEST(replied" in sql

    def test_get_followup_returns_flags(self):
        from cqc_lem.utilities.db import get_comment_followup
        conn, cur = _conn()
        cur.fetchone.return_value = {"reacted": 1, "replied": 0}
        with patch(f"{DB}.get_db_connection", return_value=conn):
            assert get_comment_followup(1, "rk") == {"reacted": 1, "replied": 0}

    def test_get_followup_none_for_empty_key(self):
        from cqc_lem.utilities.db import get_comment_followup
        assert get_comment_followup(1, "") is None


class TestNavigableAndCap:
    def test_recent_navigable_filters_to_feedurn(self):
        from cqc_lem.utilities.db import get_recent_navigable_commented_posts
        conn, cur = _conn()
        cur.fetchall.return_value = [{"post_key": "feedurn://urn:li:activity:1"}]
        with patch(f"{DB}.get_db_connection", return_value=conn):
            rows = get_recent_navigable_commented_posts(1, days=3)
        assert rows and rows[0]["post_key"].startswith("feedurn://")
        assert "feedurn://%" in cur.execute.call_args[0][0]

    def test_count_followup_replies_today(self):
        from cqc_lem.utilities.db import count_followup_replies_today
        conn, cur = _conn()
        cur.fetchone.return_value = [4]
        with patch(f"{DB}.get_db_connection", return_value=conn):
            assert count_followup_replies_today(1) == 4
        assert "replied=1" in cur.execute.call_args[0][0]


class TestUpdateKey:
    def test_upgrade_when_new_key_absent(self):
        from cqc_lem.utilities.db import update_commented_post_key
        conn, cur = _conn()
        cur.fetchone.return_value = [0]   # new key not present
        cur.rowcount = 1
        with patch(f"{DB}.get_db_connection", return_value=conn):
            assert update_commented_post_key(1, "feedpost://h", "feedurn://urn:li:activity:1") is True
        assert any("UPDATE commented_posts SET post_key" in c.args[0] for c in cur.execute.call_args_list)

    def test_deletes_stale_when_new_key_already_exists(self):
        from cqc_lem.utilities.db import update_commented_post_key
        conn, cur = _conn()
        cur.fetchone.return_value = [1]   # new key already there
        cur.rowcount = 1
        with patch(f"{DB}.get_db_connection", return_value=conn):
            assert update_commented_post_key(1, "feedpost://h", "feedurn://urn:li:activity:1") is True
        assert any("DELETE FROM commented_posts" in c.args[0] for c in cur.execute.call_args_list)

    def test_noop_for_identical_or_empty(self):
        from cqc_lem.utilities.db import update_commented_post_key
        assert update_commented_post_key(1, "x", "x") is False
        assert update_commented_post_key(1, "", "y") is False
