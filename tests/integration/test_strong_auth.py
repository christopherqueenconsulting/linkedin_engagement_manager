"""Issue #745 (2c): strong factors, recovery codes and the step-up state against a live MySQL.

Unit tests mock the cursor, so they prove the Python and not the schema. These prove what only a
real server can answer: the 2c migration's tables and columns exist, a TOTP seed is an encrypted
envelope at rest rather than a scannable secret in a dump, a recovery code and a WebAuthn challenge
can each be spent exactly once, and the step-up stamp survives a round trip.

The last class is the **final at-rest sweep** the issue asks for across 2a/2b/2c: it drives the real
write paths for every secret LEM holds and then reads the raw columns back to assert that not one
of them contains the plaintext it was given.
"""

import time
from datetime import datetime, timedelta, timezone

import mysql.connector
import pyotp
import pytest

from cqc_lem.utilities import auth_factors as af
from cqc_lem.utilities import db
from cqc_lem.utilities.crypto import SECRET_ENVELOPE_PREFIX, hash_session_token

pytestmark = pytest.mark.integration

_EMAIL = "strong-auth-745-2c@example.test"
_LI_AT = "AQEDATestCookieValue0123456789abcdef"
_ACCESS_TOKEN = "li-access-token-0123456789"
_REFRESH_TOKEN = "li-refresh-token-0123456789"
_PASSWORD = "correct-horse-battery-staple"


