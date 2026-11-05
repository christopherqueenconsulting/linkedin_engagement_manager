"""The `observer` scope: a read-only seat for an operations agent.

An operations agent needs to SEE the account — the review queues, the preferences and automation
state that say whether anything is safe, the dashboard, its own audit trail — and must be able to
change none of it. `agent` could not serve: its surface matches on PATH, so every verb on a granted
path comes with it, and it also carries the create routes. `observer` adds the missing dimension to
the ONE resolver: a scope listed in `_SCOPE_METHODS` is held to those verbs on every path it reaches.

Four claims, each asserted here rather than in prose:

1. It reaches the read surface and nothing else, and only on GET / HEAD / OPTIONS.
2. Every other verb on the SAME paths is refused, as are the credential, account-mover, session-
   revocation and minting paths — including the two minting endpoints (no successors).
3. An unknown path, an unknown scope, and a verb-limited scope with no readable request all fail
   CLOSED.
4. Its expiry is fixed at mint and never slid, exactly like an `agent`'s.

The route-table half of this (no role reaches a forbidden route, today or after the next route is
added) lives in `test_role_surface_invariants.py`.
"""

import contextvars
import re
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"

_UID = 31
_EMAIL = "owner@example.com"
_TOKEN = "observer-token"

_READ_VERBS = ("GET", "HEAD", "OPTIONS")
_WRITE_VERBS = ("POST", "PUT", "PATCH", "DELETE")

_OBSERVER_PATHS = (
    "/connection_requests", "/outreach/targets", "/dms", "/lead_signals", "/leads",
    "/catchup/touches", "/user/engagement-preferences", "/user/automation-status",
    "/dashboard/stats/", "/user/linkedin-profile-skills", "/curated/sources",
    "/auth/session", "/app-info", "/flags", "/user/security",
)

_CLOSED_DOORS = (
    "/user/linkedin-cookie", "/user/linkedin-password",
    "/user/passkeys/register/begin", "/user/passkeys/login/begin",
    "/user/totp/enroll/begin", "/user/recovery-codes/regenerate",
    "/user/email/change/init", "/user/email/change/verify",
    "/user/sessions/revoke",
    "/user/extension-token", "/user/agent-token", "/user/observer-token", "/user/operator-token",
    "/user/auth-factors", "/user/step-up/begin",
    # Capacity rather than a credential: one press opens a Chrome session from the fixed pool.
    "/user/linkedin-profile/refresh",
    # Write routes the agent reaches and an observer must not, and the operator's maintenance routes.
    "/leads/refresh", "/admin/automation-status", "/admin/automation-pause",
    "/connection_request", "/outreach/target", "/schedule_dm", "/lead_signal", "/lead",
    "/curated/source",
)


@pytest.fixture(scope="module")
def main_mod() -> Any:
    from cqc_lem.api import main
    return main


