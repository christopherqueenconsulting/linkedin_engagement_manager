"""The `mcp` / `mcp_outreach` session scopes: surface, verbs, dispatch binding, field cuts (P2).

`docs/agent-mcp-plan.md` §3.2 and §3.4. The route-table invariants every machine role keeps
(nothing in `FORBIDDEN_ROLE_ROUTES`, no write verb for a read-only role) run for both MCP scopes in
`tests/unit/api/test_role_surface_invariants.py`; this file pins what is particular to them.
"""

import logging
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_header_valid, dispatch_secret

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_UID = 64
_TOKEN = "lem_mcp_" + "scope" * 6
MCP_SCOPES = ("mcp", "mcp_outreach")

# The reviewed read surface, written out: adding a path to either scope is an edit HERE as well,
# made by someone who has read why each one is in.
EXPECTED_MCP_PATHS = frozenset({
    "/user/settings", "/user/automation-status", "/user/linkedin-signin-status",
    "/user/account-readiness", "/dashboard/stats", "/dashboard/planned-tasks", "/posts",
    "/post_url", "/content_generation_status", "/activity", "/user/post-stats",
    "/user/engagement-analytics", "/user/audience-growth", "/user/weekly-report", "/leads/funnel",
    "/user/engagement-preferences", "/user/dm-templates", "/user/story-bank", "/user/lead-magnet",
    "/user/groups", "/user/linkedin-profile-skills", "/user/linkedin-profile",
    "/user/newsletter-settings", "/user/newsletter-draft",
    # P3: counts only (LEM stores no subscriber identities), so `mcp` reads the latest count.
    "/user/newsletter-subscribers",
})
# Everything that holds OTHER people's data (owner ruling O11): opted into at mint, never on `mcp`.
EXPECTED_OUTREACH_EXTRA = frozenset({
    "/dms", "/connection_requests", "/leads", "/lead_signals", "/catchup/touches",
    "/outreach/targets", "/user/engagement-targets", "/user/engagement-suggestions",
})

# Plan §4 "never read" and §5 "never exposed", as (method, path): each must be refused to both.
NEVER_REACHABLE = (
    ("POST", "/user/agent-token"), ("POST", "/user/mcp-token"), ("POST", "/user/mcp-token/rotate"),
    ("POST", "/user/sessions/revoke"), ("POST", "/user/linkedin-cookie"), ("DELETE", "/posts"),
    ("PUT", "/user/engagement-preferences"), ("POST", "/admin/automation-pause"),
    ("POST", "/admin/mcp-kill"), ("GET", "/user/token_status"), ("GET", "/user/security"),
    ("GET", "/user/posthog-stats"), ("GET", "/user/affiliate"), ("GET", "/admin/automation-status"),
    ("GET", "/user/mcp-token/info"), ("GET", "/review/queue"), ("POST", "/schedule_dm"),
    ("POST", "/connection_request"), ("POST", "/schedule_post"), ("POST", "/update_post"),
)


@pytest.fixture(scope="module")
def main_mod() -> Any:
    from cqc_lem.api import main

    return main


@pytest.fixture(scope="module")
def get_routes(main_mod: Any) -> set:
    """Every (scope-path) the app serves for GET."""
    out = set()
    for route in main_mod._walk_routes(main_mod.app.routes):
        path = getattr(route, "path", None)
        if path and "GET" in (getattr(route, "methods", None) or ()):
            out.add(main_mod._scope_path(path))
    return out


