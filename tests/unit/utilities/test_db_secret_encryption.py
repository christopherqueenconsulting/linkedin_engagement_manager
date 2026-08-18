"""db.py is the ONE choke point where LinkedIn secrets are sealed and unsealed (issue #745, PR 2a).

These tests assert on what actually reaches MySQL — a write must hand the driver an envelope, never
the plaintext — and that every read unseals it again, so the ten consuming modules stay unchanged.
"""

import base64
from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.utilities.crypto import decrypt_secret, encrypt_secret, is_encrypted
from cqc_lem.utilities.db import (
    SECRET_FIELD_ACCESS_TOKEN,
    SECRET_FIELD_COOKIE_VALUE,
    SECRET_FIELD_PASSWORD,
    SECRET_FIELD_REFRESH_TOKEN,
    add_user,
    add_user_with_access_token,
    clear_user_linkedin_password,
    encrypt_secrets_at_rest,
    get_cookies,
    get_user_access_token,
    get_user_password_pair_by_id,
    get_user_token_info,
    has_linkedin_password,
    store_cookies,
    update_user_access_token,
    update_user_linkedin_password,
    update_user_linkedin_token,
)

pytestmark = pytest.mark.unit

KEY = base64.urlsafe_b64encode(b"K" * 32).decode()
LI_AT = "AQEDATestSessionCookieValue1234567890"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("LEM_SECRET_KEY", KEY)
    monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "1")
    monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("ENCRYPTION_REQUIRED", raising=False)


def _executed(cursor):
    """Every (sql, params) pair handed to the driver."""
    return [(c[0][0], c[0][1] if len(c[0]) > 1 else ()) for c in cursor.execute.call_args_list]


class TestCookieWrites:
    def test_cookie_value_is_encrypted_before_it_reaches_mysql(self, keyed,
                                                               mock_database_connection):
        cur = mock_database_connection["cursor"]
        cookie = {"name": "li_at", "value": LI_AT, "domain": ".linkedin.com", "path": "/",
                  "expiry": 123, "secure": True, "httpOnly": True}
        with patch("cqc_lem.platform.db.repositories.users.get_user_id", return_value=42), \
             patch("cqc_lem.platform.db.repositories.users.prune_superseded_cookies"):
            store_cookies("a@b.com", [cookie])

        stored = next(p[1] for s, p in _executed(cur) if "INSERT INTO cookies" in s)
        assert stored != LI_AT and is_encrypted(stored)
        assert LI_AT not in str(_executed(cur))
        assert decrypt_secret(stored, 42, SECRET_FIELD_COOKIE_VALUE) == LI_AT

    def test_unbindable_cookie_is_refused_not_written_in_the_clear(self, keyed,
                                                                   mock_database_connection):
        """No user_id means no AAD to bind to, so the row would be written as PLAINTEXT — and
        get_cookies JOINs users, so nothing could ever read it back. A dead plaintext li_at that
        the backfill can't see is strictly worse than no row at all.
        """
        cur = mock_database_connection["cursor"]
        cookie = {"name": "li_at", "value": LI_AT, "domain": ".linkedin.com", "path": "/",
                  "expiry": 123, "secure": True, "httpOnly": True}
        with patch("cqc_lem.platform.db.repositories.users.get_user_id", return_value=None):
            ok = store_cookies("nobody@example.com", [cookie])
        assert ok is False
        assert not any("INSERT INTO cookies" in s for s, _ in _executed(cur))
        assert LI_AT not in str(_executed(cur))

    def test_failed_write_is_reported_so_the_password_is_never_dropped(
            self, keyed, mock_database_connection):
        """store_linkedin_li_at's True is what authorises deleting the user's stored LinkedIn
        password. _store_cookie_rows swallows per-row driver errors, so the failure has to travel
        back out — otherwise a user loses the only login they had left.
        """
        from cqc_lem.utilities.db import store_linkedin_li_at

        cur = mock_database_connection["cursor"]
        cur.execute.side_effect = mysql.connector.Error("cookie write failed")
        with patch("cqc_lem.platform.db.repositories.users.get_user_id", return_value=42), \
             patch("cqc_lem.platform.db.repositories.users.get_user_email", return_value="a@b.com"), \
             patch("cqc_lem.platform.db.repositories.users.prune_superseded_cookies"):
            assert store_linkedin_li_at(42, LI_AT) is False


