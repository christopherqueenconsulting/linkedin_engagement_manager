"""Coverage tests for LinkedIn-profile CRUD + user-lookup DB helpers (db.py)."""

import pytest
from unittest.mock import MagicMock, patch

import mysql.connector

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


def _profile():
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    return LinkedInProfile(full_name="Jane Doe", job_title="CTO", company_name="Acme",
                           email="jane@acme.com")


class TestAddLinkedInProfile:
    def test_upserts_profile_row(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_linkedin_profile
            assert add_linkedin_profile(_profile(), user_id=7) is True
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "INSERT INTO profiles" in sql and "ON DUPLICATE KEY UPDATE" in sql
        assert params[1] == "jane@acme.com" and params[3] == 7
        conn.commit.assert_called_once()

    def test_returns_false_on_db_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_linkedin_profile
            assert add_linkedin_profile(_profile()) is False


class TestGetLinkedInProfileGetters:
    def test_by_url_queries_both_slash_variants(self):
        conn, cur = _conn(fetch_one=('{"full_name": "Jane"}',))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_url
            result = get_linked_in_profile_by_url("https://li.com/in/jane", updated_less_than_days_ago=3)
        params = cur.execute.call_args[0][1]
        assert params == ("https://li.com/in/jane/", "https://li.com/in/jane", 3)
        assert result == ('{"full_name": "Jane"}',)

    def test_by_url_returns_none_on_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_url
            assert get_linked_in_profile_by_url("https://li.com/in/jane") is None

    def test_by_email(self):
        conn, cur = _conn(fetch_one=('{"x": 1}',))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_email
            assert get_linked_in_profile_by_email("jane@acme.com", 2) == ('{"x": 1}',)
        assert cur.execute.call_args[0][1] == ("jane@acme.com", 2)

    def test_by_email_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_email
            assert get_linked_in_profile_by_email("jane@acme.com") is None

    def test_by_user_id(self):
        conn, cur = _conn(fetch_one=('{"x": 2}',))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_user_id
            assert get_linked_in_profile_by_user_id(5) == ('{"x": 2}',)
        assert cur.execute.call_args[0][1] == (5, 1)

    def test_by_user_id_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_linked_in_profile_by_user_id
            assert get_linked_in_profile_by_user_id(5) is None


class TestRemoveLinkedInProfiles:
    def test_remove_by_user_id(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_user_id
            assert remove_linked_in_profile_by_user_id(3) is True
        assert cur.execute.call_args[0] == ("DELETE FROM profiles WHERE user_id = %s", (3,))
        conn.commit.assert_called_once()

    def test_remove_by_user_id_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_user_id
            assert remove_linked_in_profile_by_user_id(3) is False

    def test_remove_by_url(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_url
            assert remove_linked_in_profile_by_url("https://li.com/in/jane") is True
        assert cur.execute.call_args[0] == (
            "DELETE FROM profiles WHERE profile_url = %s", ("https://li.com/in/jane",))

    def test_remove_by_url_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_url
            assert remove_linked_in_profile_by_url("u") is False

    def test_remove_by_email(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_email
            assert remove_linked_in_profile_by_email("jane@acme.com") is True
        assert cur.execute.call_args[0] == (
            "DELETE FROM profiles WHERE email = %s", ("jane@acme.com",))

    def test_remove_by_email_error(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import remove_linked_in_profile_by_email
            assert remove_linked_in_profile_by_email("jane@acme.com") is False


class TestGetPostTypeCounts:
    def test_returns_type_to_count_map(self):
        conn, cur = _conn(fetch_all=[{"post_type": "text", "count": 4},
                                     {"post_type": "video", "count": 2}])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_type_counts
            counts = get_post_type_counts(1)
        assert counts == {"text": 4, "video": 2}
        assert "GROUP BY post_type" in cur.execute.call_args[0][0]


class TestUserUrlGetters:
    def test_last_planned_post_date(self):
        from datetime import datetime
        when = datetime(2026, 7, 1, 12, 0)
        conn, cur = _conn(fetch_one=(when,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_planned_post_date_for_user
            assert get_last_planned_post_date_for_user(1) == when
        assert "MAX(scheduled_time)" in cur.execute.call_args[0][0]

    def test_last_planned_post_date_none_row(self):
        conn, _ = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_planned_post_date_for_user
            assert get_last_planned_post_date_for_user(1) is None

    def test_blog_url(self):
        conn, _ = _conn(fetch_one=("https://blog.example.com",))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_blog_url
            assert get_user_blog_url(1) == "https://blog.example.com"

    def test_blog_url_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_blog_url
            assert get_user_blog_url(1) is None

    def test_sitemap_url(self):
        conn, _ = _conn(fetch_one=("https://x.com/sitemap.xml",))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_sitemap_url
            assert get_user_sitemap_url(1) == "https://x.com/sitemap.xml"

    def test_sitemap_url_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_sitemap_url
            assert get_user_sitemap_url(1) is None


class TestUpdateUser:
    def test_no_fields_returns_false_without_touching_db(self):
        with patch(f"{_DB}.get_db_connection") as gdc:
            from cqc_lem.utilities.db import update_user
            assert update_user(1) is False
        gdc.assert_not_called()

    def test_blog_url_only(self):
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="https://x.com/blog") is True
        sql, values = cur.execute.call_args[0]
        assert sql == "UPDATE users SET blog_url = %s WHERE id = %s"
        assert values == ["https://x.com/blog", 9]

    def test_all_fields(self):
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b", sitemap_url="s") is True
        sql, values = cur.execute.call_args[0]
        assert "blog_url = %s, sitemap_url = %s" in sql
        assert values == ["b", "s", 9]

    def test_zero_rowcount_returns_false(self):
        conn, _ = _conn(rowcount=0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b") is False

    def test_db_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b") is False

    # --- issue #950: the address cannot move through here at all ------------------------------
    def test_email_keyword_is_rejected(self):
        """`update_user(..., email=…)` moved an account's address with no `user_email_history`
        row, no PIN to the new address and no session revoke. The parameter is gone, so the call
        raises instead of silently doing it."""
        with patch(f"{_DB}.get_db_connection") as gdc:
            from cqc_lem.utilities.db import update_user
            with pytest.raises(TypeError):
                update_user(9, email="attacker@example.com")
        gdc.assert_not_called()

    def test_email_is_not_an_updatable_clause(self):
        from cqc_lem.utilities.db import _ALLOWED_USER_CLAUSES
        assert "email = %s" not in _ALLOWED_USER_CLAUSES


class TestGetUserLocation:
    def test_returns_float_tuple(self):
        conn, _ = _conn(fetch_one=("40.71", "-74.00"))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_location
            assert get_user_location(1) == (40.71, -74.00)

    def test_none_when_missing_coords(self):
        conn, _ = _conn(fetch_one=(None, None))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_location
            assert get_user_location(1) is None

    def test_none_when_no_row(self):
        conn, _ = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_location
            assert get_user_location(1) is None


class TestActiveUserPasswordPairs:
    def test_collects_only_complete_pairs(self):
        with patch(f"{_DB}.get_db_connection") as get_conn, \
             patch(f"{_DB}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_DB}.get_user_password_pair_by_id",
                   side_effect=[("a@x.com", "pw1"), ("b@x.com", None), (None, None)]):
            from cqc_lem.utilities.db import get_active_user_password_pairs
            pairs = get_active_user_password_pairs()
        assert pairs == [["a@x.com", "pw1"]]
        get_conn.assert_not_called()  # no dangling direct connection — the helpers own their own

    def test_empty_when_no_active_users(self):
        with patch(f"{_DB}.get_db_connection") as get_conn, \
             patch(f"{_DB}.get_active_user_ids", return_value=[]):
            from cqc_lem.utilities.db import get_active_user_password_pairs
            assert get_active_user_password_pairs() == []
        get_conn.assert_not_called()


class TestProfileSynthesisErrorPaths:
    def test_get_profile_synthesis_error_returns_none(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            assert get_profile_synthesis(1) is None

    def test_set_profile_synthesis_error_returns_false(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_profile_synthesis
            assert set_profile_synthesis(1, "brief") is False

    def test_user_ids_needing_synthesis_error_returns_empty(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis
            assert get_user_ids_needing_profile_synthesis() == []
