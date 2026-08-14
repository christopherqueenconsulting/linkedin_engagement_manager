"""Coverage tests for log-query, user-settings and token DB helpers (db.py).

Shaped as parametrized contract tables (issue #1216) in the style of
`test_db_coverage_errors.py`: the helpers here share four contracts — read one scalar
off one row, read a row list, execute one exact statement, fall back on
`mysql.connector.Error` — so each is one table, and only the genuinely one-of-a-kind
cases stay as plain tests.
"""

from unittest.mock import patch

import mysql.connector
import pytest
from mysql.connector import errorcode

pytestmark = pytest.mark.unit


def _err_conn(fake_cursor):
    conn, cur = fake_cursor()
    cur.execute.side_effect = mysql.connector.Error("boom")
    return conn


# (case id, function name, args, row the cursor returns, expected return,
#  expected execute params or None, SQL fragment that must appear or None)
_SCALAR_READS = [
    ("has_user_commented_true", "has_user_commented_on_post_url", (1, "https://li.com/p/1"),
     (2,), True, (1, "https://li.com/p/1", "comment", "success"), None),
    ("has_user_commented_false", "has_user_commented_on_post_url", (1, "u"),
     (0,), False, None, None),
    ("post_url_from_log", "get_post_url_from_log_for_user", (1, 9),
     ("https://li.com/posted",), "https://li.com/posted", (1, 9, "post", "success"), None),
    ("post_message_from_log", "get_post_message_from_log_for_user", (1, 9),
     ("my post text",), "my post text", None, None),
    ("engaged_within_days", "has_engaged_url_with_x_days", (1, "u", 7),
     (1,), True, (1, "u", "engaged", "success", 7), None),
    ("post_status", "get_post_status", (5,), ("approved",), "approved", None, None),
    ("post_status_no_row", "get_post_status", (5,), None, None, None, None),
    ("company_url", "get_company_linked_in_url_for_user", (1,),
     ("https://li.com/company/acme",), "https://li.com/company/acme", None, None),
    ("company_url_no_row", "get_company_linked_in_url_for_user", (1,), None, None, None, None),
    ("display_name", "get_user_linkedin_display_name", (1,),
     ("  Christopher Queen  ",), "Christopher Queen", None, None),
    # A blank string must read as "not set", or the required field would look satisfied and
    # every reply check would compare against '' (issue #731).
    ("display_name_blank_is_none", "get_user_linkedin_display_name", (1,),
     ("   ",), None, None, None),
    ("display_name_no_row", "get_user_linkedin_display_name", (99,), None, None, None, None),
    ("user_email", "get_user_email", (1,), {"email": "a@x.com"}, "a@x.com", None, None),
    ("user_email_no_row", "get_user_email", (1,), None, None, None, None),
    # The row is handed back with the decrypted `refresh_token` filled in — absent in the raw
    # row, so a caller reading it never sees a KeyError.
    ("token_info", "get_user_token_info", (1,),
     {"access_token": "tok", "access_token_expires_in": 3600},
     {"access_token": "tok", "access_token_expires_in": 3600, "refresh_token": None},
     None, "refresh_token"),
    ("user_by_stripe_customer", "get_user_by_stripe_customer_id", ("cus_9",),
     {"id": 3, "stripe_customer_id": "cus_9"}, {"id": 3, "stripe_customer_id": "cus_9"},
     ("cus_9",), None),
]


class TestScalarReads:
    @pytest.mark.parametrize("case_id,fname,args,row,expected,params,sql_fragment",
                             _SCALAR_READS, ids=[c[0] for c in _SCALAR_READS])
    def test_reads_one_value_off_one_row(self, case_id, fname, args, row, expected, params,
                                         sql_fragment, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args) == expected
        if params is not None:
            assert cur.execute.call_args[0][1] == params
        if sql_fragment:
            assert sql_fragment in cur.execute.call_args[0][0]


# (case id, function name, args, rows the cursor returns, expected return,
#  expected execute params or None, SQL fragment or None)
_ROW_LIST_READS = [
    ("recent_logs", "get_recent_logs", (1,), {"limit": 5},
     [{"id": 1, "action_type": "comment"}], [{"id": 1, "action_type": "comment"}],
     (1, 5), None),
    ("stripe_subscriptions", "get_users_with_stripe_subscriptions", (), {},
     [{"id": 1, "stripe_customer_id": "cus_1"}], [{"id": 1, "stripe_customer_id": "cus_1"}],
     None, "stripe_subscription_id IS NOT NULL"),
]


class TestRowListReads:
    @pytest.mark.parametrize("case_id,fname,args,kwargs,rows,expected,params,sql_fragment",
                             _ROW_LIST_READS, ids=[c[0] for c in _ROW_LIST_READS])
    def test_returns_the_row_list(self, case_id, fname, args, kwargs, rows, expected, params,
                                  sql_fragment, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args, **kwargs) == expected
        if params is not None:
            assert cur.execute.call_args[0][1] == params
        if sql_fragment:
            assert sql_fragment in cur.execute.call_args[0][0]


# (case id, function name, args, kwargs, the exact (sql, params) executed)
_WRITE_STATEMENTS = [
    ("linkedin_password", "update_user_linkedin_password", (1, "dummy-test-value"), {},
     ("UPDATE users SET password = %s WHERE id = %s", ("dummy-test-value", 1))),
    ("display_name", "update_user_linkedin_display_name", (1, " Christopher Queen "), {},
     ("UPDATE users SET linkedin_display_name = %s WHERE id = %s", ("Christopher Queen", 1))),
    # A whitespace-only name CLEARS the column rather than storing '' (issue #731).
    ("display_name_cleared", "update_user_linkedin_display_name", (1, "   "), {},
     ("UPDATE users SET linkedin_display_name = %s WHERE id = %s", (None, 1))),
]