def _schema_available() -> bool:
    """A reachable server is not enough — these need the 2c migration. An un-migrated DB skips."""
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        for table in ("user_auth_factors", "user_recovery_codes", "auth_challenges"):
            cursor.execute("SHOW TABLES LIKE %s", (table,))
            if not cursor.fetchone():
                return False
        cursor.execute("SHOW COLUMNS FROM sessions LIKE 'last_verified_at'")
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
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    _exec("DELETE FROM auth_audit_log WHERE email=%s", (_EMAIL,))


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the #745 2c integration test")
    _cleanup()
    uid = db.add_user_by_email(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _cleanup()


@pytest.fixture
def encryption_key(monkeypatch):
    """A master key for the whole test, so the TOTP seed takes the ENCRYPTED path. Without one the
    module is deliberately a pass-through (pre-#745 behaviour) and would prove nothing here.
    """
    from cqc_lem.utilities.crypto import generate_master_key
    monkeypatch.setenv("LEM_SECRET_KEY", generate_master_key())
    monkeypatch.setenv("LEM_SECRET_KEY_VERSION", "1")
    monkeypatch.delenv("LEM_SECRET_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("ENCRYPTION_REQUIRED", raising=False)


class TestPasskeyStorage:
    def test_a_passkey_round_trips_and_resolves_its_owner(self, user_id):
        factor_id = db.add_passkey_factor(user_id, "cred-abc", "public-key-bytes",
                                          sign_count=3, label="iPhone")
        assert factor_id

        stored = db.get_passkey_by_credential_id("cred-abc")
        assert stored["user_id"] == user_id
        assert stored["public_key"] == "public-key-bytes"
        assert stored["sign_count"] == 3
        assert db.get_user_passkey_credential_ids(user_id) == ["cred-abc"]
        assert db.count_auth_factors(user_id) == 1

    def test_the_same_credential_cannot_be_enrolled_twice(self, user_id):
        assert db.add_passkey_factor(user_id, "cred-dup", "pk")
        assert db.add_passkey_factor(user_id, "cred-dup", "pk") is None

    def test_the_counter_is_persisted_so_a_regression_can_be_detected(self, user_id):
        factor_id = db.add_passkey_factor(user_id, "cred-count", "pk", sign_count=1)
        assert db.update_factor_counter(factor_id, 9) is True
        assert db.get_passkey_by_credential_id("cred-count")["sign_count"] == 9

    def test_removing_a_factor_is_scoped_to_its_owner(self, user_id):
        factor_id = db.add_passkey_factor(user_id, "cred-scoped", "pk")
        assert db.delete_auth_factor(user_id + 100_000, factor_id) is False
        assert db.delete_auth_factor(user_id, factor_id) is True
        assert db.count_auth_factors(user_id) == 0


class TestTotpAtRest:
    def test_the_seed_is_an_envelope_in_the_column_and_a_secret_only_in_memory(
            self, user_id, encryption_key):
        secret = pyotp.random_base32()
        factor_id = db.upsert_totp_factor(user_id, secret)
        assert factor_id

        raw = _exec("SELECT secret FROM user_auth_factors WHERE id=%s", (factor_id,),
                    fetch=True)[0]["secret"]
        assert raw.startswith(f"{SECRET_ENVELOPE_PREFIX}:")
        assert secret not in raw

        assert db.get_totp_factor(user_id, confirmed_only=False)["secret"] == secret

    def test_an_unconfirmed_seed_cannot_gate_a_login(self, user_id, encryption_key):
        secret = pyotp.random_base32()
        db.upsert_totp_factor(user_id, secret)
        assert db.get_totp_factor(user_id) is None
        assert db.count_auth_factors(user_id) == 0

        assert af.confirm_totp_enrollment(user_id, pyotp.TOTP(secret).now()) is True
        assert db.count_auth_factors(user_id) == 1

    def test_restarting_enrolment_replaces_the_dead_seed(self, user_id, encryption_key):
        db.upsert_totp_factor(user_id, pyotp.random_base32())
        second = pyotp.random_base32()
        db.upsert_totp_factor(user_id, second)
        rows = _exec("SELECT id FROM user_auth_factors WHERE user_id=%s AND kind='totp'",
                     (user_id,), fetch=True)
        assert len(rows) == 1
        assert db.get_totp_factor(user_id, confirmed_only=False)["secret"] == second

    def test_a_code_cannot_be_used_twice_across_the_drift_window(self, user_id, encryption_key):
        secret = pyotp.random_base32()
        db.upsert_totp_factor(user_id, secret)
        assert af.confirm_totp_enrollment(user_id, pyotp.TOTP(secret).now()) is True

        # The confirmation already spent this step, so the same code must not verify again.
        assert af.verify_totp_code(user_id, pyotp.TOTP(secret).now()) is False
        # ...and a code from the FUTURE step does, which is what proves the guard is the counter
        # and not a blanket refusal.
        future = pyotp.TOTP(secret).at(int(time.time()) + 30)
        assert af.verify_totp_code(user_id, future) is True


class TestRecoveryCodes:
    def test_codes_are_hashed_at_rest_and_spent_exactly_once(self, user_id):
        codes = af.generate_recovery_codes(user_id)
        assert len(codes) == af.RECOVERY_CODE_COUNT

        stored = _exec("SELECT code_hash FROM user_recovery_codes WHERE user_id=%s",
                       (user_id,), fetch=True)
        assert len(stored) == len(codes)
        blob = " ".join(row["code_hash"] for row in stored)
        assert all(code not in blob for code in codes)
        assert all(row["code_hash"].startswith("$argon2") for row in stored)

        assert af.verify_recovery_code(user_id, codes[0]) is True
        assert af.verify_recovery_code(user_id, codes[0]) is False
        assert db.count_recovery_codes(user_id) == (len(codes) - 1, len(codes))

    def test_regenerating_invalidates_the_previous_sheet(self, user_id):
        old = af.generate_recovery_codes(user_id)
        af.generate_recovery_codes(user_id)
        assert af.verify_recovery_code(user_id, old[0]) is False


class TestChallenges:
    def test_a_challenge_is_claimed_exactly_once(self, user_id):
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        handle = db.create_auth_challenge("webauthn_login", expires, user_id=user_id,
                                          challenge="chal")
        assert handle

        row = _exec("SELECT handle_hash FROM auth_challenges WHERE handle_hash=%s",
                    (hash_session_token(handle),), fetch=True)
        assert row and row[0]["handle_hash"] != handle

        claimed = db.consume_auth_challenge(handle, "webauthn_login")
        assert claimed["challenge"] == "chal"
        assert claimed["user_id"] == user_id
        # A replayed assertion finds nothing to verify against.
        assert db.consume_auth_challenge(handle, "webauthn_login") is None

    def test_a_challenge_cannot_be_used_for_a_different_purpose(self, user_id):
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id)
        assert db.consume_auth_challenge(handle, "webauthn_step_up") is None
        assert db.consume_auth_challenge(handle, "second_factor") is not None

    def test_an_expired_challenge_is_refused(self, user_id):
        expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id)
        assert db.consume_auth_challenge(handle, "second_factor") is None

    def test_a_pending_login_survives_a_wrong_code_and_dies_on_the_last_one(self, user_id):
        """A mistyped digit must not end the login — the only way back would be the whole email
        round trip. The count is what bounds guessing, and it lives here rather than in the Redis
        limiter in front of it, which fails open.
        """
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id)

        first = db.claim_auth_challenge_attempt(handle, "second_factor", 3)
        assert first["attempts"] == 1 and first["user_id"] == user_id
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 3)["attempts"] == 2
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 3)["attempts"] == 3
        # The third attempt burned it, so a fourth guess has nothing to guess against.
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 3) is None

    def test_a_correct_code_finishes_the_handle_for_good(self, user_id):
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id)
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 5) is not None
        assert db.finish_auth_challenge(handle) is True
        assert db.finish_auth_challenge(handle) is False
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 5) is None

    def test_an_expired_pending_login_cannot_be_attempted(self, user_id):
        expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id)
        assert db.claim_auth_challenge_attempt(handle, "second_factor", 5) is None

    def test_the_guessing_budget_is_counted_per_account_not_per_handle(self, user_id):
        """A per-handle counter bounds one pending login and nothing else: starting the sign-in
        again issues a fresh handle with a fresh counter. Summing the account's attempts over a
        window is what turns five guesses per round into five guesses, full stop.
        """
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        window_start = datetime.now(timezone.utc) - timedelta(minutes=15)

        first = db.create_auth_challenge("second_factor", expires, user_id=user_id)
        db.claim_auth_challenge_attempt(first, "second_factor", 5)
        db.claim_auth_challenge_attempt(first, "second_factor", 5)
        assert db.count_challenge_attempts(user_id, "second_factor", window_start) == 2

        # Starting over carries the spend in, so the new handle has three guesses left, not five.
        second = db.create_auth_challenge("second_factor", expires, user_id=user_id,
                                          initial_attempts=2)
        assert db.claim_auth_challenge_attempt(second, "second_factor", 5)["attempts"] == 3
        assert db.count_challenge_attempts(user_id, "second_factor", window_start) == 5

    def test_a_purpose_and_an_owner_are_both_scoped(self, user_id):
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        window_start = datetime.now(timezone.utc) - timedelta(minutes=15)
        other = db.create_auth_challenge("webauthn_step_up", expires, user_id=user_id,
                                         initial_attempts=4)
        assert other
        assert db.count_challenge_attempts(user_id, "second_factor", window_start) == 0
        assert db.count_challenge_attempts(user_id + 9999, "webauthn_step_up", window_start) == 0

    def test_a_correct_code_clears_the_account_budget(self, user_id):
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        window_start = datetime.now(timezone.utc) - timedelta(minutes=15)
        handle = db.create_auth_challenge("second_factor", expires, user_id=user_id,
                                          initial_attempts=3)
        db.claim_auth_challenge_attempt(handle, "second_factor", 5)
        assert db.count_challenge_attempts(user_id, "second_factor", window_start) == 4
        assert db.clear_challenge_attempts(user_id, "second_factor") is True
        assert db.count_challenge_attempts(user_id, "second_factor", window_start) == 0


