"""Coverage tests for avatar-training, group, post-stats and scheduled-DM DB helpers (db.py)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(fetch_one=None, fetch_all=None, rowcount=1, lastrowid=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestAvatarLedger:
    def test_ledger_entry_by_session(self):
        row = {"id": 1, "user_id": 3, "delta": 5}
        conn, cur = _conn(fetch_one=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_credit_ledger_entry_by_session
            assert get_avatar_credit_ledger_entry_by_session("cs_1") == row
        sql, params = cur.execute.call_args[0]
        assert "delta > 0" in sql and params == ("cs_1",)

    def test_ledger_entry_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_credit_ledger_entry_by_session
            assert get_avatar_credit_ledger_entry_by_session("cs_1") is None

    def test_refund_avatar_credit(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import refund_avatar_credit
            assert refund_avatar_credit(3, "train_1") is True
        sql, params = cur.execute.call_args[0]
        assert "'training_refund'" in sql and params == (3, "train_1")
        conn.commit.assert_called_once()

    def test_refund_avatar_credit_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import refund_avatar_credit
            assert refund_avatar_credit(3, "train_1") is False


class TestAvatarTrainings:
    def test_set_active_avatar_validates_then_deactivates_then_activates(self):
        conn, cur = _conn(rowcount=1, fetch_one={"id": 11, "approval_status": "approved"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 11) is True
        select_sql, select_params = cur.execute.call_args_list[0][0]
        first_sql, first_params = cur.execute.call_args_list[1][0]
        second_sql, second_params = cur.execute.call_args_list[2][0]
        assert "SELECT id" in select_sql and select_params == (11, 3)
        assert "is_active = 0" in first_sql and first_params == (3,)
        assert "is_active = 1" in second_sql and second_params == (11, 3)

    def test_set_active_avatar_invalid_id_preserves_current_active(self):
        # Unknown avatar id → False, and NO deactivating UPDATE ever runs.
        conn, cur = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 999) is False
        executed = [c[0][0] for c in cur.execute.call_args_list]
        assert len(executed) == 1 and "SELECT id" in executed[0]
        conn.commit.assert_not_called()

    def test_set_active_avatar_refuses_unapproved(self):
        """The approval gate (issue #744): activation is no longer reachable from 'succeeded'
        alone, and refusing must leave the current active avatar untouched.
        """
        conn, cur = _conn(rowcount=1, fetch_one={"id": 11, "approval_status": "pending"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 11) is False
        executed = [c[0][0] for c in cur.execute.call_args_list]
        assert len(executed) == 1 and "SELECT id" in executed[0]
        conn.commit.assert_not_called()

    def test_set_active_avatar_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 11) is False

    def test_get_avatar_trainings_serializes_rows(self):
        created = datetime(2026, 7, 1, 9, 0)
        rows = [{"id": 1, "training_id": "t1", "model_ref": "m", "trigger_word": "TOK",
                 "status": "succeeded", "is_active": 1, "created_at": created,
                 "updated_at": None}]
        conn, _ = _conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_trainings
            result = get_avatar_trainings(3)
        assert result == [{"id": 1, "training_id": "t1", "model_ref": "m",
                           "trigger_word": "TOK", "status": "succeeded", "is_active": True,
                           "gender_presentation": None, "age_band": None,
                           "attributes_confirmed_at": None,
                           # A row that predates the migration reads as un-approved, which is the
                           # safe direction: it cannot be activated until the user reviews it.
                           "approval_status": "pending", "approved_at": None,
                           "sample_paths": [], "samples_generated_at": None,
                           "sample_regen_count": 0,
                           "created_at": created.isoformat(), "updated_at": None}]

    def test_get_avatar_trainings_error_returns_empty(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_avatar_trainings
            assert get_avatar_trainings(3) == []

    def test_update_training_status_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_avatar_training_status
            assert update_avatar_training_status("t1", "failed") is False


class TestUserGroups:
    def test_get_user_groups_coerces_flags_to_bool_and_date_to_iso(self):
        rows = [{"group_id": "g1", "group_name": "AI Leaders", "enabled": 1, "post_enabled": 0,
                 "last_posted_at": datetime(2026, 7, 28, 15, 0)},
                {"group_id": "g2", "group_name": "Sales", "enabled": 0, "post_enabled": 1,
                 "last_posted_at": None}]
        conn, _ = _conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_groups
            result = get_user_groups(1)
        # Commenting and posting are independent flags (issue #769) — neither implies the other.
        assert result[0]["enabled"] is True and result[0]["post_enabled"] is False
        assert result[1]["enabled"] is False and result[1]["post_enabled"] is True
        # JSON-serializable for the SPA payload.
        assert result[0]["last_posted_at"] == "2026-07-28T15:00:00"
        assert result[1]["last_posted_at"] is None

    def test_get_user_groups_error_returns_empty(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_groups
            assert get_user_groups(1) == []


class TestPostStats:
    def test_record_post_stats_coerces_none_counts(self):
        # No matching post row → attribution snapshot is all-NULL but the stat is still recorded.
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_post_stats
            assert record_post_stats(1, 9, None, None, reposts=None, impressions=120, saves=None) is True
        params = cur.execute.call_args[0][1]  # last execute = the INSERT
        assert params == (1, 9, 0, 0, 0, 120, 0, None, None, None, None, None)

    def test_record_post_stats_snapshots_post_attribution(self):
        # The post's shape/topic is snapshotted onto the stat row at capture time (#386).
        conn, cur = _conn(fetch_one=("tactical_list", "bold_claim", "text", "AI hiring", "awareness"))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_post_stats
            assert record_post_stats(1, 9, 10, 3, reposts=1, impressions=200, saves=6) is True
        select_sql, select_params = cur.execute.call_args_list[0][0]
        assert "FROM posts WHERE id=%s AND user_id=%s" in select_sql and select_params == (9, 1)
        insert_sql, insert_params = cur.execute.call_args_list[1][0]
        assert "INSERT INTO post_stats" in insert_sql and "`format`" in insert_sql
        assert insert_params == (1, 9, 10, 3, 1, 200, 6,
                                 "tactical_list", "bold_claim", "text", "AI hiring", "awareness")

    def test_get_recent_posted_post_ids(self):
        conn, cur = _conn(fetch_all=[(4,), (7,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_posted_post_ids
            assert get_recent_posted_post_ids(1, days=10) == [4, 7]
        assert cur.execute.call_args[0][1] == (1, 10)

    def test_get_post_engagement_rows(self):
        rows = [(datetime(2026, 7, 1, 15, 0), 10, 2, 1)]
        conn, cur = _conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_engagement_rows
            assert get_post_engagement_rows(1) == rows
        assert cur.execute.call_args[0][1] == (1, 1)

    def test_get_post_engagement_rows_none_coerced(self):
        conn, cur = _conn()
        cur.fetchall.return_value = None
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_engagement_rows
            assert get_post_engagement_rows(1) == []


class TestMarkNewsletterPublished:
    def test_with_url_updates_url_too(self):
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_newsletter_published
            assert mark_newsletter_published(1, "https://li.com/nl/1") is True
        sql, params = cur.execute.call_args[0]
        assert "newsletter_url=%s" in sql and params == ("https://li.com/nl/1", 1)

    def test_without_url_only_stamps_time(self):
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_newsletter_published
            assert mark_newsletter_published(1) is True
        sql, params = cur.execute.call_args[0]
        assert "newsletter_url" not in sql and params == (1,)


class TestScheduledDms:
    def test_get_scheduled_dm(self):
        row = {"id": 4, "user_id": 1, "message": "hi"}
        conn, cur = _conn(fetch_one=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_scheduled_dm
            assert get_scheduled_dm(4) == row
        assert cur.execute.call_args[0][1] == (4,)

    def test_get_scheduled_dm_user_id(self):
        with patch(f"{_DB}.get_scheduled_dm", return_value={"user_id": 9}):
            from cqc_lem.utilities.db import get_scheduled_dm_user_id
            assert get_scheduled_dm_user_id(4) == 9

    def test_get_scheduled_dm_user_id_none(self):
        with patch(f"{_DB}.get_scheduled_dm", return_value=None):
            from cqc_lem.utilities.db import get_scheduled_dm_user_id
            assert get_scheduled_dm_user_id(4) is None

    def test_get_scheduled_dms_serializes_datetimes(self):
        when = datetime(2026, 7, 10, 15, 30)
        conn, cur = _conn()
        cur.fetchone.return_value = {"c": 1}
        cur.fetchall.return_value = [{"id": 1, "scheduled_time": when,
                                      "created_at": when, "updated_at": None}]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_scheduled_dms
            result = get_scheduled_dms(1, status_filter="pending", page=2, page_size=10,
                                       sort_order="desc")
        assert result["total"] == 1 and result["page"] == 2
        dm = result["dms"][0]
        assert dm["scheduled_time"] == when.isoformat()
        assert dm["created_at"] == when.isoformat() and dm["updated_at"] is None
        # DESC order + status filter + OFFSET (page 2 → offset 10) applied
        list_sql, list_params = cur.execute.call_args_list[1][0]
        assert "DESC" in list_sql and "AND status = %s" in list_sql
        assert list_params == (1, "pending", 10, 10)
