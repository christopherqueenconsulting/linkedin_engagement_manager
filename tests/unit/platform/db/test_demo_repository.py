"""The demo-account statements behind `scripts/seed_demo_account.py` (issue #2371).

What has to hold: every statement is bound to the synthetic account's reserved-domain email, so no
id mix-up can reach another person's rows, and a non-demo email is refused before any SQL runs.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.platform.db.enums import PostStatus, PostType
from cqc_lem.platform.db.repositories import demo

pytestmark = pytest.mark.unit

_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_EMAIL = "dana.reyes@example.com"


def _sql(cursor) -> list:
    return [" ".join(c.args[0].split()) for c in cursor.execute.call_args_list]


class TestOnlyADemoEmailIsAccepted:
    @pytest.mark.parametrize("email", ["dana@reyesadvisory.com", "someone@gmail.com", "",
                                       "@example.com", "example.com"])
    @pytest.mark.parametrize("call", [
        lambda e: demo.ensure_demo_user(e),
        lambda e: demo.reset_demo_user_rows(1, e),
        lambda e: demo.insert_demo_post(1, e, "x", PostStatus.PENDING, datetime.now(timezone.utc)),
    ])
    def test_a_real_looking_email_is_refused_before_any_sql(self, fake_cursor, email, call):
        conn, cursor = fake_cursor()
        with patch(_CONN, return_value=conn), pytest.raises(demo.NotADemoAccount):
            call(email)
        cursor.execute.assert_not_called()

    def test_case_and_whitespace_are_normalised(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one=(3,))
        with patch(_CONN, return_value=conn):
            assert demo.ensure_demo_user("  Dana.Reyes@Example.com ") == 3
        assert cursor.execute.call_args_list[0].args[1] == (_EMAIL,)


class TestEnsureDemoUser:
    def test_an_existing_account_is_reused_not_duplicated(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one=(7,))
        with patch(_CONN, return_value=conn):
            assert demo.ensure_demo_user(_EMAIL) == 7
        statements = _sql(cursor)
        assert not any(s.startswith("INSERT") for s in statements)
        update = cursor.execute.call_args_list[-1]
        assert "WHERE id = %s AND email = %s" in update.args[0]
        assert update.args[1][-2:] == (7, _EMAIL)

    def test_a_missing_account_is_created_without_a_stripe_customer(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one=None, lastrowid=12)
        with patch(_CONN, return_value=conn), \
                patch("cqc_lem.utilities.stripe_util.create_stripe_customer") as stripe:
            assert demo.ensure_demo_user(_EMAIL) == 12
        assert any(s.startswith("INSERT INTO users (email, public_uid)") for s in _sql(cursor))
        stripe.assert_not_called()

    def test_a_database_fault_answers_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_CONN, return_value=conn):
            assert demo.ensure_demo_user(_EMAIL) is None


class TestResetDemoUserRows:
    def test_every_delete_is_bound_to_the_demo_email(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=4)
        with patch(_CONN, return_value=conn):
            assert demo.reset_demo_user_rows(5, _EMAIL) == {"posts": 4, "engagement_preferences": 4}
        calls = cursor.execute.call_args_list
        assert len(calls) == 2
        for call in calls:
            assert call.args[0].startswith("DELETE")
            assert "JOIN users u" in call.args[0] and "u.email = %s" in call.args[0]
            assert call.args[1] == (5, _EMAIL)
        assert {c.args[0].split()[1] for c in calls} == {"p", "ep"}

    def test_the_account_row_itself_is_kept(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch(_CONN, return_value=conn):
            demo.reset_demo_user_rows(5, _EMAIL)
        assert not any("DELETE FROM users" in s or s.startswith("DELETE u") for s in _sql(cursor))

    def test_a_database_fault_answers_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_CONN, return_value=conn):
            assert demo.reset_demo_user_rows(5, _EMAIL) is None


class TestInsertDemoPost:
    def test_the_insert_only_lands_for_the_demo_account(self, fake_cursor):
        conn, cursor = fake_cursor(lastrowid=40)
        when = datetime(2026, 10, 12, 13, tzinfo=timezone.utc)
        with patch(_CONN, return_value=conn):
            post_id = demo.insert_demo_post(5, _EMAIL, "body", PostStatus.POSTED, when,
                                            PostType.TEXT, "decision", "promo")
        assert post_id == 40
        sql, params = cursor.execute.call_args.args
        assert "FROM users u WHERE u.id = %s AND u.email = %s" in sql
        assert params == (datetime(2026, 10, 12, 13), "text", "decision", "promo", "posted", "body",
                          5, _EMAIL)

    def test_no_matching_account_inserts_nothing(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch(_CONN, return_value=conn):
            assert demo.insert_demo_post(5, _EMAIL, "b", PostStatus.PENDING,
                                         datetime.now(timezone.utc)) is None

    def test_a_database_fault_answers_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_CONN, return_value=conn):
            assert demo.insert_demo_post(5, _EMAIL, "b", PostStatus.PENDING,
                                         datetime.now(timezone.utc)) is None


def test_the_facade_re_exports_the_demo_names():
    from cqc_lem.utilities import db
    for name in ("ensure_demo_user", "reset_demo_user_rows", "insert_demo_post",
                 "NotADemoAccount", "DEMO_EMAIL_DOMAINS"):
        assert getattr(db, name) is getattr(demo, name)
        assert name in db.__all__
