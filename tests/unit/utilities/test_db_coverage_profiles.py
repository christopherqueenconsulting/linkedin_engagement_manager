"""Coverage tests for LinkedIn-profile CRUD + user-lookup DB helpers (db.py).

Shaped as parametrized contract tables (issue #1216), the format
`test_db_coverage_errors.py` established: one table per contract these helpers share
(error fallback, scalar read, exact DELETE statement, cached-row read), and a plain
test only where the case is genuinely one of a kind.
"""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit



def _err_conn(fake_cursor):
    conn, cur = fake_cursor()
    cur.execute.side_effect = mysql.connector.Error(msg="boom")
    return conn


def _profile():
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    return LinkedInProfile(full_name="Jane Doe", job_title="CTO", company_name="Acme",
                           email="jane@acme.com")


# (function name, args, kwargs, expected fallback on mysql.connector.Error)
_ERROR_CASES = [
    ("add_linkedin_profile", ("<profile>",), {}, False),
    ("get_linked_in_profile_by_url", ("https://li.com/in/jane",), {}, None),
    ("get_linked_in_profile_by_email", ("jane@acme.com",), {}, None),
    ("get_linked_in_profile_by_user_id", (5,), {}, None),
    ("remove_linked_in_profile_by_user_id", (3,), {}, False),
    ("remove_linked_in_profile_by_url", ("u",), {}, False),
    ("remove_linked_in_profile_by_email", ("jane@acme.com",), {}, False),
    ("update_user", (9,), {"blog_url": "b"}, False),
    ("get_profile_synthesis", (1,), {}, None),
    ("set_profile_synthesis", (1, "brief"), {}, False),
    ("get_user_ids_needing_profile_synthesis", (), {}, []),
]


class TestMysqlErrorFallbacks:
    @pytest.mark.parametrize("fname,args,kwargs,expected",
                             _ERROR_CASES, ids=[c[0] for c in _ERROR_CASES])
    def test_error_returns_documented_fallback(self, fname, args, kwargs, expected, fake_cursor):
        import cqc_lem.utilities.db as db
        args = tuple(_profile() if a == "<profile>" else a for a in args)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn(fake_cursor)):
            assert getattr(db, fname)(*args, **kwargs) == expected


# (function name, args, row the cursor returns, expected return, expected execute params)
_CACHED_PROFILE_READS = [
    ("get_linked_in_profile_by_url", ("https://li.com/in/jane",), {"updated_less_than_days_ago": 3},
     ('{"full_name": "Jane"}',), ('{"full_name": "Jane"}',),
     # Both slash variants are queried, so a row saved with a trailing slash still matches.
     ("https://li.com/in/jane/", "https://li.com/in/jane", 3)),
    ("get_linked_in_profile_by_email", ("jane@acme.com", 2), {},
     ('{"x": 1}',), ('{"x": 1}',), ("jane@acme.com", 2)),
    ("get_linked_in_profile_by_user_id", (5,), {},
     ('{"x": 2}',), ('{"x": 2}',), (5, 1)),
]


class TestCachedProfileReads:
    @pytest.mark.parametrize("fname,args,kwargs,row,expected,params",
                             _CACHED_PROFILE_READS,
                             ids=[c[0] for c in _CACHED_PROFILE_READS])
    def test_reads_row_with_expected_params(self, fname, args, kwargs, row, expected, params, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args, **kwargs) == expected
        assert cur.execute.call_args[0][1] == params


# (function name, args, the exact (sql, params) the helper must execute)
_DELETE_STATEMENTS = [
    ("remove_linked_in_profile_by_user_id", (3,),
     ("DELETE FROM profiles WHERE user_id = %s", (3,))),
    ("remove_linked_in_profile_by_url", ("https://li.com/in/jane",),
     ("DELETE FROM profiles WHERE profile_url = %s", ("https://li.com/in/jane",))),
    ("remove_linked_in_profile_by_email", ("jane@acme.com",),
     ("DELETE FROM profiles WHERE email = %s", ("jane@acme.com",))),
]


class TestRemoveLinkedInProfiles:
    @pytest.mark.parametrize("fname,args,statement",
                             _DELETE_STATEMENTS, ids=[c[0] for c in _DELETE_STATEMENTS])
    def test_deletes_by_its_own_key_and_commits(self, fname, args, statement, fake_cursor):
        import cqc_lem.utilities.db as db
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args) is True
        assert cur.execute.call_args[0] == statement
        conn.commit.assert_called_once()


# (id, function name, args, row the cursor returns, expected return, SQL fragment or None)
_SCALAR_READS = [
    ("last_planned_post_date", "get_last_planned_post_date_for_user", (1,),
     ("<when>",), "<when>", "MAX(scheduled_time)"),
    ("last_planned_post_date_no_row", "get_last_planned_post_date_for_user", (1,),
     None, None, None),
    ("blog_url", "get_user_blog_url", (1,),
     ("https://blog.example.com",), "https://blog.example.com", None),
    ("blog_url_no_row", "get_user_blog_url", (1,), None, None, None),
    ("sitemap_url", "get_user_sitemap_url", (1,),
     ("https://x.com/sitemap.xml",), "https://x.com/sitemap.xml", None),
    ("sitemap_url_no_row", "get_user_sitemap_url", (1,), None, None, None),
    ("location", "get_user_location", (1,), ("40.71", "-74.00"), (40.71, -74.00), None),
    # Half a coordinate pair is not a location — it must read as "unset", not as (None, None).
    ("location_missing_coords", "get_user_location", (1,), (None, None), None, None),
    ("location_no_row", "get_user_location", (1,), None, None, None),
]


