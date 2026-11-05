"""The `operator` scope: a maintenance seat that may approve ONLY on an internal or sandbox account.

`operator` is the agent's queueing surface plus a short reviewed list of non-destructive maintenance
routes, minted the same way (human session, step-up, fixed TTL, no successor). What is new is the
FOURTH approval guard next to the agent's three: `users.account_kind`. On a 'standard' account an
operator behaves exactly like an agent — it can queue, it cannot approve. On an 'internal' or
'sandbox' account it may approve explicitly, and every one of those approvals is audited with the
role. It never gets the passive `auto_approve` default, never configures the account, and never
reaches a credential, the account mover, session revocation or a minting endpoint.

The route-table half (no role reaches a forbidden route) is `test_role_surface_invariants.py`.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_ADMIN = "cqc_lem.api.routers.admin"

_UID = 41
_TOKEN = "operator-token"
_PROFILE = "https://example.com/in/someone"


@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main
    return main


@pytest.fixture(scope="module")
def user_mod():
    from cqc_lem.api.routers import user
    return user


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor():
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet():
    """Silence the audit write in every module that binds it; `audit` re-patches the kernel's."""
    with patch(f"{_USER}.record_auth_event", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _no_access_record():
    """Keep the once-a-day access record out of the tests here that are not about it.

    `test_operator_hardening.py::TestOperatorAccessRecord` runs the real claim over a fake Redis.
    """
    from cqc_lem.api import main

    with patch(f"{_M}.claim_operator_access", return_value=False), \
         patch.object(main, "is_user_admin", return_value=False), \
         patch.object(main, "get_session_id", return_value=1):
        yield


def _kernel():
    """The `main` MODULE object. Operator audit rows are written by the auth kernel's own binding of
    the audit function, not by the router serving the path, so patching the module OBJECT says which
    of the two bindings is meant (see `test_router_patch_seam.py`).
    """
    from cqc_lem.api import main

    return main


@pytest.fixture
def audit():
    """The kernel's audit binding, captured."""
    with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
        yield recorded


def _session(scope: str):
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _kind(kind: str):
    """The acting account's `account_kind`, as the resolver reads it."""
    return patch(f"{_M}.get_user_account_kind", return_value=kind)


def _events(audit):
    return [(c.args[0], c.kwargs) for c in audit.call_args_list]


class TestOperatorSurface:
    def test_it_is_the_agent_surface_plus_the_reviewed_maintenance_routes(self, main_mod):
        assert main_mod._AGENT_SESSION_SURFACE <= main_mod._OPERATOR_SESSION_SURFACE
        assert (main_mod._OPERATOR_SESSION_SURFACE - main_mod._AGENT_SESSION_SURFACE
                == main_mod._OPERATOR_MAINTENANCE_SURFACE
                == frozenset({"/leads/refresh", "/admin/automation-status",
                              "/admin/automation-pause"}))

    @pytest.mark.parametrize("path,verb", [
        ("/connection_request", "POST"), ("/connection_request", "PUT"), ("/dms", "GET"),
        ("/leads/refresh", "POST"), ("/admin/automation-status", "GET"),
        ("/admin/automation-pause", "POST"), ("/admin/task-status/abc-123", "GET"),
    ])
    def test_the_reachable_routes(self, main_mod, path, verb):
        key = main_mod._scope_path("/api" + path)
        assert main_mod._scope_allows(main_mod.SESSION_SCOPE_OPERATOR, key, verb)

    @pytest.mark.parametrize("path", [
        "/admin/automation-resume",        # re-starts what a human may have halted — secret only
        "/admin/fix-video-urls", "/admin/user/location", "/admin/regenerate-carousel",
        "/admin/regenerate-video", "/admin/generate-media-variants", "/admin/youtube-token",
        "/admin/consolidate-duplicate-comments", "/admin/test/comment", "/admin/test/dm",
        "/admin/test/dm-direct", "/admin/test/reply", "/admin/users", "/admin/audit-log",
        "/admin/feedback", "/admin/task-status", "/admin/task-status/a/b", "/admin/task-status/",
        "/user/linkedin-cookie", "/user/linkedin-password", "/user/passkeys/register/begin",
        "/user/totp/enroll/begin", "/user/recovery-codes/regenerate", "/user/email/change/init",
        "/user/sessions/revoke", "/user/extension-token", "/user/agent-token",
        "/user/observer-token", "/user/operator-token", "/user/security",
        "/user/linkedin-profile/refresh", "/leads/export", "/leads/refresh/all",
    ])
    @pytest.mark.parametrize("verb", ["GET", "POST", "PUT", "DELETE"])
    def test_everything_else_is_closed_on_every_verb(self, main_mod, path, verb):
        key = main_mod._scope_path("/api" + path)
        assert not main_mod._scope_allows(main_mod.SESSION_SCOPE_OPERATOR, key, verb), (verb, path)

    def test_the_task_status_pattern_opens_exactly_one_segment(self, main_mod):
        scope = main_mod.SESSION_SCOPE_OPERATOR
        assert main_mod._scope_allows(scope, "/admin/task-status/9f2c", "GET")
        for bad in ("/admin/task-status", "/admin/task-status/", "/admin/task-status/a/b",
                    "/admin/task-statusX/a", "/x/admin/task-status/a"):
            assert not main_mod._scope_allows(scope, bad, "GET"), bad

    def test_the_other_scopes_never_inherit_the_pattern_or_the_maintenance_routes(self, main_mod):
        for scope in (main_mod.SESSION_SCOPE_AGENT, main_mod.SESSION_SCOPE_OBSERVER):
            for path in ("/admin/task-status/abc", "/admin/automation-status", "/leads/refresh"):
                for verb in ("GET", "POST"):
                    assert not main_mod._scope_allows(scope, path, verb), (scope, verb, path)

    def test_the_scope_is_registered_fixed_ttl_and_not_unrestricted(self, main_mod):
        from cqc_lem.platform.db.repositories.auth import FIXED_TTL_SCOPES
        assert main_mod.SESSION_SCOPE_OPERATOR == "operator"
        assert main_mod.SESSION_SCOPE_OPERATOR in main_mod._SCOPE_SURFACES
        assert main_mod.SESSION_SCOPE_OPERATOR in main_mod._SCOPE_REFUSAL_CODE
        assert main_mod.SESSION_SCOPE_OPERATOR not in main_mod._UNRESTRICTED_SCOPES
        assert main_mod.SESSION_SCOPE_OPERATOR in FIXED_TTL_SCOPES
        # Verb-limited is observer-only: an operator needs its write verbs.
        assert main_mod.SESSION_SCOPE_OPERATOR not in main_mod._SCOPE_METHODS

    def test_it_is_exported_from_the_db_facade(self):
        from cqc_lem.utilities import db
        assert db.SESSION_SCOPE_OPERATOR == "operator"
        assert "SESSION_SCOPE_OPERATOR" in db.__all__


class TestApprovalIsTheFourthGuard:
    """An operator approves only where the owning account is internal or sandbox."""

    @staticmethod
    def _approve(api_client, **extra):
        with patch(f"{_M}.insert_connection_request", return_value=77) as insert:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE,
                "status": "approved", **extra})
        return resp, insert

    @pytest.mark.parametrize("kind", ["internal", "sandbox"])
    def test_an_operator_may_approve_on_an_internal_or_sandbox_account(self, api_client, audit, kind):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        with _session("operator"), _kind(kind):
            resp, insert = self._approve(api_client)
        assert resp.status_code == 200
        assert insert.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED

    def test_the_same_request_on_a_standard_account_is_a_403(self, api_client, audit):
        with _session("operator"), _kind("standard"):
            resp, insert = self._approve(api_client)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        insert.assert_not_called()

    @pytest.mark.parametrize("unreadable", ["", "garbage", "INTERNAL", "internal ", None])
    def test_an_unrecognised_kind_is_standard(self, api_client, audit, unreadable):
        """The reader fails closed to 'standard'; the guard additionally only trusts the two names."""
        with _session("operator"), \
             patch(f"{_M}.get_user_account_kind", return_value=unreadable):
            resp, insert = self._approve(api_client)
        assert resp.status_code == 403
        insert.assert_not_called()

    @pytest.mark.parametrize("kind", ["standard", "internal", "sandbox"])
    def test_an_agent_is_refused_on_every_kind(self, api_client, audit, kind):
        """The operator exception must not have loosened the agent."""
        with _session("agent"), _kind(kind):
            resp, insert = self._approve(api_client)
        assert resp.status_code == 403
        insert.assert_not_called()

    @pytest.mark.parametrize("kind", ["standard", "internal", "sandbox"])
    def test_a_real_person_send_never_auto_approves_for_an_operator(self, api_client, audit, kind):
        """The passive default never applies to an operator, on any account kind.

        `connection_request_mode` is `auto_approve` out of the box, and the default speaks for the
        human who left it set, not for the machine. Even on an internal account, naming no status
        queues for review.
        """
        from cqc_lem.utilities.db import ConnectionRequestStatus
        with _session("operator"), _kind(kind), \
             patch(f"{_M}.get_engagement_preferences",
                   return_value={"connection_request_mode": "auto_approve"}), \
             patch(f"{_M}.insert_connection_request", return_value=78) as insert:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE})
        assert resp.status_code == 200
        assert insert.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING

    def test_a_standard_account_operator_behaves_exactly_like_an_agent_when_queueing(
            self, api_client, audit):
        from cqc_lem.utilities.db import ConnectionRequestStatus
        results = {}
        for scope in ("operator", "agent"):
            with _session(scope), _kind("standard"), \
                 patch(f"{_M}.get_engagement_preferences",
                       return_value={"connection_request_mode": "auto_approve"}), \
                 patch(f"{_M}.insert_connection_request", return_value=79) as insert:
                resp = api_client.post("/api/connection_request", json={
                    "session_token": _TOKEN, "recipient_profile_url": _PROFILE})
            results[scope] = (resp.status_code, insert.call_args.kwargs["status"])
        assert results["operator"] == results["agent"] == (200, ConnectionRequestStatus.PENDING)

    def test_an_operator_cannot_configure_the_account_even_on_an_internal_one(
            self, api_client, audit):
        """PUT /user/engagement-preferences sets the approval modes and every cap — no role may."""
        with _session("operator"), _kind("internal"), \
             patch(f"{_USER}.update_engagement_preferences") as upd:
            resp = api_client.put("/api/user/engagement-preferences",
                                  json={"session_token": _TOKEN, "tone": "casual"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_configure"
        upd.assert_not_called()

    def test_a_human_session_is_untouched(self, main_mod):
        token = main_mod._request_session_scope.set(main_mod.SESSION_SCOPE_FULL)
        try:
            main_mod._refuse_agent_approval("approve")
            main_mod._refuse_agent_approved_status("approved")
            assert main_mod._operator_may_approve() is False
        finally:
            main_mod._request_session_scope.reset(token)


class TestEveryOperatorActionIsAudited:
    def test_a_write_by_an_operator_writes_a_row_carrying_the_role(self, api_client, audit):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("operator"), _kind("standard"), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=80):
            api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE},
                headers={"CF-Connecting-IP": "203.0.113.7"})
        rows = [(e, k) for (e, k) in _events(audit) if e == AuthAuditEvent.OPERATOR_ACTION]
        assert len(rows) == 1, "exactly one row per request, however many times the resolver runs"
        _, kwargs = rows[0]
        assert kwargs["user_id"] == _UID
        assert kwargs["ip"] == "203.0.113.7"
        assert kwargs["details"] == {"role": "operator", "method": "POST",
                                     "path": "/connection_request", "account_kind": "standard"}

    def test_a_read_is_not_audited(self, api_client, audit):
        """A row per poll would bury the account's last-20 audit view under machine noise."""
        with _session("operator"), _kind("internal"), \
             patch(f"{_M}.get_connection_requests", return_value={"items": []}):
            resp = api_client.get("/api/connection_requests", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        audit.assert_not_called()

    def test_an_approval_writes_its_own_row_with_the_role(self, api_client, audit):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("operator"), _kind("internal"), \
             patch(f"{_M}.insert_connection_request", return_value=81):
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE, "status": "approved"})
        assert resp.status_code == 200
        approvals = [k for (e, k) in _events(audit) if e == AuthAuditEvent.OPERATOR_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["user_id"] == _UID
        assert approvals[0]["details"] == {"role": "operator", "via": "status",
                                           "path": "/connection_request",
                                           "account_kind": "internal"}

    def test_a_refused_approval_writes_no_approval_row(self, api_client, audit):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("operator"), _kind("standard"), \
             patch(f"{_M}.insert_connection_request", return_value=82):
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE, "status": "approved"})
        assert resp.status_code == 403
        assert AuthAuditEvent.OPERATOR_APPROVAL not in [e for (e, _k) in _events(audit)]

    def test_a_failed_audit_write_never_turns_an_action_into_a_500(self, api_client):
        with _session("operator"), _kind("standard"), \
             patch.object(_kernel(), "record_auth_event", side_effect=RuntimeError("db down")), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=83):
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE})
        assert resp.status_code == 200

    def test_other_scopes_write_no_operator_rows(self, api_client, audit):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("agent"), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=84):
            api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE})
        assert not [e for (e, _k) in _events(audit)
                    if e in (AuthAuditEvent.OPERATOR_ACTION, AuthAuditEvent.OPERATOR_APPROVAL)]


