"""Strong authentication at the API seam (issue #745, phase 2c).

What these prove: the email PIN stops opening a session once a strong factor exists (on BOTH login
paths, including the no-mail-provider bypass), a passkey assertion is the one login that arrives
already stepped up, a recovery code signs you in without unlocking the LinkedIn credentials, and
every credential-touching endpoint refuses — with a 403 the SPA can react to, never a 401 that
would log the user out — until this session has proved a factor.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_USER = "cqc_lem.api.routers.user"
_UID = 11
_EMAIL = "user@example.com"
_TOKEN = "sess-token"


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


@pytest.fixture(autouse=True)
def _quiet():
    """Silence the audit write in EVERY module that binds it.

    A fixture names no route, so it covers every route in the file — and since #1154 those are
    served from three modules. The login-ceremony names below are patched on the auth router alone,
    because it is now the only module that binds them: the enrolment and step-up halves stayed with
    the `/api/user` router, which has its own challenge kinds.
    """
    with patch(f"{_M}.record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.mark_email_verified", return_value=True), \
         patch(f"{_AUTH}.finish_auth_challenge", return_value=True), \
         patch(f"{_AUTH}.count_challenge_attempts", return_value=0), \
         patch(f"{_AUTH}.clear_challenge_attempts", return_value=True), \
         patch(f"{_AUTH}.clear_auth_limits"):
        yield


class _Allowed:
    allowed = True
    scope = None
    retry_after_seconds = 3600


class _Blocked:
    allowed = False
    scope = "verify_email"
    retry_after_seconds = 900


# ---------------------------------------------------------------------------
# The email PIN is demoted to a bootstrap
# ---------------------------------------------------------------------------

class TestPinDemotion:
    def test_a_pin_still_signs_in_an_account_with_no_strong_factor(self, client):
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=False), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN):
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["session_token"] == _TOKEN

    def test_a_pin_alone_no_longer_signs_in_an_account_that_enrolled_one(self, client):
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["passkey", "totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle"), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        detail = resp.json()["detail"]
        assert resp.status_code == 200
        assert detail["second_factor_required"] is True
        assert detail["pending_token"] == "pending-handle"
        assert "session_token" not in detail
        create_session.assert_not_called()

    def test_the_mailbox_is_still_recorded_as_verified(self, client):
        """The PIN stops being a key but it is still proof the address was reached — 2b's
        `email_verified_at` has to keep meaning that.
        """
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle"), \
             patch(f"{_AUTH}.mark_email_verified") as verified:
            client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        verified.assert_called_once_with(_UID)

    def test_the_no_mail_provider_bypass_is_gated_too(self, client):
        """The weakest way in skips the PIN entirely — leaving it ungated would be a hole straight
        through 2c on any deployment with mail unconfigured.
        """
        with patch(f"{_AUTH}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["passkey"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle"), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/email/init", json={"email": _EMAIL})
        detail = resp.json()["detail"]
        assert detail["second_factor_required"] is True
        assert "session_token" not in detail
        create_session.assert_not_called()


# ---------------------------------------------------------------------------
# Finishing a bootstrapped login
# ---------------------------------------------------------------------------

class TestSecondFactor:
    def _pending(self, attempts: int = 1):
        return patch(f"{_AUTH}.claim_auth_challenge_attempt",
                     return_value={"user_id": _UID, "challenge": None, "attempts": attempts})

    def test_a_totp_code_mints_a_fully_verified_session(self, client):
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=True), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN) as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["verified"] is True
        assert create_session.call_args.kwargs["scope"] == "full"
        assert f"lem_session={_TOKEN}" in resp.headers.get("set-cookie", "")

    def test_a_recovery_code_signs_in_but_does_not_unlock_the_credentials(self, client):
        """Design §6.8: a recovery code gets you back in to enrol a factor. If it also stepped the
        session up, a found sheet of codes would be a stolen LinkedIn session.
        """
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_recovery_code", return_value=True), \
             patch(f"{_AUTH}.count_recovery_codes", return_value=(4, 10)), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN) as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "recovery_code",
                                     "code": "ABCD234XYZ"})
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["verified"] is False
        # Marked so it may enrol a REPLACEMENT factor without proving one it no longer holds —
        # and only that: the scope is not a step-up.
        assert create_session.call_args.kwargs["scope"] == "recovery"
        assert resp.json()["detail"]["enroll_factor_required"] is True

    def test_a_replayed_pending_token_is_refused(self, client):
        """The handle is claimed by an UPDATE with `consumed_at IS NULL`, so a spent one finds
        nothing — one bootstrapped login cannot become two sessions.
        """
        with patch(f"{_AUTH}.claim_auth_challenge_attempt", return_value=None), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 400
        create_session.assert_not_called()

    def test_a_wrong_code_creates_no_session(self, client):
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=False), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "000000"})
        assert resp.status_code == 401
        create_session.assert_not_called()

    def test_a_mistyped_code_does_not_end_the_sign_in(self, client):
        """The failure mode this replaced: consuming the handle on first touch meant one wrong
        digit killed the login, and the only way back was the whole email round trip again. 401
        (not 400) is what tells the SPA to leave the user on the code field.
        """
        with self._pending(attempts=1), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=False), \
             patch(f"{_AUTH}.finish_auth_challenge") as finished:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "000000"})
        assert resp.status_code == 401
        finished.assert_not_called()

    def test_the_last_allowed_attempt_burns_the_handle(self, client):
        """Guessing is bounded in MySQL, not only in Redis — the rate limiter in front of this
        fails open, so it cannot be the thing that stops a 6-digit space being walked.
        """
        with self._pending(attempts=5), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=False), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "000000"})
        assert resp.status_code == 400
        create_session.assert_not_called()

    def test_a_correct_code_spends_the_handle(self, client):
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=True), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN), \
             patch(f"{_AUTH}.finish_auth_challenge") as finished:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 200
        finished.assert_called_once_with("p")

    def test_an_unknown_method_is_not_a_way_in(self, client):
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "sms", "code": "1"})
        assert resp.status_code == 401
        create_session.assert_not_called()

    def test_the_second_factor_step_is_rate_limited(self, client):
        """A 6-digit TOTP space is walkable without a limiter, exactly like the PIN it replaced."""
        with self._pending(), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Blocked()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code") as verify:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 429
        verify.assert_not_called()


# ---------------------------------------------------------------------------
# Passkey login
# ---------------------------------------------------------------------------

class TestPasskeyLogin:
    def test_begin_takes_no_email_and_returns_options(self, client):
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.build_authentication_options", return_value=({"rpId": "x"}, "chal")), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="handle"):
            resp = client.post("/api/auth/passkey/login/begin", json={})
        assert resp.status_code == 200
        assert resp.json()["detail"]["handle"] == "handle"

    def test_a_verified_assertion_mints_a_session_already_stepped_up(self, client):
        """The one login path that is phishing-resistant end to end. Asking the user to prove the
        same factor again before they can paste a cookie would be theatre.
        """
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.consume_auth_challenge", return_value={"user_id": None,
                                                                 "challenge": "chal"}), \
             patch(f"{_M}.credential_id_from_response", return_value="cred"), \
             patch(f"{_M}.get_passkey_by_credential_id",
                   return_value={"id": 5, "user_id": _UID, "public_key": "pk", "sign_count": 3}), \
             patch(f"{_M}.verify_passkey_assertion", return_value=4), \
             patch(f"{_M}.update_factor_counter") as counter, \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN) as create_session:
            resp = client.post("/api/auth/passkey/login/complete",
                               json={"handle": "h", "credential": {"id": "cred"}})
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["verified"] is True
        counter.assert_called_once_with(5, 4)

    def test_an_unverifiable_assertion_is_a_401_and_no_session(self, client):
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.consume_auth_challenge", return_value={"user_id": None,
                                                                 "challenge": "chal"}), \
             patch(f"{_M}.credential_id_from_response", return_value="cred"), \
             patch(f"{_M}.get_passkey_by_credential_id",
                   return_value={"id": 5, "user_id": _UID, "public_key": "pk", "sign_count": 3}), \
             patch(f"{_M}.verify_passkey_assertion", return_value=None), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/passkey/login/complete",
                               json={"handle": "h", "credential": {"id": "cred"}})
        assert resp.status_code == 401
        create_session.assert_not_called()

    def test_an_unknown_credential_signs_nobody_in(self, client):
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.consume_auth_challenge", return_value={"user_id": None,
                                                                 "challenge": "chal"}), \
             patch(f"{_M}.credential_id_from_response", return_value="cred"), \
             patch(f"{_M}.get_passkey_by_credential_id", return_value=None), \
             patch(f"{_M}.verify_passkey_assertion") as verify, \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/passkey/login/complete",
                               json={"handle": "h", "credential": {"id": "cred"}})
        assert resp.status_code == 401
        verify.assert_not_called()
        create_session.assert_not_called()

    def test_a_deployment_without_a_secure_origin_says_503_not_500(self, client):
        from cqc_lem.utilities.webauthn_util import WebAuthnUnavailable
        with patch(f"{_M}.webauthn_relying_party",
                   side_effect=WebAuthnUnavailable("no public origin")):
            resp = client.post("/api/auth/passkey/login/begin", json={})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

class TestEnrollment:
    def test_the_first_factor_needs_a_session_but_not_step_up(self, client):
        """An account with nothing enrolled has nothing to prove with — gating the first factor
        would mean no account could ever get one.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.get_user_passkey_credential_ids", return_value=[]), \
             patch(f"{_USER}.build_registration_options", return_value=({"rp": {}}, "chal")), \
             patch(f"{_USER}.create_auth_challenge", return_value="handle"):
            resp = client.post("/api/user/passkeys/register/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 200

    @pytest.mark.parametrize("path,body", [
        ("/api/user/passkeys/register/begin", {"session_token": _TOKEN}),
        ("/api/user/passkeys/register/complete",
         {"session_token": _TOKEN, "handle": "h", "credential": {}}),
        ("/api/user/totp/enroll/begin", {"session_token": _TOKEN}),
        ("/api/user/totp/enroll/confirm", {"session_token": _TOKEN, "code": "123456"}),
    ])
    def test_adding_a_SECOND_factor_is_step_up_gated(self, client, path, body):
        """The escalation this closes: enrolling stamps the session as verified, so an ungated
        enrolment hands a stolen session a step-up it never proved — add your own passkey, then
        walk into the LinkedIn credentials with it (threat T4).
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.enrollment_allowed", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=["passkey"]), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge") as consumed, \
             patch(f"{_USER}.begin_totp_enrollment") as began, \
             patch(f"{_USER}.confirm_totp_enrollment") as confirmed:
            resp = client.post(path, json=body)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "step_up_required"
        consumed.assert_not_called()
        began.assert_not_called()
        confirmed.assert_not_called()

    def test_a_recovery_code_session_may_still_enrol_a_replacement(self, client):
        """The anti-lockout half (design §6.8): the one person who cannot prove a factor is the
        one who lost it, and a recovery code is how they get back to enrol another.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.enrollment_allowed", return_value=True), \
             patch(f"{_USER}.session_signed_in_with_recovery_code", return_value=True), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.get_user_passkey_credential_ids", return_value=["old"]), \
             patch(f"{_USER}.build_registration_options", return_value=({"rp": {}}, "chal")), \
             patch(f"{_USER}.create_auth_challenge", return_value="handle"):
            resp = client.post("/api/user/passkeys/register/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_a_recovery_code_session_is_not_stepped_up_by_enrolling(self, client):
        """Otherwise a found sheet of codes IS a LinkedIn session: sign in, enrol, get stamped,
        read the cookie. It runs the ordinary step-up with the new factor instead — one touch, and
        an audited one.
        """
        class _Result:
            credential_id = "cred"
            public_key = "pk"
            sign_count = 0

        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.session_signed_in_with_recovery_code", return_value=True), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration", return_value=_Result()), \
             patch(f"{_USER}.add_passkey_factor", return_value=7), \
             patch(f"{_USER}.count_recovery_codes", return_value=(2, 10)), \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h", "credential": {}})
        assert resp.status_code == 200
        stepped_up.assert_not_called()

    def test_a_second_authenticator_app_is_refused(self, client):
        """A second confirmed TOTP row would count towards has_strong_factor and show on the
        Security card while only the newer seed's codes were ever checked.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.has_confirmed_totp", return_value=True), \
             patch(f"{_USER}.begin_totp_enrollment") as began:
            resp = client.post("/api/user/totp/enroll/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 400
        began.assert_not_called()

    def test_a_registration_challenge_belonging_to_someone_else_is_refused(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": 999, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration") as verify:
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h", "credential": {}})
        assert resp.status_code == 400
        verify.assert_not_called()

    def test_a_verified_registration_steps_the_session_up(self, client):
        """The ceremony IS a fresh proof of possession — otherwise a user who just touched their
        sensor would be asked to touch it again to save recovery codes.
        """
        class _Result:
            credential_id = "cred"
            public_key = "pk"
            sign_count = 0

        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration", return_value=_Result()), \
             patch(f"{_USER}.add_passkey_factor", return_value=7), \
             patch(f"{_USER}.count_recovery_codes", return_value=(0, 0)), \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h", "credential": {}})
        assert resp.status_code == 200
        assert resp.json()["detail"]["recovery_codes_needed"] is True
        stepped_up.assert_called_once()

    def test_an_unverifiable_registration_stores_nothing(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration", return_value=None), \
             patch(f"{_USER}.add_passkey_factor") as store:
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h", "credential": {}})
        assert resp.status_code == 400
        store.assert_not_called()

    def test_totp_enrolment_returns_the_secret_exactly_once(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.begin_totp_enrollment", return_value=(9, "SEED", "otpauth://x")):
            resp = client.post("/api/user/totp/enroll/begin", json={"session_token": _TOKEN})
        assert resp.json()["detail"]["secret"] == "SEED"

    def test_a_wrong_totp_code_does_not_confirm_the_factor(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.confirm_totp_enrollment", return_value=False), \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/totp/enroll/confirm",
                               json={"session_token": _TOKEN, "code": "000000"})
        assert resp.status_code == 400
        stepped_up.assert_not_called()

    def test_removing_a_factor_is_step_up_gated(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=["passkey"]), \
             patch(f"{_USER}.delete_auth_factor") as delete:
            resp = client.post("/api/user/auth-factors/delete",
                               json={"session_token": _TOKEN, "factor_id": 4})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "step_up_required"
        delete.assert_not_called()

    def test_regenerating_recovery_codes_is_step_up_gated(self, client):
        """A new sheet silently invalidates the old one — an attacker could otherwise lock the real
        owner out of their own recovery path without touching a factor.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=["totp"]), \
             patch(f"{_USER}.generate_recovery_codes") as generate:
            resp = client.post("/api/user/recovery-codes/regenerate",
                               json={"session_token": _TOKEN})
        assert resp.status_code == 403
        generate.assert_not_called()

    def test_recovery_codes_are_returned_once_when_the_gate_passes(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.generate_recovery_codes", return_value=["AAAA111111", "BBBB222222"]):
            resp = client.post("/api/user/recovery-codes/regenerate",
                               json={"session_token": _TOKEN})
        assert resp.json()["detail"]["codes"] == ["AAAA111111", "BBBB222222"]


# ---------------------------------------------------------------------------
# The step-up gate on the endpoints that touch credentials
# ---------------------------------------------------------------------------

class TestStepUpGate:
    GATED = [
        ("post", "/api/user/linkedin-cookie", {"li_at": "a" * 40}),
        ("put", "/api/user/linkedin-password", {"linkedin_password": "hunter2"}),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
        ("post", "/api/user/extension-token", {}),
    ]

    @pytest.mark.parametrize("method,path,body", GATED)
    def test_every_credential_touching_endpoint_refuses_without_a_fresh_factor(
            self, client, method, path, body):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=["passkey", "totp"]), \
             patch(f"{_USER}.store_linkedin_li_at") as store_cookie, \
             patch(f"{_USER}.update_user_linkedin_password") as store_password, \
             patch(f"{_USER}.revoke_other_sessions") as revoke, \
             patch(f"{_USER}.create_session") as create_session, \
             patch(f"{_USER}.create_pin_for_email") as create_pin:
            resp = getattr(client, method)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == "step_up_required"
        assert detail["methods"] == ["passkey", "totp"]
        for should_not_run in (store_cookie, store_password, revoke, create_session, create_pin):
            should_not_run.assert_not_called()

    def test_the_refusal_is_403_not_401(self, client):
        """The SPA's axios interceptor treats ANY 401 as a dead session — it clears the cookie
        sentinel and redirects. Answering "prove it's you" with a 401 would log the user out
        instead of asking them anything.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=[]):
            resp = client.post("/api/user/linkedin-cookie",
                               json={"session_token": _TOKEN, "li_at": "a" * 40})
        assert resp.status_code == 403

    def test_a_fresh_factor_lets_the_write_through(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie",
                               json={"session_token": _TOKEN, "li_at": "a" * 40})
        assert resp.status_code == 200
        store.assert_called_once()

    def test_only_the_cookie_endpoint_accepts_the_extension_scope(self, client):
        """A stolen extension token must not be able to change the email address or revoke every
        device — the scope exemption is opt-in per call site, and only one site opts in.
        """
        seen: dict[str, bool] = {}

        def record(user_id, token, extension_scope_ok=False):
            seen[str(len(seen))] = extension_scope_ok
            return True

        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", side_effect=record), \
             patch(f"{_USER}.store_linkedin_li_at", return_value=True), \
             patch(f"{_USER}.revoke_other_sessions", return_value=0):
            client.post("/api/user/linkedin-cookie",
                        json={"session_token": _TOKEN, "li_at": "a" * 40})
            cookie_flag = seen["0"]
            client.post("/api/user/sessions/revoke",
                        json={"session_token": _TOKEN, "all_others": True})
            revoke_flag = seen["1"]
        assert cookie_flag is True
        assert revoke_flag is False

    def test_the_extension_token_is_minted_with_its_own_scope(self, client):
        from cqc_lem.utilities.db import SESSION_SCOPE_EXTENSION
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="ext-token") as create_session:
            resp = client.post("/api/user/extension-token", json={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["scope"] == SESSION_SCOPE_EXTENSION


class TestStepUpVerify:
    def test_a_totp_code_stamps_this_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_USER}.verify_totp_code", return_value=True), \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "totp", "code": "123456"})
        assert resp.status_code == 200
        stepped_up.assert_called_once()

    def test_a_recovery_code_never_steps_a_session_up(self, client):
        """It gets you back INTO the account; it must not by itself unlock the LinkedIn
        credentials, or a stolen recovery sheet is a stolen LinkedIn session.
        """
        # `create=True`, and that is the assertion. The step-up router does not import
        # `verify_recovery_code` at all — refusing the method by name is HOW §6.8 is enforced, and
        # #1154 moved the last binding of it out of `main` and into the LOGIN router. The tripwire
        # stays pointed at the module serving this route, so the day step-up acquires the import,
        # this patch binds the global it would read and `verify.assert_not_called()` goes live.
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_USER}.verify_recovery_code", return_value=True, create=True) as verify, \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "recovery_code",
                                     "code": "ABCD234XYZ"})
        assert resp.status_code == 400
        verify.assert_not_called()
        stepped_up.assert_not_called()

    def test_another_accounts_passkey_cannot_step_this_session_up(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_M}.credential_id_from_response", return_value="cred"), \
             patch(f"{_M}.get_passkey_by_credential_id",
                   return_value={"id": 5, "user_id": 999, "public_key": "pk", "sign_count": 0}), \
             patch(f"{_M}.verify_passkey_assertion", return_value=1) as verify, \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "passkey",
                                     "handle": "h", "credential": {"id": "cred"}})
        assert resp.status_code == 400
        verify.assert_not_called()
        stepped_up.assert_not_called()

    def test_a_step_up_challenge_issued_for_another_account_is_refused(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": 999, "challenge": "chal"}), \
             patch(f"{_USER}.record_step_up") as stepped_up:
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "passkey",
                                     "handle": "h", "credential": {"id": "cred"}})
        assert resp.status_code == 400
        stepped_up.assert_not_called()

    def test_step_up_attempts_are_rate_limited(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Blocked()), \
             patch(f"{_USER}.verify_totp_code") as verify:
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "totp", "code": "123456"})
        assert resp.status_code == 429
        verify.assert_not_called()

    def test_beginning_a_step_up_without_a_passkey_is_a_400(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.get_user_passkey_credential_ids", return_value=[]):
            resp = client.post("/api/user/step-up/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 400


class TestSessionRequired:
    """Every 2c endpoint that acts on an account needs a live session first. Cheap to get wrong on
    a new endpoint, and getting it wrong means an unauthenticated caller enrolling a factor.
    """

    ENDPOINTS = [
        ("post", "/api/user/passkeys/register/begin", {}),
        ("post", "/api/user/passkeys/register/complete", {"handle": "h", "credential": {}}),
        ("post", "/api/user/totp/enroll/begin", {}),
        ("post", "/api/user/totp/enroll/confirm", {"code": "123456"}),
        ("post", "/api/user/auth-factors/delete", {"factor_id": 1}),
        ("post", "/api/user/recovery-codes/regenerate", {}),
        ("post", "/api/user/step-up/begin", {}),
        ("post", "/api/user/step-up/verify", {"method": "totp", "code": "123456"}),
    ]

    @pytest.mark.parametrize("method,path,body", ENDPOINTS)
    def test_no_session_is_a_401(self, client, method, path, body):
        with patch(f"{_M}.get_session_user_id", return_value=None), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.begin_totp_enrollment") as enroll, \
             patch(f"{_USER}.confirm_totp_enrollment") as confirm, \
             patch(f"{_USER}.delete_auth_factor") as delete, \
             patch(f"{_USER}.generate_recovery_codes") as generate:
            resp = getattr(client, method)(path, json={"session_token": "nope", **body})
        assert resp.status_code == 401
        for should_not_run in (enroll, confirm, delete, generate):
            should_not_run.assert_not_called()


class TestFailedWrites:
    """A ceremony that cannot be recorded must fail loudly, not hand back a half-built factor."""

    def test_a_challenge_that_cannot_be_stored_aborts_the_registration(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.get_user_passkey_credential_ids", return_value=[]), \
             patch(f"{_USER}.build_registration_options", return_value=({}, "chal")), \
             patch(f"{_USER}.create_auth_challenge", return_value=None):
            resp = client.post("/api/user/passkeys/register/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 500

    def test_a_duplicate_passkey_is_reported_not_silently_dropped(self, client):
        class _Result:
            credential_id = "cred"
            public_key = "pk"
            sign_count = 0

        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration", return_value=_Result()), \
             patch(f"{_USER}.add_passkey_factor", return_value=None):
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h", "credential": {}})
        assert resp.status_code == 400

    def test_recovery_codes_that_could_not_be_written_are_never_shown(self, client):
        """Showing a sheet the database rejected would hand the user ten codes that do not work."""
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.generate_recovery_codes", return_value=[]):
            resp = client.post("/api/user/recovery-codes/regenerate",
                               json={"session_token": _TOKEN})
        assert resp.status_code == 500

    def test_a_missing_factor_is_a_404(self, client):
        from cqc_lem.utilities.auth_factors import FactorSummary
        empty = FactorSummary(factors=[], recovery_unused=0, recovery_total=0,
                              passkeys_supported=True, has_strong_factor=False,
                              pin_is_bootstrap_only=False)
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.factor_summary", return_value=empty), \
             patch(f"{_USER}.delete_auth_factor", return_value=False):
            resp = client.post("/api/user/auth-factors/delete",
                               json={"session_token": _TOKEN, "factor_id": 99})
        assert resp.status_code == 404

    def test_a_totp_enrolment_that_could_not_start_is_a_500(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.begin_totp_enrollment", return_value=None):
            resp = client.post("/api/user/totp/enroll/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 500

    def test_a_pin_that_cannot_open_a_pending_login_does_not_fall_back_to_a_session(self, client):
        """If the handle cannot be stored the login must fail — quietly minting a session instead
        would turn a storage error into a bypass of the second factor.
        """
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value=None), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        assert resp.status_code == 500
        create_session.assert_not_called()

    def test_the_bypass_path_fails_the_same_way(self, client):
        with patch(f"{_AUTH}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value=None), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/email/init", json={"email": _EMAIL})
        assert resp.status_code == 500
        create_session.assert_not_called()

    def test_a_passkey_login_that_cannot_open_a_challenge_is_a_500(self, client):
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.build_authentication_options", return_value=({}, "chal")), \
             patch(f"{_AUTH}.create_auth_challenge", return_value=None):
            resp = client.post("/api/auth/passkey/login/begin", json={})
        assert resp.status_code == 500

    def test_a_second_factor_that_verifies_but_cannot_mint_a_session_is_a_500(self, client):
        with patch(f"{_AUTH}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": None}), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=True), \
             patch(f"{_AUTH}.create_session", return_value=None):
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 500


class TestFactorSummaryEndpoint:
    def test_it_reports_the_state_the_account_page_renders(self, client):
        from cqc_lem.utilities.auth_factors import FactorSummary
        summary = FactorSummary(
            factors=[{"id": 1, "kind": "passkey", "label": "iPhone", "created_at": None,
                      "last_used_at": None}],
            recovery_unused=7, recovery_total=10, passkeys_supported=True,
            has_strong_factor=True, pin_is_bootstrap_only=True)
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.factor_summary", return_value=summary), \
             patch(f"{_USER}.step_up_satisfied", return_value=False):
            resp = client.get(f"/api/user/auth-factors?session_token={_TOKEN}")
        detail = resp.json()["detail"]
        assert detail["pin_is_bootstrap_only"] is True
        assert detail["recovery_codes_unused"] == 7
        assert detail["step_up_satisfied"] is False
        assert detail["factors"][0]["kind"] == "passkey"

    def test_it_needs_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/user/auth-factors?session_token=nope")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Seams the two-stage login opened (owner review on PR #906)
# ---------------------------------------------------------------------------

class TestTwoStageLoginSeams:
    """A login that used to be one request is now two, and the two share a rate-limit bucket, an
    audit trail and a session stamp. Each test here is a way that seam can leak.
    """

    def test_a_correct_pin_clears_the_limiter_before_handing_over_to_the_factor(self, client):
        """Without this the user's own earlier PIN typos throttle the passkey/TOTP prompt they are
        about to answer correctly — self-inflicted lockout on the one path that has no way around
        it.
        """
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle"), \
             patch(f"{_AUTH}.clear_auth_limits") as cleared:
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        assert resp.json()["detail"]["second_factor_required"] is True
        cleared.assert_called_once()
        assert cleared.call_args.args[0] == _EMAIL

    def test_the_bypass_path_does_not_clear_it(self, client):
        """The mirror image, and the reason the two branches differ: nothing is proved on the
        bypass — no PIN is ever typed — so there are no legitimate typo counters to forgive, and
        clearing them would let an unauthenticated caller reset every limiter in front of the
        second factor once per request.
        """
        with patch(f"{_AUTH}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["passkey"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle"), \
             patch(f"{_AUTH}.clear_auth_limits") as cleared:
            resp = client.post("/api/auth/email/init", json={"email": _EMAIL})
        assert resp.json()["detail"]["second_factor_required"] is True
        cleared.assert_not_called()


# ---------------------------------------------------------------------------
# The second factor's guessing budget survives starting over (adversarial review, PR #906)
# ---------------------------------------------------------------------------

class TestSecondFactorBudget:
    """`auth_challenges.attempts` bounds ONE pending login. Re-running the stage in front of it
    mints a new handle and a new counter for free, and both ways in are inside the threat model —
    the no-mail-provider bypass proves nothing, and a compromised mailbox (T2) mints PINs at will.
    So the budget has to be counted per ACCOUNT and carried into the new handle.
    """

    def _pin_login(self, client, spent: int):
        with patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending-handle") as challenge, \
             patch(f"{_AUTH}.count_challenge_attempts", return_value=spent):
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        return resp, challenge

    def test_a_new_handle_inherits_what_the_account_already_spent(self, client):
        resp, challenge = self._pin_login(client, spent=3)
        assert resp.json()["detail"]["second_factor_required"] is True
        assert challenge.call_args.kwargs["initial_attempts"] == 3

    def test_a_spent_budget_refuses_to_issue_another_handle(self, client):
        """Five guesses per round with unlimited rounds walks a 6-digit space. The 429 is what
        makes the round cost something.
        """
        resp, challenge = self._pin_login(client, spent=5)
        assert resp.status_code == 429
        challenge.assert_not_called()

    def test_an_unreadable_budget_fails_closed(self, client):
        """count_challenge_attempts returns -1 when the DB will not answer. A bound that reads as
        empty exactly when the database is unhappy is not a bound.
        """
        resp, challenge = self._pin_login(client, spent=-1)
        assert resp.status_code == 429
        challenge.assert_not_called()

    def test_the_bypass_path_is_bounded_by_the_same_budget(self, client):
        """The branch that needs no proof at all is the one that most needs it."""
        with patch(f"{_AUTH}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.count_challenge_attempts", return_value=5), \
             patch(f"{_AUTH}.create_auth_challenge") as challenge:
            resp = client.post("/api/auth/email/init", json={"email": _EMAIL})
        assert resp.status_code == 429
        challenge.assert_not_called()

    def test_a_correct_code_clears_the_account_budget(self, client):
        """A correct code is the proof that clears it — otherwise one earlier typo follows the user
        into their next sign-in.
        """
        with patch(f"{_AUTH}.claim_auth_challenge_attempt",
                   return_value={"user_id": _UID, "challenge": None, "attempts": 3}), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=True), \
             patch(f"{_AUTH}.create_session", return_value=_TOKEN), \
             patch(f"{_AUTH}.clear_challenge_attempts") as cleared:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "123456"})
        assert resp.status_code == 200
        cleared.assert_called_once_with(_UID, "second_factor")

    def test_a_wrong_code_does_not_clear_it(self, client):
        with patch(f"{_AUTH}.claim_auth_challenge_attempt",
                   return_value={"user_id": _UID, "challenge": None, "attempts": 2}), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.verify_totp_code", return_value=False), \
             patch(f"{_AUTH}.clear_challenge_attempts") as cleared:
            resp = client.post("/api/auth/second-factor/verify",
                               json={"pending_token": "p", "method": "totp", "code": "000000"})
        assert resp.status_code == 401
        cleared.assert_not_called()

    def test_the_public_passkey_begin_is_rate_limited(self, client):
        """Unauthenticated, and it writes a challenge row per call."""
        with patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_AUTH}.check_auth_init", return_value=_Blocked()), \
             patch(f"{_AUTH}.create_auth_challenge") as challenge:
            resp = client.post("/api/auth/passkey/login/begin", json={})
        assert resp.status_code == 429
        challenge.assert_not_called()

    def test_a_duplicate_passkey_is_not_a_cross_account_oracle(self, client):
        """Credential ids are globally unique, so "already registered" would tell this caller a
        passkey they hold is enrolled on somebody ELSE's account.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.webauthn_relying_party"), \
             patch(f"{_USER}.consume_auth_challenge",
                   return_value={"user_id": _UID, "challenge": "chal"}), \
             patch(f"{_USER}.verify_passkey_registration",
                   return_value=type("R", (), {"credential_id": "c", "public_key": "pk",
                                               "sign_count": 0})()), \
             patch(f"{_USER}.add_passkey_factor", return_value=None):
            resp = client.post("/api/user/passkeys/register/complete",
                               json={"session_token": _TOKEN, "handle": "h",
                                     "credential": {"id": "c"}})
        assert resp.status_code == 400
        assert "already" not in resp.json()["detail"].lower()

    def test_a_verified_step_up_that_does_not_stamp_is_a_500_not_a_silent_loop(self, client):
        """A 200 with no stamp means the very next write asks again, forever, with nothing logged
        anywhere — the failure mode is invisible, so it has to be loud.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_USER}.verify_totp_code", return_value=True), \
             patch(f"{_USER}.record_step_up", return_value=False):
            resp = client.post("/api/user/step-up/verify",
                               json={"session_token": _TOKEN, "method": "totp", "code": "123456"})
        assert resp.status_code == 500

    def test_an_account_with_no_email_still_gets_its_own_limiter_bucket(self, client):
        """A blank identity is skipped by the limiter, so falling back to "" would leave these
        accounts unlimited at step-up.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value=None), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()) as limiter, \
             patch(f"{_USER}.verify_totp_code", return_value=True), \
             patch(f"{_USER}.record_step_up", return_value=True):
            client.post("/api/user/step-up/verify",
                        json={"session_token": _TOKEN, "method": "totp", "code": "123456"})
        assert limiter.call_args.args[0] == f"user-{_UID}"

    def test_a_denied_step_up_audits_the_client_that_was_refused(self, client):
        """STEP_UP_DENIED is the row that most often means someone else is holding this session —
        without ip/user-agent on it there is nothing to chase.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.available_methods", return_value=["passkey"]), \
             patch(f"{_USER}.record_auth_event") as audit:
            resp = client.post("/api/user/linkedin-cookie",
                               json={"session_token": _TOKEN, "li_at": "x" * 40},
                               headers={"User-Agent": "test-agent"})
        assert resp.status_code == 403
        denial = next(c for c in audit.call_args_list
                      if c.kwargs.get("details", {}).get("action") == "store_linkedin_cookie")
        assert denial.kwargs["user_agent"] == "test-agent"
        assert "ip" in denial.kwargs

    def test_removing_a_factor_records_what_was_removed_and_what_is_left(self, client):
        from cqc_lem.utilities.auth_factors import FactorSummary
        summary = FactorSummary(
            factors=[{"id": 3, "kind": "totp", "label": None, "created_at": None,
                      "last_used_at": None}],
            recovery_unused=5, recovery_total=10, passkeys_supported=True,
            has_strong_factor=True, pin_is_bootstrap_only=True)
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.factor_summary", return_value=summary), \
             patch(f"{_USER}.delete_auth_factor", return_value=True), \
             patch(f"{_USER}.has_strong_factor", return_value=False), \
             patch(f"{_USER}.record_auth_event") as audit:
            resp = client.post("/api/user/auth-factors/delete",
                               json={"session_token": _TOKEN, "factor_id": 3})
        assert resp.status_code == 200
        assert resp.json()["detail"]["has_strong_factor"] is False
        details = audit.call_args.kwargs["details"]
        assert details["kind"] == "totp"
        assert details["has_strong_factor"] is False
