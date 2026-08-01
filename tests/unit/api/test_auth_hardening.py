"""Identity + session hardening at the API seam (issue #745, phase 2b).

What these cover: the session token leaves the browser's reach (httpOnly cookie), a request is
resolved from that cookie, auth endpoints are rate limited and PIN-locked, sessions are listable
and revocable per device, and the email address can move without the account moving.
"""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_COOKIE = "lem_session"
_UID = 7


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
def _quiet_audit():
    """The audit row and the verified stamp are asserted where they matter; everywhere else they
    would just be two unmocked DB calls."""
    with patch(f"{_M}.record_auth_event", return_value=True), \
         patch(f"{_M}.mark_email_verified", return_value=True):
        yield


class _Allowed:
    allowed = True
    scope = None
    retry_after_seconds = 3600


class _Blocked:
    allowed = False
    scope = "init_email"
    retry_after_seconds = 900


# ---------------------------------------------------------------------------
# The token stops being something the page can read
# ---------------------------------------------------------------------------

class TestSessionCookie:
    def test_verify_sets_an_httponly_session_cookie(self, client):
        with patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.verify_pin_for_email", return_value=True), \
             patch(f"{_M}.get_pin_lockout", return_value=None), \
             patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.create_session", return_value="tok_secret"):
            resp = client.post("/api/auth/email/verify",
                               json={"email": "user@example.com", "pin": "123456"})
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert f"{_COOKIE}=tok_secret" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Path=/" in set_cookie

    def test_bypass_login_sets_the_cookie_too(self, client):
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.generate_pin", return_value="123456"), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.send_pin_email", return_value=(True, True)), \
             patch(f"{_M}.create_session", return_value="tok_bypass"):
            resp = client.post("/api/auth/email/init", json={"email": "user@example.com"})
        assert resp.status_code == 200
        assert f"{_COOKIE}=tok_bypass" in resp.headers.get("set-cookie", "")

    def test_logout_clears_the_cookie_and_deletes_the_session(self, client):
        with patch(f"{_M}.delete_session") as ds, \
             patch(f"{_M}._db_get_session_user_id", return_value=_UID):
            resp = client.post("/api/auth/logout", json={"session_token": "tok_secret"})
        assert resp.status_code == 200
        ds.assert_called_once_with("tok_secret")
        # An expiry in the past is how a cookie is deleted.
        assert _COOKIE in resp.headers.get("set-cookie", "")


