"""Session scopes are SURFACES (issue #745, phase 2c.1 — issue #905).

Two holds, one enforcement point. `api/main.get_session_user_id` is the ONE resolver every handler
calls, so both narrowings live there rather than at ~150 call sites:

- an `extension` session reaches the one path the browser extension actually POSTs to, and a
  STOLEN one can therefore no longer read posts, DM templates or settings the way it could in 2b;
- an `enroll` session — a PIN login that landed past `REQUIRE_STRONG_FACTOR_AFTER` on an account
  with no strong factor — reaches only what it needs to enrol one.

The other half of what these prove is the part that is easy to get wrong: nobody is locked out. The
PIN still signs the account in, enrolment is reachable, saving recovery codes is reachable, and
pulling the rollout back releases the sessions it already held.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_USER = "cqc_lem.api.routers.user"


def _kernel():
    """The `main` MODULE, for the refusals decided before any handler runs.

    A scope refusal happens in the auth kernel, so the audit row is written by the kernel's own
    binding of the audit function — not by the router module serving the path under test. The
    per-area router imported that same name for its own handlers, so naming the target as a STRING
    would be ambiguous: `test_router_patch_seam.py` reads that spelling as the moved-symbol mistake
    and has no way to tell the two call sites apart. Patching the module OBJECT says which of the
    two bindings is meant.
    """
    from cqc_lem.api import main

    return main


_UID = 21
_EMAIL = "held@example.com"
_TOKEN = "scoped-token"

# Far past / far future, so these never depend on the clock they run on.
_PAST = "2020-01-01"
_FUTURE = "2099-01-01"


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
def _account_without_a_strong_factor():
    """Overrides the directory-wide default in `conftest.py`.

    That fixture stubs `enrollment_required` / `enrollment_hold_active` off so the other ~40 API
    modules keep their pre-2c.1 behaviour — which is exactly the verdict this module exists to
    exercise. Here the real functions run; only the factor COUNT they read is stubbed, to the state
    every account is in until it enrols something.
    """
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet():
    """Silence the audit write in EVERY module that binds it.

    A fixture names no route, so it covers every route in the file — and since #1154 those are
    served from three modules. The login-only names are patched on the auth router alone, because
    that is now the only module that binds them.
    """
    with patch(f"{_M}.record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.record_auth_event", return_value=True), \
         patch(f"{_AUTH}.mark_email_verified", return_value=True), \
         patch(f"{_AUTH}.clear_auth_limits"):
        yield


def _session(scope: str):
    """The real resolver, answering for one live token with the given scope. `get_session_user_id`
    is deliberately NOT patched here — it is the thing under test.
    """
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _deadline(value: str):
    """`REQUIRE_STRONG_FACTOR_AFTER` is read at the call site, so the env IS the control."""
    return patch.dict("os.environ", {"REQUIRE_STRONG_FACTOR_AFTER": value})


class TestSurfaceMatching:
    """The path→surface mapping, on its own. Routes are mounted twice — under `/api` for the SPA
    and at the root for the redirect targets — so both spellings have to reach one entry.
    """

    def test_both_mount_points_map_to_one_surface_entry(self):
        from cqc_lem.api.main import _scope_path

        assert _scope_path("/api/user/linkedin-cookie") == "/user/linkedin-cookie"
        assert _scope_path("/user/linkedin-cookie") == "/user/linkedin-cookie"
        assert _scope_path("/api/user/linkedin-cookie/") == "/user/linkedin-cookie"
        assert _scope_path("/api") == "/"
        assert _scope_path(None) is None

    def test_an_unknown_path_fails_closed(self):
        """A restricted token that reached a handler by a route the middleware never saw is exactly
        the case the narrowing exists for.
        """
        from cqc_lem.api.main import _scope_allows

        assert _scope_allows("extension", None) is False
        assert _scope_allows("extension", "/api/user/posts") is False
        assert _scope_allows("extension", "/api/user/linkedin-cookie") is True

    def test_an_unrestricted_scope_is_allowed_anywhere(self):
        """The browser's own two, plus the legacy NULL row every 2b session carries."""
        from cqc_lem.api.main import _scope_allows

        for scope in ("full", "recovery", None):
            assert _scope_allows(scope, "/api/user/posts") is True

    def test_an_unrecognised_scope_fails_closed(self):
        """The table of surfaces must not itself be opt-in. A typo, a hand-edited row, or a scope a
        later phase adds and only half wires up would otherwise be granted EVERYTHING by omission —
        the same "remembered somewhere else" failure this design exists to remove.
        """
        from cqc_lem.api.main import _scope_allows

        for scope in ("some-future-scope", "enrol", "Enroll", "extension "):
            assert _scope_allows(scope, "/api/user/posts") is False
            # Not even its near-neighbour's surface — an unknown scope has no surface at all.
            assert _scope_allows(scope, "/api/user/linkedin-cookie") is False

    def test_an_unrecognised_scope_is_refused_at_the_resolver(self, client):
        """End to end, not just the predicate: a row carrying a scope nobody taught the table about
        gets a 403, not a session.
        """
        with _session("enrol-typo"):
            r = client.get("/api/user/auth-factors", params={"session_token": _TOKEN})
        assert r.status_code == 403

    def test_a_prefix_is_not_a_surface_match(self):
        """Without exact matching, '/user/auth-factors' would also unlock a future
        '/user/auth-factors-admin'.
        """
        from cqc_lem.api.main import _scope_allows

        assert _scope_allows("enroll", "/api/user/auth-factors") is True
        assert _scope_allows("enroll", "/api/user/auth-factors-admin") is False


