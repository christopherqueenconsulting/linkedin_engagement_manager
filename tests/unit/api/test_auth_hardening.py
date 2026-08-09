"""Identity + session hardening at the API seam (issue #745, phase 2b).

What these cover: the session token leaves the browser's reach (httpOnly cookie), a request is
resolved from that cookie, auth endpoints are rate limited and PIN-locked, sessions are listable
and revocable per device, and the email address can move without the account moving.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_COOKIE = "lem_session"
_UID = 7


@contextmanager
def _live_session(token: str = "real", user_id: int = _UID, scope: str = "full"):
    """One live token, answered by BOTH resolvers.

    Since 2c.1 `get_session_user_id` reads the row (id + scope) through `_db_resolve_session` so it
    can refuse a scoped session outside its surface; `current_session_token` still only asks whether
    a token resolves. Patching one and not the other would let a test pass on a resolver the request
    never used.
    """
    with patch(f"{_M}._db_resolve_session",
               side_effect=lambda t: {"user_id": user_id, "scope": scope} if t == token else None), \
         patch(f"{_M}._db_get_session_user_id",
               side_effect=lambda t: user_id if t == token else None):
        yield


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
    would just be two unmocked DB calls.
    """
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

    def test_the_bypass_login_does_not_claim_the_email_was_verified(self, client):
        """No mail provider means no PIN reached the address, so nothing proved control of it.
        email_verified_at records that proof and must stay empty here.
        """
        with patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.generate_pin", return_value="123456"), \
             patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.send_pin_email", return_value=(True, True)), \
             patch(f"{_M}.create_session", return_value="tok_bypass"), \
             patch(f"{_M}.mark_email_verified") as mev:
            resp = client.post("/api/auth/email/init", json={"email": "user@example.com"})
        assert resp.status_code == 200
        mev.assert_not_called()

    def test_a_verified_pin_does_stamp_the_email_as_verified(self, client):
        with patch(f"{_M}.hash_pin", return_value="h"), \
             patch(f"{_M}.verify_pin_for_email", return_value=True), \
             patch(f"{_M}.get_pin_lockout", return_value=None), \
             patch(f"{_M}.get_user_id", return_value=_UID), \
             patch(f"{_M}.create_session", return_value="tok"), \
             patch(f"{_M}.mark_email_verified") as mev:
            resp = client.post("/api/auth/email/verify",
                               json={"email": "user@example.com", "pin": "123456"})
        assert resp.status_code == 200
        mev.assert_called_once_with(_UID)

    def test_logout_clears_the_cookie_and_deletes_the_session(self, client):
        with patch(f"{_M}.delete_session") as ds, _live_session(token="tok_secret"):
            resp = client.post("/api/auth/logout", json={"session_token": "tok_secret"})
        assert resp.status_code == 200
        ds.assert_called_once_with("tok_secret")
        # An expiry in the past is how a cookie is deleted.
        assert _COOKIE in resp.headers.get("set-cookie", "")


class TestCookieResolution:
    """The SPA sends a non-secret sentinel; the request is authenticated by the cookie."""

    def test_sentinel_resolves_from_the_cookie(self, client):
        with _live_session(), \
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
        with patch(f"{_M}._db_resolve_session", return_value=None) as db:
            resp = client.get("/api/auth/session", params={"session_token": "cookie"})
        assert resp.status_code == 401
        # The sentinel is never handed to the session lookup — it is not a token.
        db.assert_not_called()

    def test_an_explicit_token_still_works_without_a_cookie(self, client):
        with _live_session(), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_analytics_profile", return_value={}), \
             patch(f"{_M}.is_user_admin", return_value=False):
            resp = client.get("/api/auth/session", params={"session_token": "real"})
        assert resp.status_code == 200

    def test_a_stale_explicit_token_falls_back_to_the_live_cookie(self, client):
        with _live_session(), \
             patch(f"{_M}.get_user_email", return_value="me@example.com"), \
             patch(f"{_M}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_M}.get_user_analytics_profile", return_value={}), \
             patch(f"{_M}.is_user_admin", return_value=False):
            resp = client.get("/api/auth/session", params={"session_token": "expired"},
                              cookies={_COOKIE: "real"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["user_id"] == _UID

    def test_a_stale_token_does_not_become_the_acting_session(self, client):
        """The seam between the two resolvers. When a stale explicit token falls through to the
        cookie, the token the request ACTS as has to fall through with it — otherwise logout deletes
        a row that no longer exists and leaves the live one signed in, and "sign out all other
        devices" fails to match the caller's own session as the one to keep and revokes it.
        """
        with _live_session(), \
             patch(f"{_M}.delete_session") as ds:
            resp = client.post("/api/auth/logout", json={"session_token": "expired"},
                               cookies={_COOKIE: "real"})
        assert resp.status_code == 200
        ds.assert_called_once_with("real")

    def test_revoke_all_others_keeps_the_cookie_session_not_a_stale_token(self, client):
        with _live_session(), \
             patch(f"{_USER}.revoke_other_sessions", return_value=1) as ro:
            # A cookie-authenticated write, so it carries the SPA's client header like the real one
            # does (issue #957); `test_csrf_client_header.py` is where its absence is the subject.
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "expired", "all_others": True},
                               cookies={_COOKIE: "real"},
                               headers={"X-LEM-Client": "spa"})
        assert resp.status_code == 200
        assert ro.call_args[1]["keep_token"] == "real"