class TestAccountKindReader:
    def _read(self, fake_cursor, row=None, error=None):
        from cqc_lem.utilities import db
        conn, _cursor = fake_cursor(fetch_one=row, execute_error=error)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            return db.get_user_account_kind(7)

    @pytest.mark.parametrize("stored,expected", [
        ("internal", "internal"), ("sandbox", "sandbox"), ("standard", "standard"),
        ("INTERNAL", "standard"), ("", "standard"), (None, "standard"), ("admin", "standard"),
    ])
    def test_only_the_two_names_grant_anything(self, fake_cursor, stored, expected):
        assert self._read(fake_cursor, row={"account_kind": stored}) == expected

    def test_a_missing_row_is_standard(self, fake_cursor):
        assert self._read(fake_cursor, row=None) == "standard"

    def test_a_database_fault_is_standard_not_internal(self, fake_cursor):
        import mysql.connector
        assert self._read(fake_cursor, error=mysql.connector.Error("down")) == "standard"

    def test_the_column_default_is_standard_so_nobody_changes_behaviour(self):
        """The migration is the one place the default lives; read it rather than trust a comment."""
        import glob
        import os

        root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "compose", "local",
                            "database", "migrations")
        files = glob.glob(os.path.join(root, "V*__add_users_account_kind.sql"))
        assert len(files) == 1
        sql = open(files[0]).read()
        assert "account_kind VARCHAR(16) NOT NULL DEFAULT 'standard'" in sql
        assert "ROLLBACK" in sql