# ---------------------------------------------------------------------------
# The extension token stops being a full session
# ---------------------------------------------------------------------------

class TestExtensionScope:
    OFF_SURFACE = [
        ("get", "/api/user/security", None),
        ("get", "/api/auth/session", None),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
        ("post", "/api/user/extension-token", {}),
        ("get", "/api/user/auth-factors", None),
    ]

    @pytest.mark.parametrize("method,path,body", OFF_SURFACE)
    def test_a_stolen_extension_token_cannot_reach_the_rest_of_the_account(
            self, client, method, path, body):
        """The whole point of 2c.1. Before it, this token read and wrote everything the SPA can."""
        with _session("extension"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL) as email, \
             patch(f"{_USER}.revoke_other_sessions") as revoke, \
             patch(f"{_USER}.create_session") as create_session, \
             patch(f"{_USER}.create_pin_for_email") as create_pin, \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            if body is None:
                resp = getattr(client, method)(path, params={"session_token": _TOKEN})
            else:
                resp = getattr(client, method)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        # Refused BEFORE the handler did anything — not merely refused on the way out.
        for should_not_run in (email, revoke, create_session, create_pin):
            should_not_run.assert_not_called()

    def test_the_one_endpoint_the_extension_actually_calls_still_works(self, client):
        with _session("extension"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie",
                               json={"session_token": _TOKEN, "li_at": "a" * 40})
        assert resp.status_code == 200
        store.assert_called_once()

    def test_the_refusal_is_audited(self, client):
        """The extension calls exactly one path, so this row cannot happen by accident — it is the
        clearest signal available that someone else is holding that token.
        """
        from cqc_lem.utilities.db import AuthAuditEvent

        with _session("extension"), \
             patch.object(_kernel(), "record_auth_event") as recorded, \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN},
                              headers={"CF-Connecting-IP": "203.0.113.9"})
        assert resp.status_code == 403
        event, kwargs = recorded.call_args[0][0], recorded.call_args[1]
        assert event == AuthAuditEvent.SESSION_SCOPE_DENIED
        assert kwargs["user_id"] == _UID
        assert kwargs["success"] is False
        assert kwargs["ip"] == "203.0.113.9"
        assert kwargs["details"] == {"scope": "extension", "path": "/user/security"}

    def test_a_failed_audit_write_does_not_turn_a_refusal_into_a_500(self, client):
        """The refusal IS the control; the row is only the record of it."""
        with _session("extension"), \
             patch.object(_kernel(), "record_auth_event", side_effect=RuntimeError("db down")), \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 403

    def test_a_held_enrolment_session_is_not_audited(self, client):
        """It produces these constantly and harmlessly while the SPA settles — auditing them would
        bury the one row that means something.
        """
        with _deadline(_PAST), _session("enroll"), \
             patch.object(_kernel(), "record_auth_event") as recorded, \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 403
        recorded.assert_not_called()

    def test_a_full_session_is_untouched_by_the_narrowing(self, client):
        with _session("full"), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_a_legacy_row_with_no_scope_behaves_as_a_full_session(self, client):
        """`scope` was added in 2c and defaults to 'full'; a NULL from an older row must not be
        read as a restriction that silently signs someone out of their own account.
        """
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": None} if t == _TOKEN else None), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_a_restricted_token_does_not_fall_through_to_a_full_cookie(self, client):
        """The narrowing has to survive a request that carries BOTH. Serving it on the cookie would
        be the restriction quietly not happening.
        """
        def resolve(token):
            return {"user_id": _UID, "scope": "extension" if token == _TOKEN else "full"}

        with patch(f"{_M}._db_resolve_session", side_effect=resolve), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN},
                              cookies={"lem_session": "browser-cookie"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Mandatory enrolment — the deadline decides the scope at LOGIN
# ---------------------------------------------------------------------------

class TestEnrollmentDeadline:
    def _pin_login(self, client):
        with patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.create_session", return_value="new-token") as create_session:
            resp = client.post("/api/auth/email/verify",
                               json={"email": _EMAIL, "pin": "123456"})
        return resp, create_session

    def test_no_deadline_keeps_todays_behaviour_exactly(self, client):
        from cqc_lem.utilities.db import SESSION_SCOPE_FULL
        with _deadline(""):
            resp, create_session = self._pin_login(client)
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["scope"] == SESSION_SCOPE_FULL
        assert resp.json()["detail"]["enrollment_required"] is False

    def test_a_future_deadline_does_not_hold_anyone_yet(self, client):
        from cqc_lem.utilities.db import SESSION_SCOPE_FULL
        with _deadline(_FUTURE):
            resp, create_session = self._pin_login(client)
        assert create_session.call_args.kwargs["scope"] == SESSION_SCOPE_FULL

    def test_past_the_deadline_a_factorless_pin_login_is_held(self, client):
        from cqc_lem.utilities.db import SESSION_SCOPE_ENROLL
        with _deadline(_PAST):
            resp, create_session = self._pin_login(client)
        # Signed in — the PIN is still a bootstrap. Held, not refused.
        assert resp.status_code == 200
        assert resp.json()["detail"]["session_token"] == "new-token"
        assert create_session.call_args.kwargs["scope"] == SESSION_SCOPE_ENROLL
        assert resp.json()["detail"]["enrollment_required"] is True

    def test_an_account_that_already_enrolled_is_never_held(self, client):
        """It goes down the 2c bootstrap path instead — PIN, then a factor."""
        with _deadline(_PAST), \
             patch(f"{_AUTH}.has_strong_factor", return_value=True), \
             patch(f"{_AUTH}.available_methods", return_value=["totp"]), \
             patch(f"{_AUTH}.count_challenge_attempts", return_value=0), \
             patch(f"{_AUTH}.create_auth_challenge", return_value="pending"), \
             patch(f"{_AUTH}.hash_pin", return_value="h"), \
             patch(f"{_AUTH}.verify_pin_for_email", return_value=True), \
             patch(f"{_AUTH}.get_pin_lockout", return_value=None), \
             patch(f"{_AUTH}.get_user_id", return_value=_UID), \
             patch(f"{_AUTH}.check_auth_verify", return_value=_Allowed()), \
             patch(f"{_AUTH}.create_session") as create_session:
            resp = client.post("/api/auth/email/verify", json={"email": _EMAIL, "pin": "123456"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["second_factor_required"] is True
        create_session.assert_not_called()

    def test_the_kill_switch_beats_the_deadline(self, client):
        from cqc_lem.utilities.db import SESSION_SCOPE_FULL
        with _deadline(_PAST), \
             patch("cqc_lem.utilities.auth_factors.STRONG_AUTH_ENABLED", False):
            _resp, create_session = self._pin_login(client)
        assert create_session.call_args.kwargs["scope"] == SESSION_SCOPE_FULL


# ---------------------------------------------------------------------------
# What a held session may and may not do
# ---------------------------------------------------------------------------

class TestEnrollmentHold:
    OFF_SURFACE = [
        ("get", "/api/user/security", None),
        ("post", "/api/user/linkedin-cookie", {"li_at": "a" * 40}),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
        ("post", "/api/user/extension-token", {}),
    ]

    @pytest.mark.parametrize("method,path,body", OFF_SURFACE)
    def test_a_held_session_reaches_nothing_but_enrolment(self, client, method, path, body):
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_USER}.store_linkedin_li_at") as store_cookie, \
             patch(f"{_USER}.revoke_other_sessions") as revoke, \
             patch(f"{_USER}.list_user_sessions", return_value=[]):
            if body is None:
                resp = getattr(client, method)(path, params={"session_token": _TOKEN})
            else:
                resp = getattr(client, method)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        # The SPA reads this code and renders the gate instead of a dead page. 403 and never 401:
        # the axios interceptor treats any 401 as a dead session and would sign the user out.
        assert resp.json()["detail"]["code"] == "enrollment_required"
        store_cookie.assert_not_called()
        revoke.assert_not_called()

    def test_a_held_session_can_still_enrol(self, client):
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_USER}.enrollment_allowed", return_value=True), \
             patch(f"{_USER}.has_confirmed_totp", return_value=False), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL), \
             patch(f"{_USER}.begin_totp_enrollment", return_value=(1, "SEED", "otpauth://x")):
            resp = client.post("/api/user/totp/enroll/begin", json={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["secret"] == "SEED"

    def test_a_held_session_can_still_save_recovery_codes(self, client):
        """Forced to enrol and then unable to save the sheet would be the worst possible order."""
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.generate_recovery_codes", return_value=["AAA", "BBB"]):
            resp = client.post("/api/user/recovery-codes/regenerate",
                               json={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["codes"] == ["AAA", "BBB"]

    def test_a_held_session_can_still_sign_out(self, client):
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_M}._db_get_session_user_id", return_value=_UID), \
             patch(f"{_AUTH}.delete_session") as ds:
            resp = client.post("/api/auth/logout", json={"session_token": _TOKEN})
        assert resp.status_code == 200
        ds.assert_called_once()

    def test_the_session_check_reports_the_hold(self, client):
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
             patch(f"{_AUTH}.is_user_admin", return_value=False), \
             patch(f"{_AUTH}.strong_factor_prompt_due", return_value=True):
            resp = client.get("/api/auth/session", params={"session_token": _TOKEN})
        detail = resp.json()["detail"]
        assert resp.status_code == 200
        assert detail["enrollment_required"] is True
        assert detail["strong_factor_prompt"] is True
        assert detail["strong_factor_deadline"].startswith("2020-01-01")

    def test_enrolling_a_factor_releases_the_hold(self, client):
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_M}._db_get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.enrollment_allowed", return_value=True), \
             patch(f"{_USER}.confirm_totp_enrollment", return_value=True), \
             patch(f"{_USER}.count_recovery_codes", return_value=(0, 0)), \
             patch(f"{_USER}.session_signed_in_with_recovery_code", return_value=False), \
             patch(f"{_USER}.record_step_up", return_value=True), \
             patch(f"{_M}.release_enrollment_scope", return_value=True) as released:
            resp = client.post("/api/user/totp/enroll/confirm",
                               json={"session_token": _TOKEN, "code": "123456"})
        assert resp.status_code == 200
        released.assert_called_once_with(_TOKEN)

    def test_pulling_the_rollout_back_releases_a_session_already_held(self, client):
        """Clearing the date must not strand everyone who signed in during the window until their
        session expires — the hold is re-decided on every read, not baked into the row.
        """
        with _deadline(""), _session("enroll"), \
             patch(f"{_M}.release_enrollment_scope", return_value=True) as released, \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        # ...and the row is promoted, so the next request costs no extra question.
        released.assert_called_once_with(_TOKEN)

    def test_enrolling_on_ONE_device_releases_the_others(self, client):
        """The hold belongs to the ACCOUNT, not the session row. Deciding it from the row alone is
        a dead end on every other device: the account now HAS a factor, so enrolling again is
        step-up gated — and the step-up ceremony is deliberately outside the enrolment surface, so
        there would be no way forward but signing out.
        """
        with _deadline(_PAST), _session("enroll"), \
             patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=1), \
             patch(f"{_M}.release_enrollment_scope", return_value=True) as released, \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        released.assert_called_once_with(_TOKEN)

    def test_a_promotion_that_cannot_be_written_still_grants_access(self, client):
        """The write is bookkeeping; the verdict is re-derived from the account every request. A DB
        error here must cost one extra query next time, never access.
        """
        with _deadline(_PAST), _session("enroll"), \
             patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=1), \
             patch(f"{_M}.release_enrollment_scope", side_effect=RuntimeError("db down")), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_the_session_check_agrees_with_what_the_server_enforces(self, client):
        """`/auth/session` reports the hold off the SAME read that authenticated the request. A
        second lookup could answer 'not held' to the browser while every request it then made was
        refused — the app rendering over a wall of 403s.
        """
        with _deadline(_PAST), _session("enroll"), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
             patch(f"{_AUTH}.is_user_admin", return_value=False):
            held = client.get("/api/auth/session", params={"session_token": _TOKEN})
        assert held.json()["detail"]["enrollment_required"] is True

        # Same session, once the account holds a factor: released, and reported released.
        with _deadline(_PAST), _session("enroll"), \
             patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=1), \
             patch(f"{_M}.release_enrollment_scope", return_value=True), \
             patch(f"{_AUTH}.get_user_email", return_value=_EMAIL), \
             patch(f"{_AUTH}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_AUTH}.get_user_analytics_profile", return_value={}), \
             patch(f"{_AUTH}.is_user_admin", return_value=False):
            free = client.get("/api/auth/session", params={"session_token": _TOKEN})
        assert free.json()["detail"]["enrollment_required"] is False

    def test_an_enrolment_write_is_not_attempted_when_no_rollout_is_configured(self, client):
        """The promotion is an UPDATE. A deployment with no deadline cannot hold a session, so it
        should not pay a write on every enrolment to discover that.
        """
        with _deadline(""), _session("full"), \
             patch(f"{_M}._db_get_session_user_id", return_value=_UID), \
             patch(f"{_USER}.enrollment_allowed", return_value=True), \
             patch(f"{_USER}.confirm_totp_enrollment", return_value=True), \
             patch(f"{_USER}.count_recovery_codes", return_value=(0, 0)), \
             patch(f"{_USER}.session_signed_in_with_recovery_code", return_value=False), \
             patch(f"{_USER}.record_step_up", return_value=True), \
             patch(f"{_M}.release_enrollment_scope") as released:
            resp = client.post("/api/user/totp/enroll/confirm",
                               json={"session_token": _TOKEN, "code": "123456"})
        assert resp.status_code == 200
        released.assert_not_called()