class TestTheSurface:
    def test_mcp_reads_exactly_the_reviewed_paths(self, main_mod: Any) -> None:
        assert main_mod._SCOPE_SURFACES["mcp"] == EXPECTED_MCP_PATHS
        assert set(main_mod._SCOPE_PATH_METHODS["mcp"]) == EXPECTED_MCP_PATHS

    def test_mcp_outreach_is_mcp_plus_exactly_the_pii_lists(self, main_mod: Any) -> None:
        assert main_mod._SCOPE_SURFACES["mcp_outreach"] == EXPECTED_MCP_PATHS | EXPECTED_OUTREACH_EXTRA
        assert set(main_mod._SCOPE_PATH_METHODS["mcp_outreach"]) == (
            EXPECTED_MCP_PATHS | EXPECTED_OUTREACH_EXTRA)

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    def test_every_grant_is_get_only(self, main_mod: Any, scope: str) -> None:
        assert {v for v in main_mod._SCOPE_PATH_METHODS[scope].values()} == {frozenset({"GET"})}

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    def test_every_granted_path_is_a_real_get_route(self, main_mod: Any, get_routes: set,
                                                    scope: str) -> None:
        """A grant matching no route is a check that cannot fail, and a tool that cannot work."""
        assert set(main_mod._SCOPE_PATH_METHODS[scope]) - get_routes == set()

    @pytest.mark.parametrize("path", sorted(EXPECTED_OUTREACH_EXTRA))
    def test_mcp_cannot_reach_the_outreach_lists(self, main_mod: Any, path: str) -> None:
        assert main_mod._scope_allows("mcp", path, "GET") is False
        assert main_mod._scope_allows("mcp_outreach", path, "GET") is True

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_no_other_verb_on_a_granted_path(self, main_mod: Any, scope: str, method: str) -> None:
        assert main_mod._scope_allows(scope, "/user/settings", method) is False

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    @pytest.mark.parametrize("method,path", NEVER_REACHABLE)
    def test_the_never_reachable_routes(self, main_mod: Any, scope: str, method: str,
                                        path: str) -> None:
        assert main_mod._scope_allows(scope, path, method) is False

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    def test_a_missing_method_fails_closed(self, main_mod: Any, scope: str) -> None:
        assert main_mod._scope_allows(scope, "/user/settings") is False

    def test_both_scopes_are_wired_into_every_table(self, main_mod: Any) -> None:
        from cqc_lem.platform.db.repositories.auth import FIXED_TTL_SCOPES

        for scope in MCP_SCOPES:
            assert scope in FIXED_TTL_SCOPES
            assert scope in main_mod._QUEUE_ONLY_SCOPES
            assert scope in main_mod._WARN_ON_REFUSAL_SCOPES
            assert main_mod._SCOPE_REFUSAL_CODE[scope] == "session_scope_forbidden"
            assert scope not in main_mod._UNRESTRICTED_SCOPES
            assert scope not in main_mod._SCOPE_METHODS


def _request(headers: dict, method: str = "GET") -> Any:
    return SimpleNamespace(method=method, headers=headers)


@pytest.fixture
def as_request(main_mod: Any) -> Iterator[Any]:
    """Set the request ContextVars `_scope_checked` reads, for a direct call."""
    resets: list = []

    def _set(path: str, headers: dict, method: str = "GET") -> None:
        resets.append((main_mod._request_path, main_mod._request_path.set(path)))
        resets.append((main_mod._request_object,
                       main_mod._request_object.set(_request(headers, method))))
        resets.append((main_mod._request_session_scope,
                       main_mod._request_session_scope.set(None)))

    yield _set
    for var, token in reversed(resets):
        var.reset(token)


