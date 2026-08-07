"""Unit tests for PIN auth, session management, and planned-posts DB functions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_and_cursor(dictionary: bool = False) -> tuple:
    """Return a (connection_mock, cursor_mock) pair wired together."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _patch_conn(conn):
    """Return a context-manager patch for get_db_connection."""
    return patch("cqc_lem.utilities.db.get_db_connection", return_value=conn)


def _sha256(token: str) -> str:
    """What `sessions.session_token` stores since #745 (2b) — the plaintext never reaches SQL."""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# create_pin_for_email
# ---------------------------------------------------------------------------

class TestCreatePinForEmail:
    def test_success_deletes_old_then_inserts_and_returns_true(self):
        from cqc_lem.utilities.db import create_pin_for_email

        conn, cursor = _make_conn_and_cursor()
        cursor.rowcount = 1

        with _patch_conn(conn):
            result = create_pin_for_email("user@example.com", "hashed_pin")

        assert result is True
        # First execute: DELETE old unused PINs
        first_call = cursor.execute.call_args_list[0]
        assert "DELETE" in first_call[0][0].upper()
        assert "user@example.com" in first_call[0][1]
        # Second execute: INSERT new PIN
        second_call = cursor.execute.call_args_list[1]
        assert "INSERT" in second_call[0][0].upper()
        assert "user@example.com" in second_call[0][1]
        assert "hashed_pin" in second_call[0][1]
        conn.commit.assert_called_once()
        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_returns_false_when_rowcount_not_one(self):
        from cqc_lem.utilities.db import create_pin_for_email

        conn, cursor = _make_conn_and_cursor()
        cursor.rowcount = 0

        with _patch_conn(conn):
            result = create_pin_for_email("user@example.com", "hashed_pin")

        assert result is False

    def test_returns_false_on_db_error(self):
        from cqc_lem.utilities.db import create_pin_for_email

        conn, cursor = _make_conn_and_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            result = create_pin_for_email("user@example.com", "hashed_pin")

        assert result is False
        # cleanup still happens
        cursor.close.assert_called_once()
        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# verify_pin_for_email
# ---------------------------------------------------------------------------

class TestVerifyPinForEmail:
    def test_valid_row_marks_used_and_returns_true(self):
        from cqc_lem.utilities.db import verify_pin_for_email

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        # First fetchone is the lockout probe (no lock), second is the PIN row itself.
        cursor.fetchone.side_effect = [None, {"id": 5}]

        with _patch_conn(conn):
            result = verify_pin_for_email("user@example.com", "correct_hash")

        assert result is True
        # Should UPDATE the row to used=1
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c[0][0].upper()
        ]
        assert len(update_calls) == 1
        assert 5 in update_calls[0][0][1]
        conn.commit.assert_called_once()

    def test_no_row_found_returns_false(self):
        from cqc_lem.utilities.db import verify_pin_for_email

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = None

        with _patch_conn(conn):
            result = verify_pin_for_email("user@example.com", "wrong_hash")

        assert result is False
        # The only UPDATE a wrong PIN may issue is the failed-attempt counter — never used = 1.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c[0][0].upper()
        ]
        assert len(update_calls) == 1
        assert "attempts" in update_calls[0][0][0]
        assert "used = 1" not in update_calls[0][0][0]

    def test_db_error_returns_false(self):
        from cqc_lem.utilities.db import verify_pin_for_email

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            result = verify_pin_for_email("user@example.com", "any_hash")

        assert result is False
        cursor.close.assert_called_once()
        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# delete_pin_for_email
# ---------------------------------------------------------------------------

class TestDeletePinForEmail:
    def test_executes_delete_and_commits(self):
        from cqc_lem.utilities.db import delete_pin_for_email

        conn, cursor = _make_conn_and_cursor()

        with _patch_conn(conn):
            delete_pin_for_email("user@example.com")

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args[0]
        assert "DELETE" in sql.upper()
        assert "user@example.com" in params
        conn.commit.assert_called_once()

    def test_db_error_is_logged_not_raised(self):
        """A DB error during delete must not propagate — function returns None."""
        from cqc_lem.utilities.db import delete_pin_for_email

        conn, cursor = _make_conn_and_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            # Must not raise
            result = delete_pin_for_email("user@example.com")

        assert result is None
        cursor.close.assert_called_once()
        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------

