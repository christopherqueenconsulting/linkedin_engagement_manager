"""Issue #1450 — the queries behind the admin User Management surface.

The one thing worth pinning here is that "is an admin" is asked with the SAME predicate
`is_user_admin` decides with — the column OR the `ADMIN_USER_EMAILS` allowlist. A guard that
counted the column alone would allow the last admin of an allowlist-run deployment to be demoted.
"""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestEffectiveAdminPredicate:
    def test_column_only_when_the_allowlist_is_empty(self):
        from cqc_lem.utilities.db import _effective_admin_sql
        with patch("cqc_lem.utilities.db.admin_email_allowlist", return_value=set()):
            sql, params = _effective_admin_sql()
        # An `IN ()` is a MySQL syntax error, so the empty case has to drop the clause entirely.
        assert sql == "u.is_admin = 1"
        assert params == []

    def test_the_allowlist_is_ored_in_as_parameters(self):
        from cqc_lem.utilities.db import _effective_admin_sql
        with patch("cqc_lem.utilities.db.admin_email_allowlist",
                   return_value={"boss@x.com", "ops@x.com"}):
            sql, params = _effective_admin_sql()
        assert "u.is_admin = 1 OR LOWER(u.email) IN (%s, %s)" in sql
        assert params == ["boss@x.com", "ops@x.com"]


class TestLikeTerm:
    def test_wildcards_in_the_search_are_literal(self):
        from cqc_lem.utilities.db import _like_term
        # A `_` an operator would match any character, quietly widening what the admin is reading.
        assert _like_term("a_b%c") == "%a\\_b\\%c%"

    def test_a_backslash_is_escaped_before_the_wildcards(self):
        from cqc_lem.utilities.db import _like_term
        assert _like_term("a\\b") == "%a\\\\b%"


class TestAdminUserFilters:
    def test_no_filters_is_no_where_clause(self):
        from cqc_lem.utilities.db import _admin_user_filters
        where, params = _admin_user_filters()
        assert where == ""
        assert params == []

    def test_search_matches_either_address(self):
        from cqc_lem.utilities.db import _admin_user_filters
        where, params = _admin_user_filters(search="acme.com")
        assert "u.email LIKE" in where and "u.linkedin_email LIKE" in where
        assert params == ["%acme.com%", "%acme.com%"]

    def test_status_filters_are_parameters_not_interpolation(self):
        from cqc_lem.utilities.db import _admin_user_filters
        where, params = _admin_user_filters(subscription_status="trial",
                                            linkedin_connection_status="expired")
        assert "u.subscription_status = %s" in where
        assert "u.linkedin_connection_status = %s" in where
        assert params == ["trial", "expired"]

    def test_is_admin_filter_uses_the_effective_predicate(self):
        from cqc_lem.utilities.db import _admin_user_filters
        with patch("cqc_lem.utilities.db.admin_email_allowlist", return_value={"boss@x.com"}):
            where, params = _admin_user_filters(is_admin=True)
        assert "LOWER(u.email) IN (%s)" in where
        assert params == ["boss@x.com"]

    def test_is_admin_false_negates_the_same_predicate(self):
        from cqc_lem.utilities.db import _admin_user_filters
        with patch("cqc_lem.utilities.db.admin_email_allowlist", return_value=set()):
            where, _ = _admin_user_filters(is_admin=False)
        assert "NOT u.is_admin = 1" in where


class TestListUsersForAdmin:
    def test_returns_the_page_newest_signup_first(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[{"id": 2, "email": "a@x.com"}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import list_users_for_admin
            rows = list_users_for_admin(limit=10, offset=20)
        assert rows == [{"id": 2, "email": "a@x.com"}]
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY u.id DESC LIMIT %s OFFSET %s" in sql
        assert "LEFT JOIN onboarding_state" in sql
        assert params == (10, 20)

    def test_no_credential_column_is_ever_selected(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import list_users_for_admin
            list_users_for_admin()
        sql = cur.execute.call_args[0][0]
        for forbidden in ("password", "access_token", "refresh_token", "proxy_url",
                          "reply_inbound_token", "latitude", "longitude",
                          "stripe_customer_id", "stripe_subscription_id", "*"):
            assert forbidden not in sql

    def test_a_db_error_is_an_empty_page_not_an_exception(self, fake_cursor):
        conn, cur = fake_cursor(execute_error=mysql.connector.Error("db down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import list_users_for_admin
            assert list_users_for_admin() == []


class TestCountUsersForAdmin:
    def test_counts_under_the_same_filters(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(7,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_users_for_admin
            assert count_users_for_admin(subscription_status="active") == 7
        sql, params = cur.execute.call_args[0]
        assert sql.startswith("SELECT COUNT(*)")
        assert params == ("active",)

    def test_a_db_error_reads_as_zero(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("db down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_users_for_admin
            assert count_users_for_admin() == 0


class TestGetUserForAdmin:
    def test_joins_the_preferences_a_drawer_shows(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 1})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_for_admin
            assert get_user_for_admin(1) == {"id": 1}
        sql, params = cur.execute.call_args[0]
        assert "LEFT JOIN engagement_preferences" in sql
        assert params == (1,)

    def test_missing_user_is_none(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_for_admin
            assert get_user_for_admin(999) is None

    def test_a_db_error_is_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("db down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_for_admin
            assert get_user_for_admin(1) is None


class TestCountAdminUsers:
    def test_counts_the_effective_admins(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(2,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.utilities.db.admin_email_allowlist", return_value={"boss@x.com"}):
            from cqc_lem.utilities.db import count_admin_users
            assert count_admin_users() == 2
        sql, params = cur.execute.call_args[0]
        assert "LOWER(u.email) IN (%s)" in sql
        assert params == ("boss@x.com",)

    def test_an_unreadable_count_is_none_never_zero(self, fake_cursor):
        # 0 would mean "no admins left", which would refuse the very revoke/grant an operator runs
        # to fix a lockout. The route answers 503 on None instead.
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("db down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_admin_users
            assert count_admin_users() is None


class TestSetUserAdmin:
    def test_writes_and_commits(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_user_admin
            assert set_user_admin(4, True) is True
        assert cur.execute.call_args[0][1] == (1, 4)
        conn.commit.assert_called_once()

    def test_revoke_writes_zero(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_user_admin
            assert set_user_admin(4, False) is True
        assert cur.execute.call_args[0][1] == (0, 4)

    def test_no_row_changed_is_not_a_successful_write(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_user_admin
            assert set_user_admin(999, True) is False

    def test_a_db_error_reports_failure(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("db down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_user_admin
            assert set_user_admin(4, True) is False