class TestTheDispatchBinding:
    def test_the_secret_is_random_per_process_and_compared_exactly(self) -> None:
        secret = dispatch_secret()
        assert len(secret) == 64
        assert dispatch_header_valid(secret) is True
        for wrong in (None, "", secret[:-1], secret + "0", secret.upper(), "é" * 64):
            assert dispatch_header_valid(wrong) is False

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    @pytest.mark.parametrize("headers", [{}, {DISPATCH_HEADER: "nope"}])
    def test_an_mcp_session_without_the_binding_is_refused_before_anything_is_written(
            self, main_mod: Any, as_request: Any, scope: str, headers: dict,
            caplog: pytest.LogCaptureFixture) -> None:
        as_request("/api/user/settings", headers)
        with patch.object(main_mod, "_scope_allows") as allows, \
             patch.object(main_mod, "_safe_auth_event") as audit, \
             patch.object(main_mod, "_record_daily_access") as access, \
             caplog.at_level(logging.WARNING, logger="cqc-lem"), \
             pytest.raises(HTTPException) as refused:
            main_mod._scope_checked({"user_id": _UID, "scope": scope}, _TOKEN)
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == "session_scope_forbidden"
        allows.assert_not_called()
        audit.assert_not_called()
        access.assert_not_called()
        warned = [r for r in caplog.records if "dispatch binding" in r.getMessage()]
        assert [r.levelno for r in warned] == [logging.WARNING]
        assert all(_TOKEN not in str(r.__dict__) for r in caplog.records)

    @pytest.mark.parametrize("scope", MCP_SCOPES)
    def test_with_the_binding_the_ordinary_surface_check_runs(self, main_mod: Any,
                                                              as_request: Any,
                                                              scope: str) -> None:
        as_request("/api/user/settings", {DISPATCH_HEADER: dispatch_secret()})
        assert main_mod._scope_checked({"user_id": _UID, "scope": scope}, _TOKEN) == _UID
        assert main_mod._request_session_scope.get() == scope

    def test_with_the_binding_an_ungranted_path_is_still_refused(
            self, main_mod: Any, as_request: Any, caplog: pytest.LogCaptureFixture) -> None:
        as_request("/api/dms", {DISPATCH_HEADER: dispatch_secret()})
        with caplog.at_level(logging.WARNING, logger="cqc-lem"), \
             pytest.raises(HTTPException) as refused:
            main_mod._scope_checked({"user_id": _UID, "scope": "mcp"}, _TOKEN)
        assert refused.value.status_code == 403
        assert any("Machine session refused outside its scope" in r.getMessage()
                   for r in caplog.records)

    def test_no_request_context_is_no_binding(self, main_mod: Any) -> None:
        token = main_mod._request_object.set(None)
        try:
            assert main_mod._mcp_dispatch_bound() is False
        finally:
            main_mod._request_object.reset(token)

    def test_other_scopes_never_need_the_binding(self, main_mod: Any, as_request: Any) -> None:
        as_request("/api/dms", {})
        with patch.object(main_mod, "_record_observer_access"):
            assert main_mod._scope_checked({"user_id": _UID, "scope": "observer"}, "t") == _UID


class TestRawApiUseIsRefused:
    """The same refusal through the real HTTP path: cookie, query token, and a wrong header."""

    @pytest.fixture(autouse=True)
    def _resolves(self, main_mod: Any) -> Iterator[None]:
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "mcp"} if t == _TOKEN
                   else None), \
             patch(f"{_M}._db_get_session_user_id",
                   side_effect=lambda t: _UID if t == _TOKEN else None), \
             patch.object(main_mod, "record_auth_event") as audit:
            self.audit = audit
            yield

    @pytest.mark.parametrize("how", ["cookie", "query", "wrong_header"])
    def test_an_mcp_token_on_raw_api_is_a_403_with_no_audit_row(self, api_client: Any,
                                                                how: str) -> None:
        # The cookie forms send the SPA's `"cookie"` sentinel, exactly as the P3 loopback will.
        kwargs: dict = {"params": {"session_token": "cookie"}}
        if how == "cookie":
            api_client.cookies.set("lem_session", _TOKEN)
        elif how == "query":
            kwargs["params"] = {"session_token": _TOKEN}
        else:
            api_client.cookies.set("lem_session", _TOKEN)
            kwargs["headers"] = {DISPATCH_HEADER: "0" * 64}
        resp = api_client.get("/api/user/automation-status", **kwargs)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        self.audit.assert_not_called()


class TestFieldCuts:
    ROW = {"id": 1, "user_id": _UID, "recipient_profile_url": "u", "message": "m",
           "status": "pending", "recipient_email": "secret@example.com", "phone": "555"}

    @pytest.mark.parametrize("scope", ["observer", *MCP_SCOPES])
    def test_a_read_only_seat_gets_only_the_whitelisted_fields(self, main_mod: Any,
                                                               scope: str) -> None:
        token = main_mod._request_session_scope.set(scope)
        try:
            rows = main_mod._observer_rows("dms", [dict(self.ROW)])
        finally:
            main_mod._request_session_scope.reset(token)
        assert set(rows[0]) <= main_mod._OBSERVER_ROW_FIELDS["dms"]
        assert "recipient_email" not in rows[0] and "phone" not in rows[0]

    @pytest.mark.parametrize("scope", ["full", "agent", "operator"])
    def test_other_seats_are_untouched(self, main_mod: Any, scope: str) -> None:
        token = main_mod._request_session_scope.set(scope)
        try:
            assert main_mod._observer_rows("dms", [dict(self.ROW)]) == [self.ROW]
        finally:
            main_mod._request_session_scope.reset(token)

    def test_the_mcp_seats_share_the_observer_key_pins(self, main_mod: Any) -> None:
        assert main_mod._FIELD_FILTERED_SCOPES == frozenset({"observer", *MCP_SCOPES})