@pytest.fixture(scope="module")
def user_mod() -> Any:
    from cqc_lem.api.routers import user
    return user


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor() -> Any:
    """Pin the account to "no enrolled factor" for the real resolver.

    This runs the resolver the same way as `test_session_scopes.py`, which it mirrors.

    `enrollment_required` / `enrollment_hold_active` read the factor COUNT, and the directory-wide
    default in `conftest.py` stubs them off. None of these tests is about the enrolment hold; the
    patch only stops the answer depending on a database the unit lane does not have. It is
    autouse because every test below goes through the resolver, and it patches one read.
    """
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet() -> Any:
    with patch(f"{_M}.record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _no_access_record() -> Any:
    """Keep the once-a-day access record out of every test that is not about it.

    `TestObserverAccessRecord` overrides this with the real claim over a fake Redis.
    """
    with patch(f"{_M}.claim_observer_access", return_value=False), \
         patch.object(_kernel(), "get_session_id", return_value=1):
        yield


def _session(scope: str) -> Any:
    """The real resolver, answering for one live token with the given scope."""
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _key(main_mod: ModuleType, path: str) -> str:
    """Resolve a path through `_scope_path`, the way a real request does.

    Asserting against the raw frozenset would pass on an entry no live request can ever match.
    """
    return main_mod._scope_path("/api" + path)


class TestObserverSurface:
    @pytest.mark.parametrize("path", _OBSERVER_PATHS)
    @pytest.mark.parametrize("verb", _READ_VERBS)
    def test_every_read_path_is_reachable_on_a_read_verb(self, main_mod: ModuleType, path: str, verb: str) -> None:
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, _key(main_mod, path), verb)

    @pytest.mark.parametrize("path", _OBSERVER_PATHS)
    def test_every_surface_entry_survives_path_normalisation(self, main_mod: ModuleType, path: str) -> None:
        assert _key(main_mod, path) in main_mod._OBSERVER_SESSION_SURFACE

    def test_the_surface_has_no_entry_the_list_above_does_not_name(self, main_mod: ModuleType) -> None:
        """The reviewed list above is the whole surface.

        A new surface entry fails here until it is added to the list, which is the moment someone
        has to say out loud that an observer may now see it.
        """
        named = {_key(main_mod, p) for p in _OBSERVER_PATHS}
        assert set(main_mod._OBSERVER_SESSION_SURFACE) == named

    @pytest.mark.parametrize("path", _OBSERVER_PATHS)
    @pytest.mark.parametrize("verb", _WRITE_VERBS)
    def test_the_same_paths_refuse_every_write_verb(self, main_mod: ModuleType, path: str, verb: str) -> None:
        """The reason `_SCOPE_METHODS` exists.

        A path grants every verb; this is the check that takes them back.
        """
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, _key(main_mod, path), verb)

    @pytest.mark.parametrize("path", _CLOSED_DOORS)
    @pytest.mark.parametrize("verb", _READ_VERBS + _WRITE_VERBS)
    def test_credential_account_session_and_mint_paths_are_closed_on_every_verb(
            self, main_mod: ModuleType, path: str, verb: str) -> None:
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, _key(main_mod, path), verb)

    @pytest.mark.parametrize("path", [
        "/dms/all", "/leads/export", "/lead_magnets", "/connection_requests/bulk-approve",
        "/user/security/export", "/flags/override", "/dashboard/stats/internal", "/posts", "/",
    ])
    def test_an_unknown_path_fails_closed_even_on_a_read_verb(self, main_mod: ModuleType, path: str) -> None:
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER,
                                          _key(main_mod, path), "GET")

    def test_a_missing_path_fails_closed(self, main_mod: ModuleType) -> None:
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, None, "GET")

    def test_the_verb_is_case_insensitive_but_never_widened(self, main_mod: ModuleType) -> None:
        key = _key(main_mod, "/user/security")
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, key, "get")
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, key, "post")
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, key, "GETX")
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, key, "")


class TestTheVerbComesFromTheRequest:
    """The verb is read off the request, so no call site has to remember it.

    `_scope_checked` calls `_scope_allows(scope, path)` with no verb; the resolver reads it off the
    Request ContextVar the middleware already stamps.
    """

    def test_the_method_is_read_off_the_request_contextvar(self, main_mod: ModuleType) -> None:
        key = _key(main_mod, "/user/security")
        for verb, expected in (("GET", True), ("POST", False), ("DELETE", False)):
            reset = main_mod._request_object.set(SimpleNamespace(method=verb))
            try:
                assert main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER, key) is expected
            finally:
                main_mod._request_object.reset(reset)

    def test_no_request_in_scope_means_refused_not_waved_through(self, main_mod: ModuleType) -> None:
        """A verb-limited scope that cannot learn its verb must not default to 'read'."""
        assert main_mod._request_object.get() is None
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OBSERVER,
                                          _key(main_mod, "/user/security"))

    def test_a_scope_with_no_verb_limit_never_consults_the_request(self, main_mod: ModuleType) -> None:
        """Byte-identical for extension / enroll / agent: no request, a write verb — still path-only."""
        assert main_mod._request_object.get() is None
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT,
                                      _key(main_mod, "/connection_request"))
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT,
                                      _key(main_mod, "/connection_request"), "DELETE")
        assert main_mod._scope_allows("extension", _key(main_mod, "/user/linkedin-cookie"), "POST")
        assert main_mod._scope_allows("enroll", _key(main_mod, "/user/auth-factors"), "POST")

    def test_only_observer_is_verb_limited(self, main_mod: ModuleType) -> None:
        assert set(main_mod._SCOPE_METHODS) == {main_mod.SESSION_SCOPE_OBSERVER}
        assert main_mod._SCOPE_METHODS[main_mod.SESSION_SCOPE_OBSERVER] == frozenset(_READ_VERBS)