class TestOperatorAdminRoutes:
    """The role-based allow on the chosen admin routes. `X-Admin-Secret` stays the break-glass."""

    _SECRET = "break-glass"

    @pytest.fixture(autouse=True)
    def _secret(self):
        with patch(f"{_ADMIN}.ADMIN_SECRET", self._SECRET):
            yield

    def test_an_operator_reads_automation_status_without_the_secret(self, api_client, audit):
        with _session("operator"), _kind("internal"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.automation_pause_remaining",
                   return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining",
                   return_value=0):
            resp = api_client.get("/api/admin/automation-status", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["paused"] is False

    def test_an_operator_may_pause_and_the_action_is_audited_with_the_role(self, api_client, audit):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("operator"), _kind("internal"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.pause_automation",
                   return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause",
                                   params={"session_token": _TOKEN, "hours": 1})
        assert resp.status_code == 200
        pause.assert_called_once()
        row = [k for (e, k) in _events(audit) if e == AuthAuditEvent.OPERATOR_ACTION]
        assert row and row[0]["details"]["role"] == "operator"
        assert row[0]["details"]["path"] == "/admin/automation-pause"

    def test_an_operator_cannot_resume_automation(self, api_client, audit):
        with _session("operator"), _kind("internal"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.resume_automation") as resume:
            resp = api_client.post("/api/admin/automation-resume",
                                   params={"session_token": _TOKEN})
        assert resp.status_code == 403
        resume.assert_not_called()

    @pytest.mark.parametrize("route,body", [
        ("/api/admin/fix-video-urls", {"old_base": "https://a", "new_base": "https://b"}),
        ("/api/admin/generate-media-variants", {"text": "hello"}),
    ])
    def test_an_operator_cannot_reach_any_other_secret_route(self, api_client, audit, route, body):
        """Valid bodies, so a 403 is the gate speaking and not the validator."""
        with _session("operator"), _kind("internal"):
            resp = api_client.post(route, params={"session_token": _TOKEN}, json=body)
        assert resp.status_code == 403

    def test_an_operator_reads_task_status_without_the_pair(self, api_client, audit):
        class _Result:
            state = "SUCCESS"
            result = "ok"

            @staticmethod
            def ready():
                return True

        with _session("operator"), _kind("internal"), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            resp = api_client.get("/api/admin/task-status/abc-123", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["state"] == "SUCCESS"

    @pytest.mark.parametrize("scope", ["agent", "observer", "full"])
    def test_no_other_session_gets_in_without_the_secret(self, api_client, audit, scope):
        """`full` is a signed-in, non-admin human: a session is not admin."""
        with _session(scope), _kind("internal"):
            resp = api_client.get("/api/admin/automation-status", params={"session_token": _TOKEN})
        assert resp.status_code == 403

    def test_no_credential_at_all_is_refused(self, api_client):
        resp = api_client.get("/api/admin/automation-status")
        assert resp.status_code == 403

    def test_the_secret_still_works_exactly_as_before(self, api_client):
        with patch("cqc_lem.utilities.linkedin.rate_limit.automation_pause_remaining",
                   return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining",
                   return_value=0):
            ok = api_client.get("/api/admin/automation-status",
                                headers={"X-Admin-Secret": self._SECRET})
            bad = api_client.get("/api/admin/automation-status",
                                 headers={"X-Admin-Secret": "wrong"})
        assert ok.status_code == 200
        assert bad.status_code == 403

    def test_a_valid_secret_is_not_defeated_by_a_stray_session_token(self, api_client):
        """An agent token alongside the secret is refused by the resolver; the secret still wins."""
        with _session("agent"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.automation_pause_remaining",
                   return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining",
                   return_value=0):
            resp = api_client.get("/api/admin/automation-status",
                                  params={"session_token": _TOKEN},
                                  headers={"X-Admin-Secret": self._SECRET})
        assert resp.status_code == 200


class TestOperatorCannotMintOrTouchCredentials:
    @pytest.mark.parametrize("verb,path,body", [
        ("post", "/api/user/operator-token", {}),
        ("post", "/api/user/agent-token", {}),
        ("post", "/api/user/observer-token", {}),
        ("post", "/api/user/extension-token", {}),
        ("post", "/api/user/linkedin-cookie", {"li_at": "a" * 40}),
        ("put", "/api/user/linkedin-password", {"linkedin_password": "x"}),
        ("post", "/api/user/passkeys/register/begin", {}),
        ("post", "/api/user/totp/enroll/begin", {}),
        ("post", "/api/user/recovery-codes/regenerate", {}),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
    ])
    @pytest.mark.parametrize("kind", ["standard", "internal"])
    def test_refused_before_the_handler_on_every_account_kind(
            self, api_client, audit, verb, path, body, kind):
        with _session("operator"), _kind(kind), \
             patch(f"{_USER}.create_session") as create_session, \
             patch(f"{_USER}.revoke_other_sessions") as revoke, \
             patch(f"{_USER}.create_pin_for_email") as create_pin, \
             patch(f"{_USER}.store_linkedin_li_at") as store:
            resp = getattr(api_client, verb)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403, f"{verb.upper()} {path} -> {resp.status_code}"
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        for must_not_run in (create_session, revoke, create_pin, store):
            must_not_run.assert_not_called()


class TestOperatorTokenTTL:
    def test_an_operator_session_expiry_is_never_slid(self, fake_cursor):
        from datetime import datetime, timezone

        from cqc_lem.utilities import db

        conn, cursor = fake_cursor(fetch_one={
            "user_id": 7, "scope": db.SESSION_SCOPE_OPERATOR,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            resolved = db.resolve_session("a" * 64)
        updates = [c[0][0] for c in cursor.execute.call_args_list if "UPDATE" in c[0][0].upper()]
        assert resolved == {"user_id": 7, "scope": "operator"}
        assert len(updates) == 1
        assert "expires_at" not in updates[0]
        assert "last_seen_at" in updates[0]


class TestOperatorTokenMint:
    def test_minting_requires_a_session(self, main_mod, user_mod):
        from fastapi import HTTPException
        with patch.object(main_mod, "get_session_user_id", return_value=None):
            with pytest.raises(HTTPException) as exc:
                user_mod.mint_operator_token(user_mod.OperatorTokenRequest(session_token="nope"))
        assert exc.value.status_code == 401

    def test_ttl_defaults_to_30_days_and_is_bounded_at_90(self, user_mod):
        f = user_mod.OperatorTokenRequest.model_fields["ttl_days"]
        assert f.default == 30
        assert "1" in str(f.metadata) and "90" in str(f.metadata)

    def test_minting_is_step_up_gated_and_never_exempts_the_operator_scope(self, user_mod):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(user_mod.mint_operator_token)))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_require_step_up"]
        assert len(calls) == 1
        assert not any(kw.arg == "extension_scope_ok" for kw in calls[0].keywords)
        assert len(calls[0].args) <= 3

    def test_a_mint_creates_a_fixed_ttl_operator_row_and_audits_it(self, api_client):
        from cqc_lem.utilities.db import AuthAuditEvent
        with _session("full"), _kind("internal"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="minted") as create_session, \
             patch(f"{_USER}.record_auth_event") as recorded:
            resp = api_client.post("/api/user/operator-token",
                                   json={"session_token": _TOKEN, "label": "ops", "ttl_days": 14})
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"session_token": "minted", "expires_in_days": 14}
        kwargs = create_session.call_args.kwargs
        assert kwargs["scope"] == "operator"
        assert kwargs["ttl_hours"] == 14 * 24
        assert recorded.call_args[0][0] == AuthAuditEvent.OPERATOR_TOKEN_MINTED
        assert recorded.call_args[0][0] == "operator_token_minted"
        assert recorded.call_args.kwargs["details"]["scope"] == "operator"

    def test_a_mint_without_a_fresh_factor_mints_nothing(self, api_client):
        with _session("full"), _kind("internal"), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post("/api/user/operator-token", json={"session_token": _TOKEN})
        assert resp.status_code == 403
        create_session.assert_not_called()