class TestStepUpState:
    def test_a_fresh_session_is_not_verified_until_a_factor_says_so(self, user_id):
        token = db.create_session(user_id)
        state = db.get_session_auth_state(token)
        assert state["last_verified_at"] is None
        assert state["scope"] == db.SESSION_SCOPE_FULL

        db.add_passkey_factor(user_id, "cred-step", "pk")
        assert af.step_up_satisfied(user_id, token) is False

        assert db.mark_session_verified(token) is True
        assert af.step_up_satisfied(user_id, token) is True

    def test_a_passkey_login_mints_a_session_already_verified(self, user_id):
        token = db.create_session(user_id, verified=True)
        assert db.get_session_auth_state(token)["last_verified_at"] is not None

    def test_the_extension_scope_survives_the_round_trip(self, user_id):
        token = db.create_session(user_id, label="LinkedIn Connect extension",
                                  scope=db.SESSION_SCOPE_EXTENSION)
        db.add_passkey_factor(user_id, "cred-ext", "pk")
        assert db.get_session_auth_state(token)["scope"] == db.SESSION_SCOPE_EXTENSION
        assert af.step_up_satisfied(user_id, token, extension_scope_ok=True) is True

    def test_a_recovery_session_may_enrol_but_still_cannot_touch_a_credential(self, user_id):
        """Both halves of the recovery-code bargain, against the real schema: it gets you back to
        a factor (design §6.8) and it gets you no further.
        """
        db.add_passkey_factor(user_id, "cred-lost", "pk")
        token = db.create_session(user_id, scope=db.SESSION_SCOPE_RECOVERY)
        assert af.session_signed_in_with_recovery_code(token) is True
        assert af.enrollment_allowed(user_id, token) is True
        assert af.step_up_satisfied(user_id, token) is False
        # ...and nowhere else: an extension token is otherwise an ordinary session.
        assert af.step_up_satisfied(user_id, token) is False

    def test_a_revoked_session_can_neither_be_verified_nor_read(self, user_id):
        token = db.create_session(user_id)
        db.revoke_session(user_id, db.get_session_id(token))
        assert db.mark_session_verified(token) is False
        assert db.get_session_auth_state(token) is None


