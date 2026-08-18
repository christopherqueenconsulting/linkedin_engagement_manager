"""Coverage tests for avatar-training, group, post-stats and scheduled-DM DB helpers (db.py).

Shaped as parametrized contract tables (issue #1216) in the style of
`test_db_coverage_errors.py`: the shared contracts — error fallback, read-a-row-with-these-params,
statement-shape-by-argument — are one table each; the multi-statement paths (activation order,
attribution snapshot, JSON coercion) stay plain tests because each asserts a different sequence.
"""

from datetime import datetime
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit



def _err_conn(fake_cursor):
    conn, cur = fake_cursor()
    cur.execute.side_effect = mysql.connector.Error(msg="boom")
    return conn


# (function name, args, expected fallback on mysql.connector.Error)
_ERROR_CASES = [
    ("get_avatar_credit_ledger_entry_by_session", ("cs_1",), None),
    ("refund_avatar_credit", (3, "train_1"), False),
    ("set_active_avatar", (3, 11), False),
    ("get_avatar_trainings", (3,), []),
    ("update_avatar_training_status", ("t1", "failed"), False),
    ("get_user_groups", (1,), []),
]


class TestMysqlErrorFallbacks:
    @pytest.mark.parametrize("fname,args,expected",
                             _ERROR_CASES, ids=[c[0] for c in _ERROR_CASES])
    def test_error_returns_documented_fallback(self, fname, args, expected, fake_cursor):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn(fake_cursor)):
            assert getattr(db, fname)(*args) == expected


# (case id, function name, args, kwargs, fetch_one, fetch_all, expected,
#  expected execute params or None, SQL fragment or None)
_ROW_READS = [
    ("avatar_ledger_entry", "get_avatar_credit_ledger_entry_by_session", ("cs_1",), {},
     {"id": 1, "user_id": 3, "delta": 5}, None, {"id": 1, "user_id": 3, "delta": 5},
     ("cs_1",), "delta > 0"),
    ("recent_posted_post_ids", "get_recent_posted_post_ids", (1,), {"days": 10},
     None, [(4,), (7,)], [4, 7], (1, 10), None),
    ("post_engagement_rows", "get_post_engagement_rows", (1,), {},
     None, [(datetime(2026, 7, 1, 15, 0), 10, 2, 1)],
     [(datetime(2026, 7, 1, 15, 0), 10, 2, 1)], (1, 1), None),
    ("scheduled_dm", "get_scheduled_dm", (4,), {},
     {"id": 4, "user_id": 1, "message": "hi"}, None, {"id": 4, "user_id": 1, "message": "hi"},
     (4,), None),
]


class TestRowReads:
    @pytest.mark.parametrize(
        "case_id,fname,args,kwargs,fetch_one,fetch_all,expected,params,sql_fragment",
        _ROW_READS, ids=[c[0] for c in _ROW_READS])
    def test_reads_with_expected_params(self, case_id, fname, args, kwargs, fetch_one, fetch_all,
                                        expected, params, sql_fragment, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor(fetch_one=fetch_one, fetch_all=fetch_all)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args, **kwargs) == expected
        if params is not None:
            assert cur.execute.call_args[0][1] == params
        if sql_fragment:
            assert sql_fragment in cur.execute.call_args[0][0]