class TestCreateSession:
    def test_success_returns_64_char_hex_token(self):
        from cqc_lem.utilities.db import create_session

        conn, cursor = _make_conn_and_cursor()

        with _patch_conn(conn):
            token = create_session(42)

        assert token is not None
        assert isinstance(token, str)
        assert len(token) == 64
        # Should be valid hex
        int(token, 16)

        # Verify INSERT into sessions was called with the token and user_id
        insert_calls = [
            c for c in cursor.execute.call_args_list
            if "INSERT" in c[0][0].upper()
        ]
        assert len(insert_calls) == 1
        insert_params = insert_calls[0][0][1]
        # The row stores the HASH; the plaintext token goes to the caller and nowhere else.
        assert token not in insert_params
        assert _sha256(token) in insert_params
        assert 42 in insert_params

        # Verify last_login UPDATE was also called
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c[0][0].upper()
        ]
        assert len(update_calls) == 1
        assert 42 in update_calls[0][0][1]

        conn.commit.assert_called_once()

    def test_db_error_returns_none(self):
        from cqc_lem.utilities.db import create_session

        conn, cursor = _make_conn_and_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            token = create_session(99)

        assert token is None
        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_each_call_produces_unique_token(self):
        from cqc_lem.utilities.db import create_session

        tokens = set()
        for _ in range(5):
            conn, cursor = _make_conn_and_cursor()
            with _patch_conn(conn):
                token = create_session(1)
            tokens.add(token)

        assert len(tokens) == 5


# ---------------------------------------------------------------------------
# get_session_user_id
# ---------------------------------------------------------------------------

class TestGetSessionUserId:
    def test_valid_non_expired_token_returns_user_id(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = {"user_id": 7}

        with _patch_conn(conn):
            uid = get_session_user_id("valid_token_string")

        assert uid == 7

    def test_expired_or_missing_token_returns_none(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = None

        with _patch_conn(conn):
            uid = get_session_user_id("expired_or_bad_token")

        assert uid is None

    def test_db_error_returns_none(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            uid = get_session_user_id("any_token")

        assert uid is None
        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_query_passes_token_and_now(self):
        """Ensure the WHERE clause includes both the token and a datetime comparison."""
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = None

        with _patch_conn(conn):
            get_session_user_id("my_session_token")

        sql, params = cursor.execute.call_args[0]
        assert "expires_at" in sql
        assert "my_session_token" not in params
        assert _sha256("my_session_token") in params

    def test_valid_token_slides_expiry_forward(self):
        """A live token should be extended (sliding idle window) and still return the user."""
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = {
            "user_id": 7,
            "created_at": datetime.now(timezone.utc),
        }

        with _patch_conn(conn):
            uid = get_session_user_id("live_token")

        assert uid == 7
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c[0][0].upper()
        ]
        assert len(update_calls) == 1
        assert "expires_at" in update_calls[0][0][0]
        assert _sha256("live_token") in update_calls[0][0][1]
        conn.commit.assert_called_once()

    def test_sliding_expiry_capped_by_absolute_max(self):
        """Near the absolute cap the new expiry must not exceed created_at + max days."""
        from cqc_lem.utilities.db import get_session_user_id

        old_created = datetime.now(timezone.utc) - timedelta(days=29, hours=23)
        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = {"user_id": 3, "created_at": old_created}

        with _patch_conn(conn):
            uid = get_session_user_id("aging_token")

        assert uid == 3
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE" in c[0][0].upper()
        ]
        new_expiry = update_calls[0][0][1][0]
        cap = old_created + timedelta(days=30)
        # Capped to the absolute max, not now + idle window
        assert abs((new_expiry - cap).total_seconds()) < 5

    def test_naive_created_at_is_handled(self):
        """created_at coming back tz-naive (typical MySQL TIMESTAMP) must not raise."""
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _make_conn_and_cursor(dictionary=True)
        cursor.fetchone.return_value = {
            "user_id": 9,
            "created_at": datetime.now(),  # naive
        }

        with _patch_conn(conn):
            uid = get_session_user_id("naive_token")

        assert uid == 9


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------

class TestDeleteSession:
    def test_success_returns_true(self):
        from cqc_lem.utilities.db import delete_session

        conn, cursor = _make_conn_and_cursor()

        with _patch_conn(conn):
            result = delete_session("some_token")

        assert result is True
        sql, params = cursor.execute.call_args[0]
        assert "DELETE" in sql.upper()
        assert "some_token" not in params
        assert _sha256("some_token") in params
        conn.commit.assert_called_once()

    def test_db_error_returns_false_not_raised(self):
        from cqc_lem.utilities.db import delete_session

        conn, cursor = _make_conn_and_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")

        with _patch_conn(conn):
            result = delete_session("bad_token")

        assert result is False
        cursor.close.assert_called_once()
        conn.close.assert_called_once()