class TestScopeRegistration:
    def test_the_scope_is_registered_so_it_does_not_fail_closed_everywhere(self, main_mod: ModuleType) -> None:
        assert main_mod.SESSION_SCOPE_OBSERVER == "observer"
        assert main_mod.SESSION_SCOPE_OBSERVER in main_mod._SCOPE_SURFACES
        assert main_mod.SESSION_SCOPE_OBSERVER in main_mod._SCOPE_REFUSAL_CODE
        assert main_mod._SCOPE_REFUSAL_CODE[main_mod.SESSION_SCOPE_OBSERVER] == "session_scope_forbidden"

    def test_the_scope_is_not_unrestricted(self, main_mod: ModuleType) -> None:
        assert main_mod.SESSION_SCOPE_OBSERVER not in main_mod._UNRESTRICTED_SCOPES

    def test_it_is_exported_from_the_db_facade(self) -> None:
        from cqc_lem.utilities import db
        assert db.SESSION_SCOPE_OBSERVER == "observer"
        assert "SESSION_SCOPE_OBSERVER" in db.__all__

    @pytest.mark.parametrize("scope", ["observr", "Observer", "observer ", "read-only", "operator-x"])
    def test_an_unrecognised_scope_still_fails_closed(self, main_mod: ModuleType, scope: str) -> None:
        assert not main_mod._scope_allows(scope, _key(main_mod, "/user/security"), "GET")


class TestObserverEndToEnd:
    """Through the real resolver and a real route, so a refusal is a 403 BEFORE the handler runs."""

    def test_an_allowed_read_is_a_200(self, api_client: Any) -> None:
        with _session("observer"), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = api_client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_the_same_path_with_a_write_verb_is_a_403_and_writes_nothing(self, api_client: Any) -> None:
        """A write verb on a read path is refused before the handler.

        `/user/engagement-preferences` is on the surface for its GET; the PUT behind the same path
        sets the approval modes and every cap.
        """
        with _session("observer"), \
             patch(f"{_USER}.update_engagement_preferences") as upd:
            resp = api_client.put("/api/user/engagement-preferences",
                                  json={"session_token": _TOKEN, "tone": "casual"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        upd.assert_not_called()

    @pytest.mark.parametrize("verb,path,body", [
        ("post", "/api/user/linkedin-cookie", {"li_at": "a" * 40}),
        ("put", "/api/user/linkedin-password", {"linkedin_password": "x"}),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
        ("post", "/api/user/extension-token", {}),
        ("post", "/api/user/agent-token", {}),
        ("post", "/api/user/observer-token", {}),
        ("post", "/api/user/passkeys/register/begin", {}),
        ("post", "/api/user/totp/enroll/begin", {}),
        ("post", "/api/user/recovery-codes/regenerate", {}),
        ("post", "/api/connection_request", {"recipient_profile_url": "https://example.com/in/x"}),
        ("put", "/api/connection_request", {"request_id": 1, "action": "approve"}),
        ("post", "/api/schedule_dm", {"recipient_profile_url": "https://example.com/in/x",
                                       "message": "hi", "status": "approved",
                                       "scheduled_datetime": "2099-01-01T09:00:00"}),
    ])
    def test_credential_mint_and_write_routes_are_refused_before_the_handler(
            self, api_client: Any, verb: str, path: str, body: dict[str, Any]) -> None:
        with _session("observer"), \
             patch(f"{_USER}.create_session") as create_session, \
             patch(f"{_USER}.revoke_other_sessions") as revoke, \
             patch(f"{_USER}.create_pin_for_email") as create_pin, \
             patch(f"{_USER}.store_linkedin_li_at") as store:
            resp = getattr(api_client, verb)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403, f"{verb.upper()} {path} -> {resp.status_code}"
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        for must_not_run in (create_session, revoke, create_pin, store):
            must_not_run.assert_not_called()

    def test_a_read_on_a_path_outside_the_surface_is_a_403(self, api_client: Any) -> None:
        with _session("observer"):
            resp = api_client.get("/api/posts/", params={"session_token": _TOKEN})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"

    def test_an_unknown_scope_is_still_refused(self, api_client: Any) -> None:
        with _session("observr"):
            resp = api_client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 403

    def test_a_full_session_is_untouched(self, api_client: Any) -> None:
        with _session("full"), \
             patch(f"{_USER}.list_user_sessions", return_value=[]), \
             patch(f"{_USER}.get_auth_audit_events", return_value=[]), \
             patch(f"{_USER}.get_user_public_uid", return_value="pub-1"), \
             patch(f"{_USER}.get_user_email", return_value=_EMAIL):
            resp = api_client.get("/api/user/security", params={"session_token": _TOKEN})
        assert resp.status_code == 200

    def test_an_agent_session_still_reaches_its_write_routes_byte_for_byte(self, main_mod: ModuleType) -> None:
        """The regression that matters for the shared resolver.

        Adding a verb table must not have changed what an `agent` may do.
        """
        for path, verb in (("/connection_request", "POST"), ("/connection_request", "PUT"),
                           ("/connection_request", "DELETE"), ("/user/engagement-preferences", "PUT"),
                           ("/dms", "GET")):
            assert main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT,
                                          _key(main_mod, path), verb), (verb, path)
        for path in ("/user/agent-token", "/user/observer-token", "/user/security",
                     "/user/sessions/revoke"):
            assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_AGENT,
                                              _key(main_mod, path), "POST"), path