class TestAvatarLedger:
    def test_refund_avatar_credit(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import refund_avatar_credit
            assert refund_avatar_credit(3, "train_1") is True
        sql, params = cur.execute.call_args[0]
        assert "'training_refund'" in sql and params == (3, "train_1")
        conn.commit.assert_called_once()


class TestAvatarTrainings:
    def test_set_active_avatar_validates_then_deactivates_then_activates(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 11, "approval_status": "approved"}, rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 11) is True
        select_sql, select_params = cur.execute.call_args_list[0][0]
        first_sql, first_params = cur.execute.call_args_list[1][0]
        second_sql, second_params = cur.execute.call_args_list[2][0]
        assert "SELECT id" in select_sql and select_params == (11, 3)
        assert "is_active = 0" in first_sql and first_params == (3,)
        assert "is_active = 1" in second_sql and second_params == (11, 3)

    # (case id, the row the validating SELECT returns) — both must refuse WITHOUT running the
    # deactivating UPDATE, or a refusal would strand the account with no active avatar.
    @pytest.mark.parametrize("case_id,row", [
        ("unknown_id", None),
        # The approval gate (issue #744): activation is not reachable from 'succeeded' alone.
        ("unapproved", {"id": 11, "approval_status": "pending"}),
    ], ids=["unknown_id", "unapproved"])
    def test_set_active_avatar_refusal_preserves_current_active(self, case_id, row, fake_cursor):
        conn, cur = fake_cursor(fetch_one=row, rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_active_avatar
            assert set_active_avatar(3, 11 if row else 999) is False
        executed = [c[0][0] for c in cur.execute.call_args_list]
        assert len(executed) == 1 and "SELECT id" in executed[0]
        conn.commit.assert_not_called()

    def test_get_avatar_trainings_serializes_rows(self, fake_cursor):
        created = datetime(2026, 7, 1, 9, 0)
        rows = [{"id": 1, "training_id": "t1", "model_ref": "m", "trigger_word": "TOK",
                 "status": "succeeded", "is_active": 1, "created_at": created,
                 "updated_at": None}]
        conn, _ = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
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


class TestUserGroups:
    def test_get_user_groups_coerces_flags_to_bool_and_date_to_iso(self, fake_cursor):
        rows = [{"group_id": "g1", "group_name": "AI Leaders", "enabled": 1, "post_enabled": 0,
                 "last_posted_at": datetime(2026, 7, 28, 15, 0)},
                {"group_id": "g2", "group_name": "Sales", "enabled": 0, "post_enabled": 1,
                 "last_posted_at": None}]
        conn, _ = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_groups
            result = get_user_groups(1)
        # Commenting and posting are independent flags (issue #769) — neither implies the other.
        assert result[0]["enabled"] is True and result[0]["post_enabled"] is False
        assert result[1]["enabled"] is False and result[1]["post_enabled"] is True
        # JSON-serializable for the SPA payload.
        assert result[0]["last_posted_at"] == "2026-07-28T15:00:00"
        assert result[1]["last_posted_at"] is None


class TestPostStats:
    def test_record_post_stats_coerces_none_counts(self, fake_cursor):
        # No matching post row → attribution snapshot is all-NULL but the stat is still recorded.
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_post_stats
            assert record_post_stats(1, 9, None, None, reposts=None, impressions=120, saves=None) is True
        params = cur.execute.call_args[0][1]  # last execute = the INSERT
        assert params == (1, 9, 0, 0, 0, 120, 0, None, None, None, None, None)

    def test_record_post_stats_snapshots_post_attribution(self, fake_cursor):
        # The post's shape/topic is snapshotted onto the stat row at capture time (#386).
        conn, cur = fake_cursor(fetch_one=("tactical_list", "bold_claim", "text", "AI hiring", "awareness"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_post_stats
            assert record_post_stats(1, 9, 10, 3, reposts=1, impressions=200, saves=6) is True
        select_sql, select_params = cur.execute.call_args_list[0][0]
        assert "FROM posts WHERE id=%s AND user_id=%s" in select_sql and select_params == (9, 1)
        insert_sql, insert_params = cur.execute.call_args_list[1][0]
        assert "INSERT INTO post_stats" in insert_sql and "`format`" in insert_sql
        assert insert_params == (1, 9, 10, 3, 1, 200, 6,
                                 "tactical_list", "bold_claim", "text", "AI hiring", "awareness")

    def test_get_post_engagement_rows_none_coerced(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_engagement_rows
            assert get_post_engagement_rows(1) == []


class TestMarkNewsletterPublished:
    # (case id, args, SQL fragment that must be present, fragment that must be absent, params)
    @pytest.mark.parametrize("case_id,args,present,absent,params", [
        ("with_url", (1, "https://li.com/nl/1"), "newsletter_url=%s", None,
         ("https://li.com/nl/1", 1)),
        ("without_url", (1,), None, "newsletter_url", (1,)),
    ], ids=["with_url", "without_url"])
    def test_updates_url_only_when_one_is_given(self, case_id, args, present, absent, params, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_newsletter_published
            assert mark_newsletter_published(*args) is True
        sql, executed_params = cur.execute.call_args[0]
        if present:
            assert present in sql
        if absent:
            assert absent not in sql
        assert executed_params == params


class TestScheduledDms:
    # (case id, what get_scheduled_dm returns, the user id read off it)
    @pytest.mark.parametrize("case_id,row,expected", [
        ("row_present", {"user_id": 9}, 9),
        ("no_row", None, None),
    ], ids=["row_present", "no_row"])
    def test_get_scheduled_dm_user_id(self, case_id, row, expected):
        with patch("cqc_lem.platform.db.repositories.outreach.get_scheduled_dm", return_value=row):
            from cqc_lem.utilities.db import get_scheduled_dm_user_id
            assert get_scheduled_dm_user_id(4) == expected

    def test_get_scheduled_dms_serializes_datetimes(self, fake_cursor):
        when = datetime(2026, 7, 10, 15, 30)
        conn, cur = fake_cursor(fetch_one={"c": 1})
        cur.fetchall.return_value = [{"id": 1, "scheduled_time": when,
                                      "created_at": when, "updated_at": None}]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
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