class TestCookieReads:
    def _row(self, value, user_id=42):
        return {"name": "li_at", "value": value, "domain": ".linkedin.com", "path": "/",
                "expiry": 1, "secure": 1, "http_only": 1, "user_id": user_id}

    def test_encrypted_cookie_is_decrypted_and_user_id_stripped(self, keyed,
                                                                mock_database_connection):
        sealed = encrypt_secret(LI_AT, 42, SECRET_FIELD_COOKIE_VALUE)
        mock_database_connection["cursor"].fetchall.return_value = [self._row(sealed)]
        cookies = get_cookies("https://www.linkedin.com/feed", "a@b.com")
        assert cookies[0]["value"] == LI_AT
        # Selenium's add_cookie() rejects unknown keys — user_id must not survive the read.
        assert "user_id" not in cookies[0]

    def test_legacy_plaintext_cookie_still_loads(self, keyed, mock_database_connection):
        mock_database_connection["cursor"].fetchall.return_value = [self._row(LI_AT)]
        assert get_cookies("https://www.linkedin.com/feed", "a@b.com")[0]["value"] == LI_AT

    def test_undecryptable_cookie_is_dropped_not_returned_empty(self, keyed,
                                                                mock_database_connection):
        """An empty li_at would make LEM report a live session against a dead one."""
        sealed = encrypt_secret(LI_AT, 42, SECRET_FIELD_COOKIE_VALUE)
        mock_database_connection["cursor"].fetchall.return_value = [self._row(sealed, user_id=99)]
        assert get_cookies("https://www.linkedin.com/feed", "a@b.com") == []