class TestNoPlaintextSecretsRemain:
    """The acceptance criterion that spans all three PRs: drive every secret through its real write
    path, then read the raw columns back and assert none of them holds what was handed in.
    """

    def test_every_secret_column_holds_an_envelope_or_a_hash(self, user_id, encryption_key):
        session_token = db.create_session(user_id)
        db.store_linkedin_li_at(user_id, _LI_AT)
        db.update_user_linkedin_token(user_id, "li-sub-2c", _ACCESS_TOKEN, 3600,
                                      refresh_token=_REFRESH_TOKEN, refresh_token_expires_in=3600)
        db.update_user_linkedin_password(user_id, _PASSWORD)
        totp_secret = pyotp.random_base32()
        db.upsert_totp_factor(user_id, totp_secret)
        recovery_codes = af.generate_recovery_codes(user_id)

        user_row = _exec("SELECT access_token, refresh_token, password FROM users WHERE id=%s",
                         (user_id,), fetch=True)[0]
        cookie_rows = _exec("SELECT value FROM cookies WHERE user_id=%s", (user_id,), fetch=True)
        session_rows = _exec("SELECT session_token FROM sessions WHERE user_id=%s",
                             (user_id,), fetch=True)
        factor_rows = _exec("SELECT secret FROM user_auth_factors WHERE user_id=%s",
                            (user_id,), fetch=True)
        recovery_rows = _exec("SELECT code_hash FROM user_recovery_codes WHERE user_id=%s",
                              (user_id,), fetch=True)

        # 2a — the LinkedIn secrets are envelopes, never the value handed in.
        for column, plaintext in (("access_token", _ACCESS_TOKEN),
                                  ("refresh_token", _REFRESH_TOKEN),
                                  ("password", _PASSWORD)):
            assert user_row[column].startswith(f"{SECRET_ENVELOPE_PREFIX}:"), column
            assert plaintext not in user_row[column], column
        assert cookie_rows, "the li_at write stored nothing"
        for row in cookie_rows:
            assert _LI_AT not in (row["value"] or "")

        # 2b — the session token is a hash, and the plaintext still authenticates.
        assert session_rows and session_rows[0]["session_token"] == hash_session_token(session_token)
        assert session_rows[0]["session_token"] != session_token
        assert db.get_session_user_id(session_token) == user_id

        # 2c — the TOTP seed is an envelope and the recovery codes are argon2 hashes.
        assert factor_rows and factor_rows[0]["secret"].startswith(f"{SECRET_ENVELOPE_PREFIX}:")
        assert totp_secret not in factor_rows[0]["secret"]
        assert recovery_rows
        blob = " ".join(row["code_hash"] for row in recovery_rows)
        assert all(code not in blob for code in recovery_codes)

        # And every one of them still reads back correctly through db.py.
        assert db.get_user_password_pair_by_id(user_id)[1] == _PASSWORD
        assert db.get_totp_factor(user_id, confirmed_only=False)["secret"] == totp_secret