# ---------------------------------------------------------------------------
# The surfaces are path LITERALS, so they can drift away from what they describe
# ---------------------------------------------------------------------------

class TestSurfacesDoNotDrift:
    """Both surfaces are hardcoded path strings, and each has a different failure mode when the
    thing it names moves. Rename a route and the `enroll` surface becomes a LOCKOUT — the gate's
    own fetch 403s and the held user has nowhere to go. Change what the extension POSTs and the
    `extension` surface breaks the one-click reconnect. Neither shows up in any other test, because
    every other test spells the same literal the source does.
    """

    def test_every_surface_path_is_a_real_route(self):
        """Closes the loop against the router itself rather than against another copy of the list.

        Read off `router` (the `/api` surface, where every entry in both sets lives) plus `app` (the
        handful mounted at the root), and assert the source is non-trivial first — a guard that
        silently compares against an empty set is worse than no guard.
        """
        from cqc_lem.api.main import _SCOPE_SURFACES, _scope_path, _walk_routes, app

        # `_walk_routes`, not `router.routes + app.routes`. The concatenation only works while
        # every route hangs off ONE router: FastAPI >=0.139 represents an included router as a
        # single `_IncludedRouter` node with no `.path`, so the moment `/api` is split into
        # per-area routers (#1154) those routes drop out of `known` and this guard starts
        # reporting real surface paths as "routes that do not exist". Both forms yield the same
        # 159 keys today, which is why the swap happens here, while the guard is green.
        known = {_scope_path(r.path)
                 for r in _walk_routes(app.routes)
                 if getattr(r, "path", None)}
        assert len(known) > 100, "route table not populated — this guard would be vacuous"
        for scope, surface in _SCOPE_SURFACES.items():
            missing = surface - known
            assert not missing, f"{scope} surface names routes that do not exist: {missing}"

    def test_the_extension_surface_matches_what_the_extension_actually_calls(self):
        """Read out of `browser_extension/popup.js`, not asserted in prose. A path the extension
        calls that is NOT on the surface is a 403 on the user's reconnect click; the reverse — a
        surface entry the extension never calls — is blast radius handed to a stolen token for
        nothing, so this is an equality, not a subset.
        """
        import re
        from pathlib import Path

        import cqc_lem
        from cqc_lem.api.main import _EXTENSION_SESSION_SURFACE, _scope_path

        popup = (Path(cqc_lem.__file__).parent / "browser_extension" / "popup.js").read_text()
        called = {_scope_path(m) for m in re.findall(r"/api/[A-Za-z0-9\-_/]+", popup)}
        assert called == set(_EXTENSION_SESSION_SURFACE)

    def test_the_extension_surface_is_one_path_and_one_METHOD(self):
        """A surface entry is a PATH, and a path is method-blind. Adding `GET /user/linkedin-cookie`
        later would hand every extension token — including a stolen one — a READ of the LinkedIn
        cookie it never had, with nothing in the source saying so and no other test noticing. This
        is what says so: the entry the extension POSTs to must stay POST-only, or the surface has to
        be widened deliberately.
        """
        from cqc_lem.api.main import _EXTENSION_SESSION_SURFACE, _scope_path, _walk_routes, app

        served: dict = {}
        for r in _walk_routes(app.routes):
            key = _scope_path(getattr(r, "path", None))
            if key in _EXTENSION_SESSION_SURFACE:
                served.setdefault(key, set()).update(getattr(r, "methods", None) or set())
        assert served == {"/user/linkedin-cookie": {"POST"}}


class _Allowed:
    allowed = True
    scope = None
    retry_after_seconds = 3600