class TestCookieResolution:
    """The SPA sends a non-secret sentinel; the request is authenticated by the cookie."""

    def test_sentinel_resolves_from_the_cookie(self, client):
        with patch(f"{_M}._db_get_session_user_id", side_effect=lambda t: _UID if t == "real" else None), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_analytics_profile", return_value={}), \
             patch(f"{_M}.is_user_admin", return_value=False):
            resp = client.get("/api/auth/session", params={"session_token": "cookie"},
                              cookies={_COOKIE: "real"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["user_id"] == _UID
        assert resp.json()["detail"]["public_uid"] == "pub-1"

    def test_no_cookie_and_only_the_sentinel_is_unauthenticated(self, client):
        with patch(f"{_M}._db_get_session_user_id", return_value=None) as db:
            resp = client.get("/api/auth/session", params={"session_token": "cookie"})
        assert resp.status_code == 401
        # The sentinel is never handed to the session lookup — it is not a token.
        db.assert_not_called()

    def test_an_explicit_token_still_works_without_a_cookie(self, client):
        with patch(f"{_M}._db_get_session_user_id", side_effect=lambda t: _UID if t == "real" else None), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_analytics_profile", return_value={}), \
             patch(f"{_M}.is_user_admin", return_value=False):
            resp = client.get("/api/auth/session", params={"session_token": "real"})
        assert resp.status_code == 200

    def test_a_stale_explicit_token_falls_back_to_the_live_cookie(self, client):
        with patch(f"{_M}._db_get_session_user_id", side_effect=lambda t: _UID if t == "real" else None), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_analytics_profile", return_value={}), \
             patch(f"{_M}.is_user_admin", return_value=False):
            resp = client.get("/api/auth/session", params={"session_token": "expired"},
                              cookies={_COOKIE: "real"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["user_id"] == _UID


# ---------------------------------------------------------------------------
# Rate limiting + PIN lockout
# ---------------------------------------------------------------------------

class TestAuthRateLimiting:
    def test_init_over_the_limit_returns_429_with_retry_after(self, client):
        with patch(f"{_M}.check_auth_init", return_value=_Blocked()), \
             patch(f"{_M}.get_user_id", return_value=_UID) as gui:
            resp = client.post("/api/auth/email/init", json={"email": "user@example.com"})
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "900"
        # Blocked BEFORE any account lookup, so a throttled caller learns nothing.
        gui.assert_not_called()

    def test_verify_over_the_limit_returns_429(self, client):
        with patch(f"{_M}.check_auth_verify", return_value=_Blocked()), \
             patch(f"{_M}.verify_pin_for_email") as vp:
            resp = client.post("/api/auth/email/verify",
                               json={"email": "user@example.com", "pin": "000000"})
        assert resp.status_code == 429
        vp.assert_not_called()

    def test_a_locked_pin_returns_429_not_401(self, client):
        with patch(f"{_M}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.get_pin_lockout", return_value="2026-08-01T00:15:00"), \
             patch(f"{_M}.verify_pin_for_email") as vp:
            resp = client.post("/api/auth/email/verify",
                               json={"email": "user@example.com", "pin": "000000"})
        assert resp.status_code == 429
        vp.assert_not_called()

    def test_a_successful_login_clears_the_counters(self, client):
        with patch(f"{_M}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.get_pin_lockout", return_value=None), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.verify_pin_for_email", return_value=True), \
             patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.create_session", return_value="tok"), \
             patch(f"{_M}.clear_auth_limits") as clear:
            resp = client.post("/api/auth/email/verify",
                               json={"email": "user@example.com", "pin": "123456"})
        assert resp.status_code == 200
        clear.assert_called_once()


# ---------------------------------------------------------------------------
# Per-device sessions
# ---------------------------------------------------------------------------

class TestSessionManagement:
    def test_security_endpoint_401s_without_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/user/security", params={"session_token": "bad"})
        assert resp.status_code == 401

    def test_security_endpoint_returns_devices_and_history(self, client):
        rows = [{"id": 1, "label": "Chrome on macOS", "created_at": None, "last_seen_at": None,
                 "expires_at": None, "is_current": True}]
        events = [{"event": "login_success", "success": 1, "user_agent": "UA", "created_at": None}]
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.list_user_sessions", return_value=rows), \
             patch(f"{_M}.get_auth_audit_events", return_value=events), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"):
            resp = client.get("/api/user/security", params={"session_token": "tok"})
        detail = resp.json()["detail"]
        assert detail["sessions"][0]["is_current"] is True
        assert detail["recent_events"][0]["event"] == "login_success"
        # No token material of any kind leaves the API.
        assert "session_token" not in str(detail)

    def test_revoke_one_device_is_scoped_to_the_caller(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.revoke_session", return_value=True) as rs:
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "session_id": 3})
        assert resp.status_code == 200
        rs.assert_called_once_with(_UID, 3)

    def test_revoking_someone_elses_session_is_a_404(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.revoke_session", return_value=False):
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "session_id": 999})
        assert resp.status_code == 404

    def test_revoke_all_others_keeps_this_device(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.revoke_other_sessions", return_value=2) as ro:
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "all_others": True})
        assert resp.status_code == 200
        assert resp.json()["detail"]["revoked"] == 2
        assert ro.call_args[1]["keep_token"] == "tok"

    def test_the_extension_gets_its_own_labelled_session(self, client):
        """The SPA has no token to hand over any more, so the extension is minted one — a device of
        its own that can be revoked without signing the person out of the app."""
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.create_session", return_value="tok_ext") as cs:
            resp = client.post("/api/user/extension-token", json={"session_token": "tok"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["session_token"] == "tok_ext"
        assert cs.call_args[1]["label"] == "LinkedIn Connect extension"

    def test_extension_token_401s_without_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.post("/api/user/extension-token", json={"session_token": "bad"})
        assert resp.status_code == 401

    def test_revoke_without_a_target_is_a_400(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID):
            resp = client.post("/api/user/sessions/revoke", json={"session_token": "tok"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Email as an attribute of the account
# ---------------------------------------------------------------------------

class TestEmailChange:
    BASE_INIT = "/api/user/email/change/init"
    BASE_VERIFY = "/api/user/email/change/verify"

    def test_init_sends_the_code_to_the_new_address(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_email", return_value="old@example.com"), \
             patch(f"{_M}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_M}.get_user_id", return_value=None), \
             patch(f"{_M}.generate_pin", return_value="123456"), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.create_pin_for_email", return_value=True) as cp, \
             patch(f"{_M}.send_pin_email", side_effect=[(False, False), (True, False)]) as sp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "New@Example.com "})
        assert resp.status_code == 200
        assert cp.call_args[0][0] == "new@example.com"
        assert sp.call_args_list[-1][0][0] == "new@example.com"

    def test_init_rejects_an_address_owned_by_someone_else(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_email", return_value="old@example.com"), \
             patch(f"{_M}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_M}.send_pin_email", return_value=(False, False)), \
             patch(f"{_M}.get_user_id", return_value=99), \
             patch(f"{_M}.create_pin_for_email") as cp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "taken@example.com"})
        assert resp.status_code == 400
        cp.assert_not_called()

    def test_init_rejects_the_address_already_on_the_account(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_email", return_value="same@example.com"):
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "SAME@example.com"})
        assert resp.status_code == 400

    def test_init_is_unavailable_when_no_mail_provider_is_configured(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.get_user_email", return_value="old@example.com"), \
             patch(f"{_M}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_M}.get_user_id", return_value=None), \
             patch(f"{_M}.send_pin_email", return_value=(True, True)), \
             patch(f"{_M}.create_pin_for_email") as cp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "new@example.com"})
        assert resp.status_code == 503
        cp.assert_not_called()

    def test_verify_moves_the_account_and_revokes_other_devices(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.get_pin_lockout", return_value=None), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.verify_pin_for_email", return_value=True), \
             patch(f"{_M}.get_user_email", return_value="old@example.com"), \
             patch(f"{_M}.get_session_id", return_value=11), \
             patch(f"{_M}.change_user_email", return_value=True) as ce, \
             patch(f"{_M}.revoke_other_sessions", return_value=3) as ro:
            resp = client.post(self.BASE_VERIFY, json={"session_token": "tok",
                                                       "new_email": "new@example.com",
                                                       "pin": "123456"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["sessions_revoked"] == 3
        ce.assert_called_once_with(_UID, "new@example.com", changed_by_session_id=11)
        assert ro.call_args[1]["keep_token"] == "tok"

    def test_verify_with_a_bad_pin_changes_nothing(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_M}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_M}.get_pin_lockout", return_value=None), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.verify_pin_for_email", return_value=False), \
             patch(f"{_M}.change_user_email") as ce:
            resp = client.post(self.BASE_VERIFY, json={"session_token": "tok",
                                                       "new_email": "new@example.com",
                                                       "pin": "000000"})
        assert resp.status_code == 401
        ce.assert_not_called()

    def test_verify_401s_without_a_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.post(self.BASE_VERIFY, json={"session_token": "bad",
                                                       "new_email": "new@example.com",
                                                       "pin": "123456"})
        assert resp.status_code == 401