class TestPassword:
    def test_password_is_encrypted_on_write(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        assert update_user_linkedin_password(5, "hunter2-linkedin") is True
        params = cur.execute.call_args[0][1]
        assert is_encrypted(params[0])
        assert decrypt_secret(params[0], 5, SECRET_FIELD_PASSWORD) == "hunter2-linkedin"

    def test_password_is_decrypted_on_read(self, keyed, mock_database_connection):
        sealed = encrypt_secret("hunter2-linkedin", 5, SECRET_FIELD_PASSWORD)
        mock_database_connection["cursor"].fetchone.return_value = {"email": "a@b.com",
                                                                   "password": sealed}
        assert get_user_password_pair_by_id(5) == ("a@b.com", "hunter2-linkedin")

    def test_password_round_trips_through_the_db_layer(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        update_user_linkedin_password(5, "round-trip")
        cur.fetchone.return_value = {"email": "a@b.com", "password": cur.execute.call_args[0][1][0]}
        assert get_user_password_pair_by_id(5)[1] == "round-trip"

    def test_add_user_writes_the_row_before_sealing_the_password(self, keyed,
                                                                 mock_database_connection):
        """The AAD is bound to users.id, which only exists after the INSERT."""
        cur = mock_database_connection["cursor"]
        cur.lastrowid = 11
        add_user("new@example.com", "plain-pw")
        statements = _executed(cur)
        assert "INSERT INTO users (email) VALUES" in statements[0][0]
        sealed = statements[1][1][0]
        assert decrypt_secret(sealed, 11, SECRET_FIELD_PASSWORD) == "plain-pw"

    def test_clear_password_nulls_the_column(self, mock_database_connection):
        assert clear_user_linkedin_password(5) is True
        sql, params = mock_database_connection["cursor"].execute.call_args[0][:2]
        assert "password = NULL" in sql and params == (5,)

    def test_clear_password_survives_a_db_error(self, mock_database_connection):
        mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
        assert clear_user_linkedin_password(5) is False

    def test_has_password_is_a_presence_check_not_a_decrypt(self, keyed,
                                                            mock_database_connection):
        """An undecryptable row must still read as 'has a password' — otherwise a broken key
        silently flips a required readiness item to not-ready.
        """
        cur = mock_database_connection["cursor"]
        cur.fetchone.return_value = (1,)
        assert has_linkedin_password(5) is True
        assert "password IS NOT NULL" in cur.execute.call_args[0][0]
        cur.fetchone.return_value = None
        assert has_linkedin_password(5) is False

    def test_has_password_survives_a_db_error(self, mock_database_connection):
        mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
        assert has_linkedin_password(5) is False


class TestOAuthTokens:
    def test_access_token_is_encrypted_on_write_and_decrypted_on_read(
            self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        update_user_access_token(9, "oauth-access", 3600, refresh_token="oauth-refresh",
                                 refresh_token_expires_in=99)
        params = cur.execute.call_args[0][1]
        assert decrypt_secret(params[0], 9, SECRET_FIELD_ACCESS_TOKEN) == "oauth-access"
        assert decrypt_secret(params[3], 9, SECRET_FIELD_REFRESH_TOKEN) == "oauth-refresh"

        cur.fetchone.return_value = {"access_token": params[0]}
        assert get_user_access_token(9) == "oauth-access"

    def test_access_token_only_write_still_seals(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        update_user_access_token(9, "oauth-access", 3600)
        params = cur.execute.call_args[0][1]
        assert decrypt_secret(params[0], 9, SECRET_FIELD_ACCESS_TOKEN) == "oauth-access"

    def test_token_info_decrypts_both_tokens(self, keyed, mock_database_connection):
        mock_database_connection["cursor"].fetchone.return_value = {
            "access_token": encrypt_secret("a-tok", 9, SECRET_FIELD_ACCESS_TOKEN),
            "refresh_token": encrypt_secret("r-tok", 9, SECRET_FIELD_REFRESH_TOKEN),
        }
        info = get_user_token_info(9)
        assert info["access_token"] == "a-tok" and info["refresh_token"] == "r-tok"

    def test_token_info_handles_no_row(self, keyed, mock_database_connection):
        mock_database_connection["cursor"].fetchone.return_value = None
        assert get_user_token_info(9) is None

    def test_linkedin_token_write_seals_both(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        update_user_linkedin_token(9, "sub", "a-tok", 3600, refresh_token="r-tok",
                                   refresh_token_expires_in=99)
        params = cur.execute.call_args[0][1]
        assert decrypt_secret(params[2], 9, SECRET_FIELD_ACCESS_TOKEN) == "a-tok"
        assert decrypt_secret(params[5], 9, SECRET_FIELD_REFRESH_TOKEN) == "r-tok"

    def test_linkedin_token_write_without_refresh_seals_access(self, keyed,
                                                               mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 1
        update_user_linkedin_token(9, "sub", "a-tok", 3600)
        params = cur.execute.call_args[0][1]
        assert decrypt_secret(params[2], 9, SECRET_FIELD_ACCESS_TOKEN) == "a-tok"

    def test_new_oauth_user_is_inserted_then_sealed_against_its_id(self, keyed,
                                                                   mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.lastrowid = 77
        add_user_with_access_token("new@example.com", "sub", "a-tok", 3600,
                                   refresh_token="r-tok", refresh_token_expires_in=99)
        insert_sql = _executed(cur)[0][0]
        assert "access_token" not in insert_sql  # tokens can't be sealed before the id exists
        update_sql, params = _executed(cur)[1]
        assert "UPDATE users SET" in update_sql
        assert decrypt_secret(params[0], 77, SECRET_FIELD_ACCESS_TOKEN) == "a-tok"
        assert decrypt_secret(params[3], 77, SECRET_FIELD_REFRESH_TOKEN) == "r-tok"

    def test_upsert_pins_the_existing_row_id_for_the_duplicate_branch(
            self, keyed, mock_database_connection):
        """Without `id = LAST_INSERT_ID(id)`, MySQL's lastrowid on the ON DUPLICATE KEY UPDATE
        branch can be the auto-increment value burned by the failed insert — a row that does not
        exist. The tokens would then be sealed against that id and the UPDATE would touch nothing,
        silently leaving the reconnecting user with no OAuth token at all.
        """
        cur = mock_database_connection["cursor"]
        cur.lastrowid = 5
        add_user_with_access_token("existing@example.com", "sub", "a-tok", 3600)
        insert_sql = _executed(cur)[0][0]
        assert "ON DUPLICATE KEY UPDATE" in insert_sql
        assert "id = LAST_INSERT_ID(id)" in insert_sql

    def test_existing_oauth_user_resolves_its_id_by_email(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.lastrowid = 0  # MySQL returns 0 for the ON DUPLICATE KEY UPDATE branch
        cur.fetchone.return_value = (5,)
        add_user_with_access_token("existing@example.com", "sub", "a-tok", 3600)
        update_sql, params = _executed(cur)[-1]
        assert "UPDATE users SET" in update_sql
        assert decrypt_secret(params[0], 5, SECRET_FIELD_ACCESS_TOKEN) == "a-tok"


class TestBackfillAndRotation:
    def _rows(self, cursor, users, cookies, orphans=0):
        cursor.fetchall.side_effect = [users, cookies]
        cursor.fetchone.return_value = {"n": orphans}

    def test_disabled_without_a_key(self, mock_database_connection, monkeypatch):
        monkeypatch.delenv("LEM_SECRET_KEY", raising=False)
        stats = encrypt_secrets_at_rest()
        assert stats["enabled"] is False and stats["rewritten"] == 0

    def test_plaintext_rows_are_encrypted(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        self._rows(cur,
                   [{"id": 1, "password": "pw", "access_token": "at", "refresh_token": None}],
                   [{"id": 3, "user_id": 1, "value": LI_AT}])
        stats = encrypt_secrets_at_rest()
        assert stats["rewritten"] == 3 and stats["failed"] == 0
        assert stats["plaintext_remaining"] == 0

        writes = [c[0][1] for c in cur.execute.call_args_list if "UPDATE" in c[0][0]]
        assert all(is_encrypted(p[0]) for p in writes)
        assert decrypt_secret(writes[0][0], 1, SECRET_FIELD_PASSWORD) == "pw"
        assert decrypt_secret(writes[2][0], 1, SECRET_FIELD_COOKIE_VALUE) == LI_AT

    def test_second_run_is_a_no_op(self, keyed, mock_database_connection):
        """Idempotency is what makes a daily schedule safe."""
        cur = mock_database_connection["cursor"]
        self._rows(cur, [{"id": 1,
                          "password": encrypt_secret("pw", 1, SECRET_FIELD_PASSWORD),
                          "access_token": None, "refresh_token": None}], [])
        stats = encrypt_secrets_at_rest()
        assert stats["scanned"] == 0 and stats["rewritten"] == 0

    def test_rotation_reseals_under_the_new_key(self, keyed, mock_database_connection,
                                                monkeypatch):
        old = encrypt_secret("pw", 1, SECRET_FIELD_PASSWORD)
        monkeypatch.setenv("LEM_SECRET_KEY", base64.urlsafe_b64encode(b"N" * 32).decode())
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "2")
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS", KEY)
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS_VERSION", "1")

        cur = mock_database_connection["cursor"]
        self._rows(cur, [{"id": 1, "password": old, "access_token": None,
                          "refresh_token": None}], [])
        stats = encrypt_secrets_at_rest()
        assert stats["rewritten"] == 1
        new_value = cur.execute.call_args[0][1][0]
        assert new_value.startswith("lemv1:2:")
        assert decrypt_secret(new_value, 1, SECRET_FIELD_PASSWORD) == "pw"

    def test_undecryptable_row_is_counted_not_destroyed(self, keyed, mock_database_connection,
                                                        monkeypatch):
        """Overwriting it would burn a secret a corrected key could still recover. Here the row is
        due for rotation (sealed under v1) but bound to the wrong user, so it cannot be unsealed.
        """
        rows = [{"id": 1, "password": encrypt_secret("pw", 999, SECRET_FIELD_PASSWORD),
                 "access_token": None, "refresh_token": None}]
        monkeypatch.setenv("LEM_SECRET_KEY", base64.urlsafe_b64encode(b"N" * 32).decode())
        monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "2")
        monkeypatch.setenv("LEM_SECRET_KEY_PREVIOUS", KEY)
        cur = mock_database_connection["cursor"]
        self._rows(cur, rows, [])
        stats = encrypt_secrets_at_rest()
        assert stats["failed"] == 1 and stats["plaintext_remaining"] == 1
        assert stats["rewritten"] == 0
        assert not any("UPDATE" in c[0][0] for c in cur.execute.call_args_list)

    def test_limit_caps_the_work_and_reports_the_remainder(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        self._rows(cur, [{"id": 1, "password": "pw", "access_token": "at",
                          "refresh_token": "rt"}], [])
        stats = encrypt_secrets_at_rest(limit=1)
        assert stats["rewritten"] == 1 and stats["plaintext_remaining"] == 2

    def test_write_failure_is_counted_as_still_unprotected(self, keyed, mock_database_connection):
        cur = mock_database_connection["cursor"]
        self._rows(cur, [{"id": 1, "password": "pw", "access_token": None,
                          "refresh_token": None}], [])
        # users SELECT, cookies SELECT, orphan COUNT, then the UPDATE fails
        cur.execute.side_effect = [None, None, None, mysql.connector.Error("boom")]
        stats = encrypt_secrets_at_rest()
        assert stats["rewritten"] == 0 and stats["plaintext_remaining"] == 1

    def test_orphaned_plaintext_cookies_keep_the_gate_off_zero(self, keyed,
                                                               mock_database_connection):
        """A cookie row with no user_id can be neither encrypted nor read, but it IS a plaintext
        session in every dump. If it didn't count, the operator would read 'nothing unprotected'
        and flip ENCRYPTION_REQUIRED with live credentials still in the clear.
        """
        cur = mock_database_connection["cursor"]
        self._rows(cur, [], [], orphans=2)
        stats = encrypt_secrets_at_rest()
        assert stats["orphaned"] == 2
        assert stats["plaintext_remaining"] == 2
        # Reported, never rewritten — the remedy is deletion, not encryption.
        assert stats["rewritten"] == 0
        assert not any("UPDATE" in c[0][0] for c in cur.execute.call_args_list)

    def test_read_failure_returns_early(self, keyed, mock_database_connection):
        mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
        stats = encrypt_secrets_at_rest()
        assert stats["rewritten"] == 0 and stats["scanned"] == 0
