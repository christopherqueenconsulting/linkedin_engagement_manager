"""Coverage tests for log-query, user-settings and token DB helpers (db.py)."""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest
from mysql.connector import errorcode

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


class TestInsertNewLog:
    def test_inserts_with_enum_values(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import LogActionType, LogResultType, insert_new_log
            ok = insert_new_log(1, LogActionType.COMMENT, LogResultType.SUCCESS,
                                post_id=9, post_url="https://li.com/p/1", message="hi")
        assert ok is True
        params = cur.execute.call_args[0][1]
        assert params == (1, "comment", 9, "https://li.com/p/1", "hi", "success")
        conn.commit.assert_called_once()

    def test_rowcount_not_one_returns_false(self):
        conn, _ = _conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import LogActionType, LogResultType, insert_new_log
            assert insert_new_log(1, LogActionType.DM, LogResultType.FAILURE) is False


class TestLogLookups:
    def test_has_user_commented_true(self):
        conn, cur = _conn(fetch_one=(2,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_user_commented_on_post_url
            assert has_user_commented_on_post_url(1, "https://li.com/p/1") is True
        assert cur.execute.call_args[0][1] == (1, "https://li.com/p/1", "comment", "success")

    def test_has_user_commented_false(self):
        conn, _ = _conn(fetch_one=(0,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_user_commented_on_post_url
            assert has_user_commented_on_post_url(1, "u") is False

    def test_get_post_url_from_log(self):
        conn, cur = _conn(fetch_one=("https://li.com/posted",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_url_from_log_for_user
            assert get_post_url_from_log_for_user(1, 9) == "https://li.com/posted"
        assert cur.execute.call_args[0][1] == (1, 9, "post", "success")

    def test_get_post_message_from_log(self):
        conn, _ = _conn(fetch_one=("my post text",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_message_from_log_for_user
            assert get_post_message_from_log_for_user(1, 9) == "my post text"

    def test_has_engaged_url_within_days(self):
        conn, cur = _conn(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_engaged_url_with_x_days
            assert has_engaged_url_with_x_days(1, "u", 7) is True
        assert cur.execute.call_args[0][1] == (1, "u", "engaged", "success", 7)

    def test_dm_history_filters_empty_messages(self):
        conn, cur = _conn(fetch_all=[("hello",), (None,), ("follow-up",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_history_for_profile
            msgs = get_dm_history_for_profile(1, "https://li.com/in/jane")
        assert msgs == ["hello", "follow-up"]
        assert cur.execute.call_args[0][1] == (1, "https://li.com/in/jane", "dm")

    def test_get_recent_logs(self):
        rows = [{"id": 1, "action_type": "comment"}]
        conn, cur = _conn(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_logs
            assert get_recent_logs(1, limit=5) == rows
        assert cur.execute.call_args[0][1] == (1, 5)

    def test_count_comments_and_dms_today(self):
        conn, cur = _conn(fetch_one=(4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_comments_today, count_dms_sent_today
            assert count_comments_today(1) == 4
            assert count_dms_sent_today(1) == 4
        # comment first, then dm
        first_params = cur.execute.call_args_list[0][0][1]
        second_params = cur.execute.call_args_list[1][0][1]
        assert first_params == (1, "comment", "success")
        assert second_params == (1, "dm", "success")


class TestPostStatusAndCompanyUrl:
    def test_get_post_status(self):
        conn, _ = _conn(fetch_one=("approved",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_status
            assert get_post_status(5) == "approved"

    def test_get_post_status_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_status
            assert get_post_status(5) is None

    def test_get_company_url(self):
        conn, _ = _conn(fetch_one=("https://li.com/company/acme",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_company_linked_in_url_for_user
            assert get_company_linked_in_url_for_user(1) == "https://li.com/company/acme"

    def test_get_company_url_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_company_linked_in_url_for_user
            assert get_company_linked_in_url_for_user(1) is None


class TestUserSettingsWrites:
    def test_update_linkedin_password(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_password
            assert update_user_linkedin_password(1, "dummy-test-value") is True
        assert cur.execute.call_args[0] == (
            "UPDATE users SET password = %s WHERE id = %s", ("dummy-test-value", 1))
        conn.commit.assert_called_once()

    def test_get_linkedin_display_name(self):
        conn, _ = _conn(fetch_one=("  Christopher Queen  ",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_linkedin_display_name
            assert get_user_linkedin_display_name(1) == "Christopher Queen"

    def test_get_linkedin_display_name_blank_is_none(self):
        # A blank string must read as "not set", or the required field would look satisfied and
        # every reply check would compare against '' (issue #731).
        conn, _ = _conn(fetch_one=("   ",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_linkedin_display_name
            assert get_user_linkedin_display_name(1) is None

    def test_get_linkedin_display_name_no_row(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_linkedin_display_name
            assert get_user_linkedin_display_name(99) is None

    def test_get_linkedin_display_name_db_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_linkedin_display_name
            assert get_user_linkedin_display_name(1) is None

    def test_update_linkedin_display_name(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_display_name
            assert update_user_linkedin_display_name(1, " Christopher Queen ") is True
        assert cur.execute.call_args[0] == (
            "UPDATE users SET linkedin_display_name = %s WHERE id = %s", ("Christopher Queen", 1))
        conn.commit.assert_called_once()

    def test_update_linkedin_display_name_clears_on_empty(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_display_name
            assert update_user_linkedin_display_name(1, "   ") is True
        assert cur.execute.call_args[0][1] == (None, 1)

    def test_update_linkedin_display_name_db_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_display_name
            assert update_user_linkedin_display_name(1, "Jordan") is False

    def test_update_user_settings(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_settings
            assert update_user_settings(1, blog_url="b", sitemap_url="s") is True
        assert cur.execute.call_args[0][1] == ("b", "s", 1)


class TestAddUserByEmail:
    def test_creates_trial_user_and_attaches_stripe_customer(self):
        conn, cur = _conn(rowcount=1, lastrowid=42)
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

    def test_stripe_failure_is_non_fatal(self):
        conn, cur = _conn(rowcount=1, lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.stripe_util.create_stripe_customer",
                   side_effect=RuntimeError("stripe down")):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("new@x.com") == 42
        # Only the INSERT ran — no stripe_customer_id update
        assert len(cur.execute.call_args_list) == 1

    def test_duplicate_email_returns_existing_user_id(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(
            msg="dup", errno=errorcode.ER_DUP_ENTRY)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.platform.db.repositories.users.get_user_id", return_value=7):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("existing@x.com") == 7

    def test_other_db_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom", errno=9999)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_user_by_email
            assert add_user_by_email("x@x.com") is None


class TestUserEmailAndTokenInfo:
    def test_get_user_email(self):
        conn, _ = _conn(fetch_one={"email": "a@x.com"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_email
            assert get_user_email(1) == "a@x.com"

    def test_get_user_email_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_email
            assert get_user_email(1) is None

    def test_get_user_token_info(self):
        row = {"access_token": "tok", "access_token_expires_in": 3600}
        conn, cur = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_token_info
            assert get_user_token_info(1) == row
        assert "refresh_token" in cur.execute.call_args[0][0]


class TestUpdateAccessToken:
    def test_with_refresh_token_updates_both(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600, refresh_token="rt",
                                            refresh_token_expires_in=86400) is True
        sql, params = cur.execute.call_args[0]
        assert "refresh_token = %s" in sql
        assert params[0] == "at" and params[3] == "rt" and params[-1] == 1

    def test_without_refresh_token(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600) is True
        sql = cur.execute.call_args[0][0]
        assert "refresh_token" not in sql

    def test_zero_rowcount_false(self):
        conn, _ = _conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_access_token
            assert update_user_access_token(1, "at", 3600) is False


class TestUpdateLinkedInToken:
    def test_with_refresh_token(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_token
            assert update_user_linkedin_token(1, "sub", "at", 3600, refresh_token="rt",
                                              refresh_token_expires_in=86400,
                                              linkedin_email="li@x.com") is True
        sql, params = cur.execute.call_args[0]
        assert "linkedin_connection_status = 'connected'" in sql
        assert "refresh_token = %s" in sql
        assert params[0] == "sub" and params[1] == "li@x.com"

    def test_without_refresh_token_blank_email_coerced_to_null(self):
        conn, cur = _conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_linkedin_token
            assert update_user_linkedin_token(1, "sub", "at", 3600, linkedin_email="") is True
        sql, params = cur.execute.call_args[0]
        assert "refresh_token" not in sql
        assert params[1] is None


class TestStripeSubscriptionQueries:
    def test_get_users_with_stripe_subscriptions(self):
        rows = [{"id": 1, "stripe_customer_id": "cus_1"}]
        conn, cur = _conn(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_with_stripe_subscriptions
            assert get_users_with_stripe_subscriptions() == rows
        assert "stripe_subscription_id IS NOT NULL" in cur.execute.call_args[0][0]

    def test_empty_fetchall_none_coerced_to_list(self):
        conn, cur = _conn()
        cur.fetchall.return_value = None
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_with_stripe_subscriptions
            assert get_users_with_stripe_subscriptions() == []

    def test_get_user_by_stripe_customer_id(self):
        row = {"id": 3, "stripe_customer_id": "cus_9"}
        conn, cur = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_by_stripe_customer_id
            assert get_user_by_stripe_customer_id("cus_9") == row
        assert cur.execute.call_args[0][1] == ("cus_9",)
