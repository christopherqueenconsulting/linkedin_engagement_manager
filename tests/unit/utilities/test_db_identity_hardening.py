"""Identity + session hardening in the DB layer (issue #745, phase 2b).

`db.py` is the only module that touches these tables, so this is where the guarantees live: the
plaintext session token never reaches SQL, revocation is scoped to the owning account, a PIN locks
after enough wrong guesses, and moving an email is recorded rather than silently overwritten.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit


def _conn_cursor() -> tuple:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _patch_conn(conn):
    return patch("cqc_lem.utilities.db.get_db_connection", return_value=conn)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Token hashing
# ---------------------------------------------------------------------------

class TestTokenHashing:
    def test_hash_is_deterministic_hex_of_the_right_width(self):
        from cqc_lem.utilities.crypto import hash_session_token

        assert hash_session_token("abc") == hash_session_token("abc") == _sha256("abc")
        # The sessions.session_token column is VARCHAR(64).
        assert len(hash_session_token("abc")) == 64

    def test_empty_token_hashes_to_nothing(self):
        from cqc_lem.utilities.crypto import hash_session_token

        assert hash_session_token(None) is None
        assert hash_session_token("") is None

    def test_hash_does_not_depend_on_the_master_key(self, monkeypatch):
        """A rotated or missing LEM_SECRET_KEY must not log every user out."""
        from cqc_lem.utilities.crypto import hash_session_token, generate_master_key

        monkeypatch.delenv("LEM_SECRET_KEY", raising=False)
        without = hash_session_token("tok")
        monkeypatch.setenv("LEM_SECRET_KEY", generate_master_key())
        assert hash_session_token("tok") == without

    def test_ip_hash_is_keyed_when_a_master_key_exists(self, monkeypatch):
        from cqc_lem.utilities.crypto import generate_master_key, hash_client_ip

        monkeypatch.delenv("LEM_SECRET_KEY", raising=False)
        unkeyed = hash_client_ip("203.0.113.9")
        monkeypatch.setenv("LEM_SECRET_KEY", generate_master_key())
        keyed = hash_client_ip("203.0.113.9")
        assert hash_client_ip(None) is None
        assert unkeyed != keyed
        # A ~2^32 space would be walkable from a plain digest.
        assert keyed != _sha256("ip:203.0.113.9")


class TestSessionRowIsHashOnly:
    def test_create_session_stores_the_hash_and_the_device_facts(self):
        from cqc_lem.utilities.db import create_session

        conn, cursor = _conn_cursor()
        with _patch_conn(conn):
            token = create_session(42, user_agent="Mozilla/5.0 (Macintosh) Chrome/1", ip="1.2.3.4")

        insert = next(c for c in cursor.execute.call_args_list if "INSERT" in c[0][0].upper())
        params = insert[0][1]
        assert _sha256(token) in params
        assert token not in params
        assert "1.2.3.4" not in params
        assert "Chrome on macOS" in params

    def test_device_label_falls_back_when_there_is_no_user_agent(self):
        from cqc_lem.utilities.db import _device_label

        assert _device_label(None) == "Unknown device"
        assert _device_label("Mozilla/5.0 (iPhone) Safari/604") == "Safari on iPhone"

    def test_lookup_ignores_a_revoked_session(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = None
        with _patch_conn(conn):
            assert get_session_user_id("tok") is None
        sql = cursor.execute.call_args_list[0][0][0]
        assert "revoked_at IS NULL" in sql

    def test_lookup_touches_last_seen(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"user_id": 5, "created_at": datetime.now(timezone.utc)}
        with _patch_conn(conn):
            assert get_session_user_id("tok") == 5
        update = next(c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper())
        assert "last_seen_at" in update[0][0]


class TestListAndRevoke:
    def test_list_returns_no_token_material_and_flags_the_current_device(self):
        from cqc_lem.utilities.db import list_user_sessions

        conn, cursor = _conn_cursor()
        cursor.fetchall.return_value = [
            {"id": 1, "session_token": _sha256("mine"), "label": "Chrome on macOS",
             "user_agent": "UA", "created_at": None, "last_seen_at": None, "expires_at": None},
            {"id": 2, "session_token": _sha256("theirs"), "label": None,
             "user_agent": "Mozilla/5.0 (iPhone) Safari/604", "created_at": None,
             "last_seen_at": None, "expires_at": None},
        ]
        with _patch_conn(conn):
            rows = list_user_sessions(7, current_token="mine")

        assert rows[0]["is_current"] is True
        assert rows[1]["is_current"] is False
        assert rows[1]["label"] == "Safari on iPhone"
        assert all("session_token" not in r for r in rows)

    def test_list_with_no_current_token_marks_nothing_current(self):
        from cqc_lem.utilities.db import list_user_sessions

        conn, cursor = _conn_cursor()
        cursor.fetchall.return_value = [
            {"id": 1, "session_token": None, "label": "Chrome on macOS", "user_agent": None,
             "created_at": None, "last_seen_at": None, "expires_at": None},
        ]
        with _patch_conn(conn):
            rows = list_user_sessions(7)
        assert rows[0]["is_current"] is False

    def test_revoke_is_scoped_to_the_owner(self):
        from cqc_lem.utilities.db import revoke_session

        conn, cursor = _conn_cursor()
        cursor.rowcount = 0
        with _patch_conn(conn):
            assert revoke_session(7, 99) is False
        sql, params = cursor.execute.call_args[0]
        assert "user_id = %s" in sql
        assert 7 in params

    def test_revoke_others_keeps_the_presented_token(self):
        from cqc_lem.utilities.db import revoke_other_sessions

        conn, cursor = _conn_cursor()
        cursor.rowcount = 3
        with _patch_conn(conn):
            assert revoke_other_sessions(7, keep_token="mine") == 3
        sql, params = cursor.execute.call_args[0]
        assert "session_token <> %s" in sql
        assert _sha256("mine") in params
        assert "mine" not in params

    def test_revoke_others_without_a_token_revokes_everything(self):
        from cqc_lem.utilities.db import revoke_other_sessions

        conn, cursor = _conn_cursor()
        cursor.rowcount = 4
        with _patch_conn(conn):
            assert revoke_other_sessions(7) == 4
        sql, _ = cursor.execute.call_args[0]
        assert "session_token <>" not in sql

    def test_db_error_revokes_nothing_rather_than_raising(self):
        from cqc_lem.utilities.db import revoke_other_sessions, revoke_session

        conn, cursor = _conn_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")
        with _patch_conn(conn):
            assert revoke_other_sessions(7) == 0
            assert revoke_session(7, 1) is False


class TestPinLockout:
    def test_wrong_pin_locks_after_the_configured_attempts(self):
        from cqc_lem.utilities.db import verify_pin_for_email
        from cqc_lem.utilities.env_constants import PIN_MAX_ATTEMPTS

        conn, cursor = _conn_cursor()
        cursor.fetchone.side_effect = [None, None]  # not locked, PIN does not match
        with _patch_conn(conn):
            assert verify_pin_for_email("user@example.com", "wrong") is False
        update = next(c for c in cursor.execute.call_args_list if "attempts" in c[0][0])
        assert "locked_until" in update[0][0]
        assert PIN_MAX_ATTEMPTS in update[0][1]

    def test_a_locked_email_never_reaches_the_pin_comparison(self):
        from cqc_lem.utilities.db import verify_pin_for_email

        conn, cursor = _conn_cursor()
        cursor.fetchone.side_effect = [{"id": 1}]  # the lockout probe finds a lock
        with _patch_conn(conn):
            assert verify_pin_for_email("user@example.com", "correct") is False
        assert cursor.execute.call_count == 1

    def test_get_pin_lockout_reads_the_live_lock(self):
        from cqc_lem.utilities.db import get_pin_lockout

        until = datetime.now(timezone.utc) + timedelta(minutes=15)
        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"locked_until": until}
        with _patch_conn(conn):
            assert get_pin_lockout("user@example.com") == until

    def test_get_pin_lockout_is_none_on_db_error(self):
        from cqc_lem.utilities.db import get_pin_lockout

        conn, cursor = _conn_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")
        with _patch_conn(conn):
            assert get_pin_lockout("user@example.com") is None


class TestPublicUid:
    def test_returns_the_stored_uid(self):
        from cqc_lem.utilities.db import get_user_public_uid

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"public_uid": "abc-123"}
        with _patch_conn(conn):
            assert get_user_public_uid(7) == "abc-123"

    def test_mints_one_for_a_row_that_has_none(self):
        from cqc_lem.utilities.db import get_user_public_uid

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"public_uid": None}
        with _patch_conn(conn):
            minted = get_user_public_uid(7)
        assert minted and minted != "abc-123"
        update = next(c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper())
        assert minted in update[0][1]

    def test_unknown_user_is_none(self):
        from cqc_lem.utilities.db import get_user_public_uid

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = None
        with _patch_conn(conn):
            assert get_user_public_uid(404) is None


class TestEmailChange:
    def test_records_history_and_stamps_verification(self):
        from cqc_lem.utilities.db import change_user_email

        conn, cursor = _conn_cursor()
        cursor.fetchone.side_effect = [None, {"email": "old@example.com"}]
        with _patch_conn(conn):
            assert change_user_email(7, "new@example.com", changed_by_session_id=11) is True

        insert = next(c for c in cursor.execute.call_args_list
                      if "user_email_history" in c[0][0])
        assert "old@example.com" in insert[0][1]
        assert "new@example.com" in insert[0][1]
        assert 11 in insert[0][1]

    def test_refuses_an_address_owned_by_another_account(self):
        from cqc_lem.utilities.db import change_user_email

        conn, cursor = _conn_cursor()
        cursor.fetchone.side_effect = [{"id": 99}]
        with _patch_conn(conn):
            assert change_user_email(7, "taken@example.com") is False
        assert not any("UPDATE users" in c[0][0] for c in cursor.execute.call_args_list)

    def test_unknown_user_changes_nothing(self):
        from cqc_lem.utilities.db import change_user_email

        conn, cursor = _conn_cursor()
        cursor.fetchone.side_effect = [None, None]
        with _patch_conn(conn):
            assert change_user_email(404, "new@example.com") is False


class TestAuthAudit:
    def test_writes_the_event_with_a_hashed_ip(self):
        from cqc_lem.utilities.db import AuthAuditEvent, record_auth_event

        conn, cursor = _conn_cursor()
        with _patch_conn(conn):
            assert record_auth_event(AuthAuditEvent.LOGIN_SUCCESS, user_id=7,
                                     email="me@example.com", ip="203.0.113.9",
                                     user_agent="UA", details={"method": "email_pin"}) is True
        sql, params = cursor.execute.call_args[0]
        assert "auth_audit_log" in sql
        assert "login_success" in params
        assert "203.0.113.9" not in params

    def test_truncates_an_oversized_user_agent(self):
        from cqc_lem.utilities.db import AuthAuditEvent, record_auth_event

        conn, cursor = _conn_cursor()
        with _patch_conn(conn):
            record_auth_event(AuthAuditEvent.LOGIN_FAILED, email="me@example.com",
                              user_agent="x" * 900, success=False)
        params = cursor.execute.call_args[0][1]
        # VARCHAR(512) — an over-long agent string must not fail the write.
        assert any(isinstance(p, str) and len(p) == 512 for p in params)

    def test_a_failed_audit_write_never_raises(self):
        from cqc_lem.utilities.db import AuthAuditEvent, record_auth_event

        conn, cursor = _conn_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")
        with _patch_conn(conn):
            assert record_auth_event(AuthAuditEvent.LOGOUT, user_id=7) is False

    def test_history_read_returns_no_ip_hash(self):
        from cqc_lem.utilities.db import get_auth_audit_events

        conn, cursor = _conn_cursor()
        cursor.fetchall.return_value = [{"event": "login_success", "success": 1,
                                         "user_agent": "UA", "created_at": None}]
        with _patch_conn(conn):
            rows = get_auth_audit_events(7)
        assert rows[0]["event"] == "login_success"
        assert "ip_hash" not in cursor.execute.call_args[0][0]


# ---------------------------------------------------------------------------
# Session SCOPE plumbing (issue #745, phase 2c.1 — issue #905)
# ---------------------------------------------------------------------------

class TestSessionScopeResolution:
    """`resolve_session` is what lets the API refuse a scoped session on the SAME query that
    authenticates it — reading the scope separately would double a query every request makes."""

    def test_resolve_returns_the_user_and_the_scope(self):
        from cqc_lem.utilities.db import resolve_session

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"user_id": 7, "created_at": datetime.now(timezone.utc),
                                        "scope": "extension"}
        with _patch_conn(conn):
            assert resolve_session("tok") == {"user_id": 7, "scope": "extension"}
        # Still hashed before it reaches SQL.
        assert cursor.execute.call_args_list[0][0][1][0] == _sha256("tok")

    def test_a_legacy_row_with_no_scope_resolves_as_full(self):
        """`scope` arrived in 2c with a 'full' default; a NULL must not read as a restriction."""
        from cqc_lem.utilities.db import resolve_session

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"user_id": 7, "created_at": None, "scope": None}
        with _patch_conn(conn):
            assert resolve_session("tok")["scope"] == "full"

    def test_an_expired_or_unknown_token_resolves_to_nothing(self):
        from cqc_lem.utilities.db import resolve_session

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = None
        with _patch_conn(conn):
            assert resolve_session("tok") is None

    def test_get_session_user_id_still_answers_the_id_alone(self):
        from cqc_lem.utilities.db import get_session_user_id

        conn, cursor = _conn_cursor()
        cursor.fetchone.return_value = {"user_id": 7, "created_at": None, "scope": "full"}
        with _patch_conn(conn):
            assert get_session_user_id("tok") == 7


class TestEnrollmentScopeRelease:
    def test_release_is_conditional_on_the_current_scope(self):
        """A full, recovery or extension session enrolling a factor must not be widened by this —
        which is why the check is inside the UPDATE and not a read-then-write."""
        from cqc_lem.utilities.db import release_enrollment_scope

        conn, cursor = _conn_cursor()
        cursor.rowcount = 1
        with _patch_conn(conn):
            assert release_enrollment_scope("tok") is True
        sql, params = cursor.execute.call_args[0]
        assert "scope = %s" in sql and "AND scope = %s" in sql
        assert params == ("full", _sha256("tok"), "enroll")

    def test_nothing_held_is_not_an_error(self):
        from cqc_lem.utilities.db import release_enrollment_scope

        conn, cursor = _conn_cursor()
        cursor.rowcount = 0
        with _patch_conn(conn):
            assert release_enrollment_scope("tok") is False

    def test_a_failed_write_never_raises(self):
        from cqc_lem.utilities.db import release_enrollment_scope

        conn, cursor = _conn_cursor()
        cursor.execute.side_effect = mysql.connector.Error("DB error")
        with _patch_conn(conn):
            assert release_enrollment_scope("tok") is False

    def test_an_empty_token_never_reaches_sql(self):
        from cqc_lem.utilities.db import release_enrollment_scope

        conn, cursor = _conn_cursor()
        with _patch_conn(conn):
            assert release_enrollment_scope("") is False
        cursor.execute.assert_not_called()