class TestObserverTokenTTL:
    @staticmethod
    def _resolve_with_scope(fake_cursor: Any, scope: str) -> Any:
        from datetime import datetime, timezone

        from cqc_lem.utilities import db

        conn, cursor = fake_cursor(fetch_one={
            "user_id": 7, "scope": scope,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            resolved = db.resolve_session("a" * 64)
        updates = [c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper()]
        assert len(updates) == 1
        return resolved, updates[0][0][0]

    def test_an_observer_session_expiry_is_never_slid(self, fake_cursor: Any) -> None:
        """The same trap as the agent's.

        `resolve_session` slides EVERY session to now + SESSION_IDLE_HOURS, so a 90-day token
        would be 24 hours after its first request.
        """
        from cqc_lem.utilities.db import SESSION_SCOPE_OBSERVER

        resolved, sql = self._resolve_with_scope(fake_cursor, SESSION_SCOPE_OBSERVER)
        assert resolved == {"user_id": 7, "scope": SESSION_SCOPE_OBSERVER}
        assert "expires_at" not in sql, "an observer session's granted TTL must survive being used"
        assert "last_seen_at" in sql

    def test_agent_is_still_fixed_and_a_browser_session_still_slides(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import SESSION_SCOPE_AGENT, SESSION_SCOPE_FULL

        _, agent_sql = self._resolve_with_scope(fake_cursor, SESSION_SCOPE_AGENT)
        _, full_sql = self._resolve_with_scope(fake_cursor, SESSION_SCOPE_FULL)
        assert "expires_at" not in agent_sql
        assert "expires_at" in full_sql

    def test_the_fixed_ttl_scopes_are_exactly_the_machine_held_ones(self) -> None:
        from cqc_lem.platform.db.repositories.auth import FIXED_TTL_SCOPES
        assert FIXED_TTL_SCOPES == frozenset({"agent", "observer", "operator"})


class TestObserverTokenMint:
    def test_minting_requires_a_session(self, main_mod: ModuleType, user_mod: ModuleType) -> None:
        from fastapi import HTTPException
        with patch.object(main_mod, "get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as exc:
                user_mod.mint_observer_token(user_mod.ObserverTokenRequest(session_token="nope"))
        assert exc.value.status_code == 401

    def test_ttl_defaults_to_90_days_and_is_bounded(self, user_mod: ModuleType) -> None:
        f = user_mod.ObserverTokenRequest.model_fields["ttl_days"]
        assert f.default == 90
        assert "1" in str(f.metadata) and "365" in str(f.metadata)

    def test_minting_is_step_up_gated_and_never_exempts_the_observer_scope(self, user_mod: ModuleType) -> None:
        """Read the call off the AST, as the agent test does.

        The CALL grants the exemption, not a comment documenting it.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(user_mod.mint_observer_token)))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_require_step_up"]
        assert len(calls) == 1, "minting must be step-up gated exactly once"
        assert not any(kw.arg == "extension_scope_ok" for kw in calls[0].keywords)
        assert len(calls[0].args) <= 3, "no positional argument may reach extension_scope_ok"

    def test_a_mint_creates_a_fixed_ttl_observer_row_and_audits_it(self, api_client: Any) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="minted") as create_session, \
             patch(f"{_USER}.record_auth_event") as recorded:
            resp = api_client.post("/api/user/observer-token",
                                   json={"session_token": _TOKEN, "label": "ops watcher",
                                         "ttl_days": 30})
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"session_token": "minted", "expires_in_days": 30}
        kwargs = create_session.call_args.kwargs
        assert kwargs["scope"] == "observer"
        assert kwargs["ttl_hours"] == 30 * 24
        assert kwargs["label"] == "ops watcher"
        event = recorded.call_args[0][0]
        assert event == AuthAuditEvent.OBSERVER_TOKEN_MINTED
        assert event == "observer_token_minted"
        assert recorded.call_args.kwargs["details"]["scope"] == "observer"

    def test_a_mint_without_a_fresh_factor_is_refused_and_mints_nothing(self, api_client: Any) -> None:
        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post("/api/user/observer-token", json={"session_token": _TOKEN})
        assert resp.status_code == 403
        create_session.assert_not_called()

    def test_an_observer_cannot_mint_and_neither_can_an_agent(self, api_client: Any) -> None:
        """No successors: each mint endpoint is absent from both machine surfaces."""
        for scope in ("observer", "agent"):
            for path in ("/api/user/observer-token", "/api/user/agent-token",
                         "/api/user/extension-token"):
                with _session(scope), patch(f"{_USER}.create_session") as create_session:
                    resp = api_client.post(path, json={"session_token": _TOKEN})
                assert resp.status_code == 403, (scope, path)
                create_session.assert_not_called()


def _kernel() -> Any:
    """The `main` MODULE object, for patching the kernel's own binding of a name."""
    from cqc_lem.api import main

    return main


class TestRefusalsAreLoggedAtWarning:
    """A refused machine seat is logged at WARNING.

    A seat reaching a verb or path it was not given is a misconfigured agent or a stolen token.
    Until this was a WARNING it was a DEBUG line nobody reads, so a leaked observer token probing
    the API left no trace.
    """

    _MSG = "Machine session refused outside its scope"

    @staticmethod
    def _refuse(scope: str, method: str, path: str, token: str = "tok-123") -> Any:
        """Drive `_scope_checked` for one request and return the HTTPException it raises."""
        from fastapi import HTTPException

        main = _kernel()
        resets = [(main._request_object, main._request_object.set(SimpleNamespace(method=method))),
                  (main._request_path, main._request_path.set(path))]
        try:
            with pytest.raises(HTTPException) as exc:
                main._scope_checked({"user_id": _UID, "scope": scope}, token)
        finally:
            for var, reset in reversed(resets):
                var.reset(reset)
        return exc.value

    def test_an_observer_post_to_a_surface_path_emits_a_warning(self) -> None:
        with patch.object(_kernel(), "log_warning") as warn, \
             patch.object(_kernel(), "log_debug") as debug:
            exc = self._refuse("observer", "POST", "/api/dms")
        assert exc.status_code == 403
        assert exc.detail["code"] == "session_scope_forbidden"
        warn.assert_called_once()
        assert warn.call_args.args[0] == self._MSG, "the message is a stable template"
        kwargs = warn.call_args.kwargs
        assert kwargs["scope"] == "observer"
        assert kwargs["method"] == "POST"
        assert kwargs["path"] == "/dms"
        assert kwargs["user_id"] == _UID
        assert not [c for c in debug.call_args_list if "refused outside" in str(c)], (
            "the refusal must not go back to a debug-only line")

    def test_an_observer_read_outside_the_surface_also_warns(self) -> None:
        with patch.object(_kernel(), "log_warning") as warn:
            self._refuse("observer", "GET", "/api/user/linkedin-cookie")
        assert warn.call_args.kwargs["path"] == "/user/linkedin-cookie"

    def test_an_agent_refusal_warns_too(self) -> None:
        with patch.object(_kernel(), "log_warning") as warn:
            self._refuse("agent", "POST", "/api/user/agent-token")
        warn.assert_called_once()
        assert warn.call_args.kwargs["scope"] == "agent"

    def test_the_log_line_carries_a_hash_of_the_session_never_the_token(self) -> None:
        import hashlib

        token = "tok-secret-value-123"
        with patch.object(_kernel(), "log_warning") as warn:
            self._refuse("observer", "POST", "/api/dms", token=token)
        logged = repr(warn.call_args)
        session = warn.call_args.kwargs["session"]
        assert token not in logged
        assert session and re.fullmatch(r"[0-9a-f]{16}", session)
        # Not the column the database looks sessions up by.
        assert session != hashlib.sha256(token.encode()).hexdigest()[:16]
        assert hashlib.sha256(token.encode()).hexdigest() not in logged

    def test_the_same_session_always_gets_the_same_label(self) -> None:
        main = _kernel()
        assert main._session_log_id("abc") == main._session_log_id("abc")
        assert main._session_log_id("abc") != main._session_log_id("abd")
        assert main._session_log_id(None) is None

    def test_a_refused_enrolment_hold_stays_quiet(self) -> None:
        """The enrolment hold produces these constantly and harmlessly while the SPA settles."""
        with patch.object(_kernel(), "log_warning") as warn, \
             patch.object(_kernel(), "enrollment_required", return_value=True):
            self._refuse("enroll", "GET", "/api/posts")
        warn.assert_not_called()

    def test_a_refusal_writes_no_audit_row(self) -> None:
        """Logged, not audited: an agent probing a path must not fill the last-20 events list."""
        with patch.object(_kernel(), "record_auth_event") as recorded:
            self._refuse("observer", "POST", "/api/dms")
        recorded.assert_not_called()

    def test_end_to_end_the_refusal_is_a_403_and_a_warning(self, api_client: Any) -> None:
        with _session("observer"), patch.object(_kernel(), "log_warning") as warn:
            resp = api_client.put("/api/user/engagement-preferences",
                                  json={"session_token": _TOKEN, "tone": "casual"})
        assert resp.status_code == 403
        assert warn.call_args.args[0] == self._MSG
        assert warn.call_args.kwargs["method"] == "PUT"


class TestAMissingRequestContextIsARefusalNotA500:
    def test_scope_allows_refuses_in_a_context_where_no_request_was_ever_set(self) -> None:
        main = _kernel()

        def run() -> bool:
            return main._scope_allows(main.SESSION_SCOPE_OBSERVER, "/user/security")

        assert contextvars.Context().run(run) is False

    def test_scope_checked_raises_a_403_not_an_attribute_error(self) -> None:
        from fastapi import HTTPException

        main = _kernel()

        def run() -> int:
            return main._scope_checked({"user_id": _UID, "scope": "observer"}, "tok")

        with pytest.raises(HTTPException) as exc:
            contextvars.Context().run(run)
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("request_object", [object(), SimpleNamespace(), SimpleNamespace(method=None),
                                                SimpleNamespace(method=b"GET"), SimpleNamespace(method=7)])
    def test_a_request_object_with_no_usable_method_is_refused(self, request_object: Any) -> None:
        main = _kernel()
        reset = main._request_object.set(request_object)
        try:
            assert main._scope_allows(main.SESSION_SCOPE_OBSERVER, "/user/security") is False
        finally:
            main._request_object.reset(reset)

    def test_an_explicit_method_still_works_without_any_request(self) -> None:
        main = _kernel()
        assert main._scope_allows(main.SESSION_SCOPE_OBSERVER, "/user/security", "GET") is True


def _mint_headers() -> dict[str, str]:
    return {"X-LEM-Client": "spa"}


class TestObserverTokenMintEndpoint:
    """Through the real route, so a refusal is a status code the client sees."""

    def _post(self, api_client: Any, body: dict[str, Any]) -> Any:
        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="minted") as create_session:
            resp = api_client.post("/api/user/observer-token",
                                   json={"session_token": _TOKEN, **body})
        self.create_session = create_session
        return resp

    @pytest.mark.parametrize("ttl_days", [0, 366, -1, 10_000])
    def test_a_ttl_outside_1_to_365_is_a_422_and_mints_nothing(self, api_client: Any,
                                                              ttl_days: int) -> None:
        resp = self._post(api_client, {"ttl_days": ttl_days})
        assert resp.status_code == 422
        self.create_session.assert_not_called()

    @pytest.mark.parametrize("ttl_days", [1, 365])
    def test_the_bounds_themselves_are_accepted(self, api_client: Any, ttl_days: int) -> None:
        resp = self._post(api_client, {"ttl_days": ttl_days})
        assert resp.status_code == 200
        assert self.create_session.call_args.kwargs["ttl_hours"] == ttl_days * 24

    @pytest.mark.parametrize("label", [
        "owner@example.com", "line one\nline two", "tab\there", "<script>", "name; DROP",
        "a" * 121, "café", "trailing newline\n", "ops (prod)", "x/y",
    ])
    def test_a_label_outside_the_allowed_characters_or_length_is_a_422(self, api_client: Any,
                                                                      label: str) -> None:
        resp = self._post(api_client, {"label": label})
        assert resp.status_code == 422, label
        self.create_session.assert_not_called()

    @pytest.mark.parametrize("label", ["ops watcher", "monitor.v2", "ops:prod-1", "A_b c", "a" * 120, ""])
    def test_a_plain_label_is_accepted_as_given(self, api_client: Any, label: str) -> None:
        resp = self._post(api_client, {"label": label})
        assert resp.status_code == 200
        assert self.create_session.call_args.kwargs["label"] == (label or "Read-only observer")

    def test_the_label_field_says_what_it_is_for(self, user_mod: Any) -> None:
        field = user_mod.ObserverTokenRequest.model_fields["label"]
        description = field.description or ""
        for phrase in ("Security card", "audit log", "personal data"):
            assert phrase in description, phrase

    def test_no_session_is_a_401(self, api_client: Any) -> None:
        with _session("full"):
            resp = api_client.post("/api/user/observer-token", json={"session_token": "stale"})
        assert resp.status_code == 401

    def test_a_missing_factor_is_a_403_and_mints_nothing(self, api_client: Any) -> None:
        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post("/api/user/observer-token", json={"session_token": _TOKEN})
        assert resp.status_code == 403
        create_session.assert_not_called()

    def test_a_session_that_cannot_be_stored_is_a_500_not_a_token(self, api_client: Any) -> None:
        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value=None), \
             patch(f"{_USER}.record_auth_event") as recorded:
            resp = api_client.post("/api/user/observer-token", json={"session_token": _TOKEN})
        assert resp.status_code == 500
        recorded.assert_not_called()

    @pytest.mark.parametrize("path", ["/api/user/observer-token", "/api/user/agent-token",
                                      "/api/user/extension-token"])
    def test_an_observer_cannot_mint_any_token(self, api_client: Any, path: str) -> None:
        with _session("observer"), patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post(path, json={"session_token": _TOKEN})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        create_session.assert_not_called()
