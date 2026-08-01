"""Issue #745 (2b): identity + session hardening against a live, migrated MySQL.

Unit tests mock the cursor, so they prove the Python but not the schema. These prove the parts only
a real server can answer: the migration's columns exist and the code writes what it says it writes
(a hash in `sessions.session_token`, never the token), revocation really takes a session out of
circulation, the PIN lockout survives a round trip, `public_uid` is unique per account, and moving
an email writes history rather than overwriting silently.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.crypto import hash_session_token

pytestmark = pytest.mark.integration

_EMAIL = "identity-745-2b@example.test"
_EMAIL_NEW = "identity-745-2b-new@example.test"
_EMAIL_OTHER = "identity-745-2b-other@example.test"


def _schema_available() -> bool:
    """A reachable server is not enough — these need the 2b migration. An un-migrated DB skips."""
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW TABLES LIKE 'auth_audit_log'")
        present = bool(cursor.fetchone())
        if present:
            cursor.execute("SHOW COLUMNS FROM sessions LIKE 'revoked_at'")
            present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=(), fetch: bool = False):
    connection = db.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows
    finally:
        cursor.close()
        connection.close()


def _cleanup():
    for email in (_EMAIL, _EMAIL_NEW, _EMAIL_OTHER):
        _exec("DELETE FROM users WHERE email=%s", (email,))
        _exec("DELETE FROM email_pin_auth WHERE email=%s", (email,))
        # auth_audit_log deliberately has no user FK (a failed login has no account), so its rows
        # outlive the user row and have to be cleared by hand.
        _exec("DELETE FROM auth_audit_log WHERE email=%s", (email,))


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the #745 2b integration test")
    _cleanup()
    uid = db.add_user_by_email(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _cleanup()


class TestSessionsAtRest:
    def test_the_row_holds_a_hash_and_never_the_token(self, user_id):
        token = db.create_session(user_id, user_agent="Mozilla/5.0 (Macintosh) Chrome/140",
                                  ip="203.0.113.7")
        assert token and len(token) == 64

        rows = _exec("SELECT session_token, user_agent, ip_hash, label FROM sessions "
                     "WHERE user_id=%s", (user_id,), fetch=True)
        assert len(rows) == 1
        assert rows[0]["session_token"] == hash_session_token(token)
        assert rows[0]["session_token"] != token
        # The raw address is never stored either.
        assert rows[0]["ip_hash"] and rows[0]["ip_hash"] != "203.0.113.7"
        assert rows[0]["label"] == "Chrome on macOS"

        # And the plaintext token still authenticates.
        assert db.get_session_user_id(token) == user_id

    def test_a_revoked_session_stops_authenticating(self, user_id):
        token = db.create_session(user_id)
        session_id = db.get_session_id(token)
        assert session_id

        assert db.revoke_session(user_id, session_id) is True
        assert db.get_session_user_id(token) is None
        # A second revoke is a no-op, not an error.
        assert db.revoke_session(user_id, session_id) is False

    def test_revoke_all_others_keeps_the_presented_device(self, user_id):
        keep = db.create_session(user_id, user_agent="keep")
        gone_one = db.create_session(user_id, user_agent="gone-1")
        gone_two = db.create_session(user_id, user_agent="gone-2")

        assert db.revoke_other_sessions(user_id, keep_token=keep) == 2
        assert db.get_session_user_id(keep) == user_id
        assert db.get_session_user_id(gone_one) is None
        assert db.get_session_user_id(gone_two) is None

    def test_the_session_list_names_the_current_device_and_leaks_nothing(self, user_id):
        current = db.create_session(user_id, user_agent="Mozilla/5.0 (iPhone) Safari/604")
        db.create_session(user_id, user_agent="Mozilla/5.0 (Windows NT) Firefox/1")

        rows = db.list_user_sessions(user_id, current_token=current)
        assert len(rows) == 2
        assert sum(1 for r in rows if r["is_current"]) == 1
        assert {"Safari on iPhone", "Firefox on Windows"} == {r["label"] for r in rows}
        assert all("session_token" not in r for r in rows)

    def test_an_expired_session_is_neither_listed_nor_valid(self, user_id):
        token = db.create_session(user_id)
        _exec("UPDATE sessions SET expires_at=%s WHERE user_id=%s",
              (datetime.now(timezone.utc) - timedelta(hours=1), user_id))
        assert db.get_session_user_id(token) is None
        assert db.list_user_sessions(user_id) == []

    def test_delete_session_removes_the_row_by_hash(self, user_id):
        token = db.create_session(user_id)
        assert db.delete_session(token) is True
        assert _exec("SELECT id FROM sessions WHERE user_id=%s", (user_id,), fetch=True) == []


class TestPinLockout:
    def test_wrong_pins_lock_the_outstanding_pin(self, user_id):
        from cqc_lem.utilities.env_constants import PIN_MAX_ATTEMPTS

        assert db.create_pin_for_email(_EMAIL, "correct-hash") is True
        for _ in range(PIN_MAX_ATTEMPTS):
            assert db.verify_pin_for_email(_EMAIL, "wrong-hash") is False

        assert db.get_pin_lockout(_EMAIL) is not None
        # Even the RIGHT PIN is refused while the lock stands — that is the point.
        assert db.verify_pin_for_email(_EMAIL, "correct-hash") is False

    def test_a_correct_pin_before_the_lock_is_consumed_once(self, user_id):
        assert db.create_pin_for_email(_EMAIL, "correct-hash") is True
        assert db.verify_pin_for_email(_EMAIL, "correct-hash") is True
        # Used PINs never verify twice.
        assert db.verify_pin_for_email(_EMAIL, "correct-hash") is False
        assert db.get_pin_lockout(_EMAIL) is None


class TestPublicUidAndEmail:
    def test_a_new_account_gets_a_unique_public_uid(self, user_id):
        other_id = db.add_user_by_email(_EMAIL_OTHER)
        try:
            mine = db.get_user_public_uid(user_id)
            theirs = db.get_user_public_uid(other_id)
            assert mine and theirs and mine != theirs
            assert db.get_user_id_by_public_uid(mine) == user_id
        finally:
            _exec("DELETE FROM users WHERE email=%s", (_EMAIL_OTHER,))

    def test_a_legacy_row_is_backfilled_on_first_read(self, user_id):
        _exec("UPDATE users SET public_uid=NULL WHERE id=%s", (user_id,))
        minted = db.get_user_public_uid(user_id)
        assert minted
        assert db.get_user_public_uid(user_id) == minted

    def test_changing_the_email_keeps_the_account_and_records_history(self, user_id):
        token = db.create_session(user_id)
        session_id = db.get_session_id(token)

        assert db.change_user_email(user_id, _EMAIL_NEW, changed_by_session_id=session_id) is True
        assert db.get_user_email(user_id) == _EMAIL_NEW
        # Same account, same public identity — the address is an attribute, not the identity.
        assert db.get_user_id(_EMAIL_NEW) == user_id

        history = _exec("SELECT old_email, new_email, changed_by_session_id FROM user_email_history "
                        "WHERE user_id=%s", (user_id,), fetch=True)
        assert history[0]["old_email"] == _EMAIL
        assert history[0]["new_email"] == _EMAIL_NEW
        assert history[0]["changed_by_session_id"] == session_id

    def test_an_address_owned_by_another_account_is_refused(self, user_id):
        other_id = db.add_user_by_email(_EMAIL_OTHER)
        try:
            assert db.change_user_email(user_id, _EMAIL_OTHER) is False
            assert db.get_user_email(user_id) == _EMAIL
            assert db.get_user_email(other_id) == _EMAIL_OTHER
        finally:
            _exec("DELETE FROM users WHERE email=%s", (_EMAIL_OTHER,))


class TestAuthAudit:
    def test_events_round_trip_without_the_raw_ip(self, user_id):
        assert db.record_auth_event(db.AuthAuditEvent.LOGIN_SUCCESS, user_id=user_id,
                                    email=_EMAIL, ip="203.0.113.7", user_agent="UA",
                                    details={"method": "email_pin"}) is True
        assert db.record_auth_event(db.AuthAuditEvent.LOGIN_FAILED, user_id=user_id,
                                    email=_EMAIL, success=False) is True

        events = db.get_auth_audit_events(user_id)
        assert [e["event"] for e in events] == ["login_failed", "login_success"]
        assert events[0]["success"] == 0

        stored = _exec("SELECT ip_hash FROM auth_audit_log WHERE user_id=%s AND ip_hash IS NOT NULL",
                       (user_id,), fetch=True)
        assert stored and stored[0]["ip_hash"] != "203.0.113.7"

    def test_a_failed_login_for_an_unknown_email_still_records(self, user_id):
        assert db.record_auth_event(db.AuthAuditEvent.LOGIN_FAILED,
                                    email="stranger@example.test", success=False) is True
        rows = _exec("SELECT user_id FROM auth_audit_log WHERE email=%s",
                     ("stranger@example.test",), fetch=True)
        try:
            assert rows and rows[0]["user_id"] is None
        finally:
            _exec("DELETE FROM auth_audit_log WHERE email=%s", ("stranger@example.test",))