class TestScalarUserReads:
    @pytest.mark.parametrize("case_id,fname,args,row,expected,sql_fragment",
                             _SCALAR_READS, ids=[c[0] for c in _SCALAR_READS])
    def test_reads_value_or_none(self, case_id, fname, args, row, expected, sql_fragment, fake_cursor):
        from datetime import datetime

        import cqc_lem.utilities.db as db
        when = datetime(2026, 7, 1, 12, 0)
        row = (when,) if row == ("<when>",) else row
        expected = when if expected == "<when>" else expected
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert getattr(db, fname)(*args) == expected
        if sql_fragment:
            assert sql_fragment in cur.execute.call_args[0][0]


class TestAddLinkedInProfile:
    def test_upserts_profile_row(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import add_linkedin_profile
            assert add_linkedin_profile(_profile(), user_id=7) is True
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "INSERT INTO profiles" in sql and "ON DUPLICATE KEY UPDATE" in sql
        assert params[1] == "jane@acme.com" and params[3] == 7
        conn.commit.assert_called_once()


class TestGetPostTypeCounts:
    def test_returns_type_to_count_map(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[{"post_type": "text", "count": 4},
                                     {"post_type": "video", "count": 2}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_type_counts
            counts = get_post_type_counts(1)
        assert counts == {"text": 4, "video": 2}
        assert "GROUP BY post_type" in cur.execute.call_args[0][0]


class TestUpdateUser:
    def test_no_fields_returns_false_without_touching_db(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as gdc:
            from cqc_lem.utilities.db import update_user
            assert update_user(1) is False
        gdc.assert_not_called()

    def test_blog_url_only(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="https://x.com/blog") is True
        sql, values = cur.execute.call_args[0]
        assert sql == "UPDATE users SET blog_url = %s WHERE id = %s"
        assert values == ["https://x.com/blog", 9]

    def test_all_fields(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b", sitemap_url="s") is True
        sql, values = cur.execute.call_args[0]
        assert "blog_url = %s, sitemap_url = %s" in sql
        assert values == ["b", "s", 9]

    # --- issue #1574: clearing a URL is a write, and re-saving one is not a failure -------------
    def test_re_saving_the_same_value_is_not_a_failure(self, fake_cursor):
        """MySQL reports 0 CHANGED rows when the stored value already matches.

        That used to be False, so pressing Save twice answered the second press with
        "Update failed".
        """
        conn, cur = fake_cursor(rowcount=0, fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b") is True
        assert "SELECT 1 FROM users WHERE id = %s" in cur.execute.call_args[0][0]

    def test_zero_rowcount_on_a_missing_user_returns_false(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0, fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="b") is False

    def test_none_clears_the_column(self, fake_cursor):
        """Removing a blog URL used to be swallowed by the falsy check.

        A silent no-op the Account page still reported as saved.
        """
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url=None) is True
        sql, values = cur.execute.call_args[0]
        assert sql == "UPDATE users SET blog_url = %s WHERE id = %s"
        assert values == [None, 9]

    def test_empty_string_is_stored_as_null(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, blog_url="", sitemap_url="s") is True
        _, values = cur.execute.call_args[0]
        assert values == [None, "s", 9]

    def test_an_omitted_column_is_left_alone(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user
            assert update_user(9, sitemap_url=None) is True
        sql, values = cur.execute.call_args[0]
        assert sql == "UPDATE users SET sitemap_url = %s WHERE id = %s"
        assert values == [None, 9]

    # --- issue #950: the address cannot move through here at all ------------------------------
    def test_email_keyword_is_rejected(self):
        """`update_user(..., email=…)` moved an account's address with no `user_email_history`
        row, no PIN to the new address and no session revoke. The parameter is gone, so the call
        raises instead of silently doing it.
        """
        with patch("cqc_lem.platform.db.connection.get_db_connection") as gdc:
            from cqc_lem.utilities.db import update_user
            with pytest.raises(TypeError):
                update_user(9, email="attacker@example.com")
        gdc.assert_not_called()

    def test_email_is_not_an_updatable_clause(self):
        from cqc_lem.utilities.db import _ALLOWED_USER_CLAUSES
        assert "email = %s" not in _ALLOWED_USER_CLAUSES


class TestActiveUserPasswordPairs:
    def test_collects_only_complete_pairs(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn, \
             patch("cqc_lem.platform.db.repositories.users.get_active_user_ids", return_value=[1, 2, 3]), \
             patch("cqc_lem.platform.db.repositories.users.get_user_password_pair_by_id",
                   side_effect=[("a@x.com", "pw1"), ("b@x.com", None), (None, None)]):
            from cqc_lem.utilities.db import get_active_user_password_pairs
            pairs = get_active_user_password_pairs()
        assert pairs == [["a@x.com", "pw1"]]
        get_conn.assert_not_called()  # no dangling direct connection — the helpers own their own

    def test_empty_when_no_active_users(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn, \
             patch("cqc_lem.platform.db.repositories.users.get_active_user_ids", return_value=[]):
            from cqc_lem.utilities.db import get_active_user_password_pairs
            assert get_active_user_password_pairs() == []
        get_conn.assert_not_called()
