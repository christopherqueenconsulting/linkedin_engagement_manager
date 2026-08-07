"""Unit tests for premium video credit + post-quality DB functions."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _db(fetchone=None, rowcount=1):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestVideoCreditBalance:
    def test_sum(self):
        conn, _ = _db(fetchone={"balance": 7})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_video_credit_balance
            assert get_video_credit_balance(1) == 7

    def test_zero_on_error(self):
        conn, cur = _db()
        cur.execute.side_effect = __import__("mysql.connector", fromlist=["c"]).Error("x")
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_video_credit_balance
            assert get_video_credit_balance(1) == 0


class TestVideoCreditMutations:
    def test_add_inserts_positive(self):
        conn, cur = _db()
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_video_credits
            assert add_video_credits(1, 15, "purchase_medium", "sess_1") is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO video_credit_ledger" in sql and params[1] == 15

    def test_deduct_inserts_negative(self):
        conn, cur = _db()
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import deduct_video_credits
            assert deduct_video_credits(1, 3, post_id=9, reason="premium_video_veo3.1") is True
        params = cur.execute.call_args[0][1]
        assert params[1] == -3 and params[3] == 9

    def test_refund_inserts_positive(self):
        conn, cur = _db()
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import refund_video_credits
            assert refund_video_credits(1, 3, post_id=9) is True
        assert cur.execute.call_args[0][1][1] == 3

    def test_ledger_by_session(self):
        conn, _ = _db(fetchone={"id": 1, "user_id": 1, "delta": 15})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_video_credit_ledger_entry_by_session
            assert get_video_credit_ledger_entry_by_session("sess_1")["delta"] == 15


class TestPostVideoQuality:
    def test_get_defaults_standard(self):
        conn, _ = _db(fetchone=None)
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_video_quality
            assert get_post_video_quality(5) == "standard"

    def test_get_returns_value(self):
        conn, _ = _db(fetchone={"video_quality": "premium"})
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_video_quality
            assert get_post_video_quality(5) == "premium"

    def test_update(self):
        conn, cur = _db(rowcount=1)
        with patch("cqc_lem.utilities.db.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_post_video_quality
            assert update_post_video_quality(5, "premium_top") is True
        assert "UPDATE posts SET video_quality" in cur.execute.call_args[0][0]