class TestClientAddress:
    """The per-IP limiter and every ip_hash are only worth the header they are read from."""

    def test_cloudflare_header_wins_over_a_spoofed_forwarded_for(self, client):
        with patch(f"{_M}.check_auth_init", return_value=_Blocked()), \
             patch(f"{_M}.record_auth_event") as rec:
            resp = client.post("/api/auth/email/init", json={"email": "user@example.com"},
                               headers={"CF-Connecting-IP": "203.0.113.9",
                                        "X-Forwarded-For": "1.1.1.1, 203.0.113.9"})
        assert resp.status_code == 429
        # A caller who prepends their own X-Forwarded-For entry must not get a fresh IP bucket.
        assert rec.call_args[1]["ip"] == "203.0.113.9"

    def test_forwarded_for_is_still_used_when_there_is_no_cloudflare(self, client):
        with patch(f"{_M}.check_auth_init", return_value=_Blocked()), \
             patch(f"{_M}.record_auth_event") as rec:
            client.post("/api/auth/email/init", json={"email": "user@example.com"},
                        headers={"X-Forwarded-For": "198.51.100.4"})
        assert rec.call_args[1]["ip"] == "198.51.100.4"


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
             patch(f"{_USER}.list_user_sessions", return_value=rows), \
             patch(f"{_USER}.get_auth_audit_events", return_value=events), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value="me@example.com"):
            resp = client.get("/api/user/security", params={"session_token": "tok"})
        detail = resp.json()["detail"]
        assert detail["sessions"][0]["is_current"] is True
        assert detail["recent_events"][0]["event"] == "login_success"
        # No token material of any kind leaves the API.
        assert "session_token" not in str(detail)

    def test_revoke_one_device_is_scoped_to_the_caller(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.revoke_session", return_value=True) as rs:
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "session_id": 3})
        assert resp.status_code == 200
        rs.assert_called_once_with(_UID, 3)

    def test_revoking_someone_elses_session_is_a_404(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.revoke_session", return_value=False):
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "session_id": 999})
        assert resp.status_code == 404

    def test_revoke_all_others_keeps_this_device(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.revoke_other_sessions", return_value=2) as ro:
            resp = client.post("/api/user/sessions/revoke",
                               json={"session_token": "tok", "all_others": True})
        assert resp.status_code == 200
        assert resp.json()["detail"]["revoked"] == 2
        assert ro.call_args[1]["keep_token"] == "tok"

    def test_the_extension_gets_its_own_labelled_session(self, client):
        """The SPA has no token to hand over any more, so the extension is minted one — a device of
        its own that can be revoked without signing the person out of the app.
        """
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.create_session", return_value="tok_ext") as cs:
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
             patch(f"{_USER}.get_user_email", return_value="old@example.com"), \
             patch(f"{_USER}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_USER}.get_user_id", return_value=None), \
             patch(f"{_USER}.generate_pin", return_value="123456"), \
             patch(f"{_USER}.hash_pin", return_value="h"), \
             patch(f"{_USER}.create_pin_for_email", return_value=True) as cp, \
             patch(f"{_USER}.send_pin_email", side_effect=[(False, False), (True, False)]) as sp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "New@Example.com "})
        assert resp.status_code == 200
        assert cp.call_args[0][0] == "new@example.com"
        assert sp.call_args_list[-1][0][0] == "new@example.com"

    def test_init_rejects_an_address_owned_by_someone_else(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value="old@example.com"), \
             patch(f"{_USER}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_USER}.send_pin_email", return_value=(False, False)), \
             patch(f"{_USER}.get_user_id", return_value=99), \
             patch(f"{_USER}.create_pin_for_email") as cp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "taken@example.com"})
        assert resp.status_code == 400
        cp.assert_not_called()

    def test_init_rejects_the_address_already_on_the_account(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value="same@example.com"):
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "SAME@example.com"})
        assert resp.status_code == 400

    def test_init_is_unavailable_when_no_mail_provider_is_configured(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.get_user_email", return_value="old@example.com"), \
             patch(f"{_USER}.check_auth_init", return_value=_Allowed()), \
             patch(f"{_USER}.get_user_id", return_value=None), \
             patch(f"{_USER}.send_pin_email", return_value=(True, True)), \
             patch(f"{_USER}.create_pin_for_email") as cp:
            resp = client.post(self.BASE_INIT,
                               json={"session_token": "tok", "new_email": "new@example.com"})
        assert resp.status_code == 503
        cp.assert_not_called()

    def test_verify_moves_the_account_and_revokes_other_devices(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_USER}.get_pin_lockout", return_value=None), \
             patch(f"{_USER}.hash_pin", return_value="h"), \
             patch(f"{_USER}.verify_pin_for_email", return_value=True), \
             patch(f"{_USER}.get_user_email", return_value="old@example.com"), \
             patch(f"{_USER}.get_session_id", return_value=11), \
             patch(f"{_USER}.change_user_email", return_value=True) as ce, \
             patch(f"{_USER}.revoke_other_sessions", return_value=3) as ro:
            resp = client.post(self.BASE_VERIFY, json={"session_token": "tok",
                                                       "new_email": "new@example.com",
                                                       "pin": "123456"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["sessions_revoked"] == 3
        ce.assert_called_once_with(_UID, "new@example.com", changed_by_session_id=11)
        assert ro.call_args[1]["keep_token"] == "tok"

    def test_verify_with_a_bad_pin_changes_nothing(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_USER}.get_pin_lockout", return_value=None), \
             patch(f"{_USER}.hash_pin", return_value="h"), \
             patch(f"{_USER}.verify_pin_for_email", return_value=False), \
             patch(f"{_USER}.change_user_email") as ce:
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