class TestExactWriteStatements:
    @pytest.mark.parametrize("case_id,fname,args,kwargs,statement",
                             _WRITE_STATEMENTS, ids=[c[0] for c in _WRITE_STATEMENTS])
    def test_writes_and_commits(self, case_id, fname, args, kwargs, statement, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args, **kwargs) is True
        assert cur.execute.call_args[0] == statement
        conn.commit.assert_called_once()

    def test_update_user_settings_binds_both_urls(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_settings
            assert update_user_settings(1, blog_url="b", sitemap_url="s") is True
        assert cur.execute.call_args[0][1] == ("b", "s", 1)


# (function name, args, expected fallback on mysql.connector.Error)
_ERROR_CASES = [
    ("get_user_linkedin_display_name", (1,), None),
    ("update_user_linkedin_display_name", (1, "Jordan"), False),
]


class TestMysqlErrorFallbacks:
    @pytest.mark.parametrize("fname,args,expected",
                             _ERROR_CASES, ids=[c[0] for c in _ERROR_CASES])
    def test_error_returns_documented_fallback(self, fname, args, expected, fake_cursor):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn(fake_cursor)):
            assert getattr(db, fname)(*args) == expected


class TestInsertNewLog:
    def test_inserts_with_enum_values(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import LogActionType, LogResultType, insert_new_log
            ok = insert_new_log(1, LogActionType.COMMENT, LogResultType.SUCCESS,
                                post_id=9, post_url="https://li.com/p/1", message="hi")
        assert ok is True
        params = cur.execute.call_args[0][1]
        assert params == (1, "comment", 9, "https://li.com/p/1", "hi", "success")
        conn.commit.assert_called_once()

    def test_rowcount_not_one_returns_false(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import LogActionType, LogResultType, insert_new_log
            assert insert_new_log(1, LogActionType.DM, LogResultType.FAILURE) is False


class TestLogAggregates:
    def test_dm_history_filters_empty_messages(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("hello",), (None,), ("follow-up",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_history_for_profile
            msgs = get_dm_history_for_profile(1, "https://li.com/in/jane")
        assert msgs == ["hello", "follow-up"]
        assert cur.execute.call_args[0][1] == (1, "https://li.com/in/jane", "dm")

    def test_count_comments_and_dms_today(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_comments_today, count_dms_sent_today
            assert count_comments_today(1) == 4
            assert count_dms_sent_today(1) == 4
        # comment first, then dm
        first_params = cur.execute.call_args_list[0][0][1]
        second_params = cur.execute.call_args_list[1][0][1]
        assert first_params == (1, "comment", "success")
        assert second_params == (1, "dm", "success")

    def test_empty_fetchall_none_coerced_to_list(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_with_stripe_subscriptions
            assert get_users_with_stripe_subscriptions() == []


class TestAddUserByEmail:
    def test_creates_trial_user_and_attaches_stripe_customer(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1, lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.stripe_util.create_stripe_customer",
                   return_value="cus_123") as csc:
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("new@x.com") == 42
        csc.assert_called_once_with("new@x.com", 42)
        insert_sql = cur.execute.call_args_list[0][0][0]
        assert "subscription_status" in insert_sql and "'trial'" in insert_sql
        update_sql, update_params = cur.execute.call_args_list[1][0]
        assert "stripe_customer_id" in update_sql
        assert update_params == ("cus_123", 42)

    def test_stripe_failure_is_non_fatal(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1, lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.stripe_util.create_stripe_customer",
                   side_effect=RuntimeError("stripe down")):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("new@x.com") == 42
        # Only the INSERT ran — no stripe_customer_id update
        assert len(cur.execute.call_args_list) == 1

    def test_duplicate_email_returns_existing_user_id(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(
            msg="dup", errno=errorcode.ER_DUP_ENTRY)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.platform.db.repositories.users.get_user_id", return_value=7):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("existing@x.com") == 7

    def test_other_db_error_returns_none(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom", errno=9999)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("x@x.com") is None


class TestUpdateAccessToken:
    def test_with_refresh_token_updates_both(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600, refresh_token="rt",
                                            refresh_token_expires_in=86400) is True
        sql, params = cur.execute.call_args[0]
        assert "refresh_token = %s" in sql
        assert params[0] == "at" and params[3] == "rt" and params[-1] == 1

    def test_without_refresh_token(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600) is True
        sql = cur.execute.call_args[0][0]
        assert "refresh_token" not in sql

    def test_zero_rowcount_false(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600) is False


class TestUpdateLinkedInToken:
    def test_with_refresh_token(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_token
            assert update_user_linkedin_token(1, "sub", "at", 3600, refresh_token="rt",
                                              refresh_token_expires_in=86400,
                                              linkedin_email="li@x.com") is True
        sql, params = cur.execute.call_args[0]
        assert "linkedin_connection_status = 'connected'" in sql
        assert "refresh_token = %s" in sql
        assert params[0] == "sub" and params[1] == "li@x.com"

    def test_without_refresh_token_blank_email_coerced_to_null(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_token
            assert update_user_linkedin_token(1, "sub", "at", 3600, linkedin_email="") is True
        sql, params = cur.execute.call_args[0]
        assert "refresh_token" not in sql
        assert params[1] is None
