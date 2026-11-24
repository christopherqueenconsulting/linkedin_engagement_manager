"""The MCP kill-switch admin route, the two owner scripts, and the mcp rows of W1's guards.

`POST /api/admin/mcp-kill` and `scripts/mcp_kill.py` / `scripts/mcp_revoke_all.py` are plan §3.5 and
§10. The queue-only rows prove the W1 guards (#2684) already hold for both MCP seats, on EVERY
account kind: an MCP seat never gets the operator exemption (owner ruling O1 lands in W2 as its own
branch).
"""

import importlib.util
import io
import pathlib
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[4] / "scripts"
_ADMIN = "cqc_lem.api.routers.admin"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAdminKillRoute:
    @pytest.fixture
    def admin(self) -> Iterator[dict]:
        with patch("cqc_lem.api.main.get_session_user_id", return_value=3) as uid, \
             patch(f"{_ADMIN}.is_user_admin", return_value=True) as is_admin, \
             patch("cqc_lem.api.routers.user.step_up_satisfied", return_value=True) as step_up, \
             patch("cqc_lem.api.routers.user.available_methods", return_value=[]), \
             patch("cqc_lem.api.routers.user.record_auth_event", return_value=True), \
             patch("cqc_lem.api.mcp.limits.set_kill_key", return_value=True) as setter, \
             patch(f"{_ADMIN}.record_auth_event", return_value=True) as audit:
            yield {"uid": uid, "is_admin": is_admin, "step_up": step_up, "set": setter,
                   "audit": audit}

    def _kill(self, client: Any, on: bool) -> Any:
        return client.post("/api/admin/mcp-kill", json={"session_token": "tok", "on": on})

    @pytest.mark.parametrize("on", [True, False])
    def test_an_admin_with_step_up_sets_the_key_and_it_is_audited(self, api_client: Any,
                                                                  admin: dict, on: bool) -> None:
        resp = self._kill(api_client, on)
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"] == {"on": on}
        admin["set"].assert_called_once_with(on)
        event = admin["audit"].call_args
        assert str(event.args[0]) == "mcp_kill_toggled"
        assert event.kwargs["user_id"] == 3 and event.kwargs["details"]["on"] is on

    def test_a_non_admin_is_403_and_nothing_changes(self, api_client: Any, admin: dict) -> None:
        admin["is_admin"].return_value = False
        assert self._kill(api_client, True).status_code == 403
        admin["set"].assert_not_called()

    def test_step_up_is_required(self, api_client: Any, admin: dict) -> None:
        admin["step_up"].return_value = False
        resp = self._kill(api_client, True)
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "step_up_required"
        admin["set"].assert_not_called()

    def test_an_unreachable_redis_is_503_and_unaudited(self, api_client: Any,
                                                       admin: dict) -> None:
        admin["set"].return_value = False
        assert self._kill(api_client, True).status_code == 503
        admin["audit"].assert_not_called()

    def test_no_session_is_401(self, api_client: Any, admin: dict) -> None:
        admin["uid"].return_value = None
        assert self._kill(api_client, True).status_code == 401

    def test_it_is_hidden_from_the_published_schema(self) -> None:
        from cqc_lem.api.main import app

        assert "/api/admin/mcp-kill" not in app.openapi()["paths"]


class TestKillScript:
    def test_on_off_and_status(self) -> None:
        script = _load("mcp_kill")
        with patch.object(script, "set_kill_key", return_value=True) as setter, \
             patch.object(script, "record_auth_event") as audit:
            out = io.StringIO()
            assert script.main(["on"], out=out) == 0
            assert "ON" in out.getvalue()
            assert script.main(["off"], out=io.StringIO()) == 0
        assert [c.args for c in setter.call_args_list] == [(True,), (False,)]
        assert [c.kwargs["details"]["on"] for c in audit.call_args_list] == [True, False]
        assert all(str(c.args[0]) == "mcp_kill_toggled" for c in audit.call_args_list)

    def test_an_unreachable_redis_exits_2_unaudited(self) -> None:
        script = _load("mcp_kill")
        with patch.object(script, "set_kill_key", return_value=False), \
             patch.object(script, "record_auth_event") as audit:
            assert script.main(["on"], out=io.StringIO()) == 2
        audit.assert_not_called()

    @pytest.mark.parametrize("state,code", [("ON", 0), ("OFF", 0), ("UNREADABLE", 2)])
    def test_status(self, state: str, code: int) -> None:
        script = _load("mcp_kill")
        out = io.StringIO()
        with patch.object(script, "read_kill_key", return_value=getattr(script.KillState, state)):
            assert script.main(["status"], out=out) == code
        assert state.lower() in out.getvalue()


class TestRevokeAllScript:
    def test_it_revokes_both_scopes_with_the_reason(self) -> None:
        script = _load("mcp_revoke_all")
        out = io.StringIO()
        with patch.object(script, "revoke_sessions_by_scope", return_value=4) as revoke:
            assert script.main(["--reason", "incident", "--yes"], out=out) == 0
        revoke.assert_called_once_with(frozenset({"mcp", "mcp_outreach"}), reason="incident")
        assert "Revoked 4" in out.getvalue()

    def test_a_failed_transaction_exits_2(self) -> None:
        script = _load("mcp_revoke_all")
        with patch.object(script, "revoke_sessions_by_scope", return_value=None):
            assert script.main(["--reason", "x", "--yes"], out=io.StringIO()) == 2

    def test_it_asks_first_and_a_wrong_answer_revokes_nothing(self) -> None:
        script = _load("mcp_revoke_all")
        with patch.object(script, "revoke_sessions_by_scope") as revoke:
            assert script.main(["--reason", "x"], out=io.StringIO(),
                               confirm=lambda _prompt: "no") == 1
            revoke.assert_not_called()
            revoke.return_value = 0
            assert script.main(["--reason", "x"], out=io.StringIO(),
                               confirm=lambda _prompt: "revoke") == 0

    def test_no_terminal_and_no_yes_refuses(self) -> None:
        script = _load("mcp_revoke_all")
        with patch.object(script, "revoke_sessions_by_scope") as revoke, \
             patch.object(script.sys.stdin, "isatty", return_value=False):
            assert script.main(["--reason", "x"], out=io.StringIO()) == 1
        revoke.assert_not_called()


@pytest.fixture
def main_mod() -> Any:
    from cqc_lem.api import main

    return main


def _as(main_mod: Any, scope: str, kind: str) -> tuple:
    return (main_mod._request_session_scope.set(scope),
            main_mod._request_operator.set({"account_kind": kind, "user_id": 1}))


def _reset(main_mod: Any, resets: tuple) -> None:
    main_mod._request_operator.reset(resets[1])
    main_mod._request_session_scope.reset(resets[0])


class TestTheMcpSeatsAreQueueOnlyOnEveryAccountKind:
    KINDS = ["standard", "internal", "sandbox"]

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    @pytest.mark.parametrize("kind", KINDS)
    def test_held_to_pending_only_and_never_exempt(self, main_mod: Any, scope: str,
                                                   kind: str) -> None:
        resets = _as(main_mod, scope, kind)
        try:
            assert main_mod._agent_scoped() is True
            assert main_mod._operator_may_approve() is False
            assert main_mod._edits_pending_only() is True
        finally:
            _reset(main_mod, resets)

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("guard,arg", [("_refuse_agent_approval", "approve"),
                                           ("_refuse_agent_approval", "retry"),
                                           ("_refuse_agent_approved_status", "approved")])
    def test_every_approval_is_a_403_with_no_operator_audit(self, main_mod: Any, scope: str,
                                                            kind: str, guard: str,
                                                            arg: str) -> None:
        resets = _as(main_mod, scope, kind)
        try:
            with patch.object(main_mod, "_audit_operator_approval") as audit, \
                 pytest.raises(HTTPException) as refused:
                getattr(main_mod, guard)(arg)
        finally:
            _reset(main_mod, resets)
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == "agent_may_not_approve"
        audit.assert_not_called()

    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_a_non_linkedin_profile_url_is_refused(self, main_mod: Any, scope: str) -> None:
        resets = _as(main_mod, scope, "internal")
        try:
            with pytest.raises(HTTPException) as refused:
                main_mod._linkedin_profile_url_for_seat("https://example.com/in/jane")
            assert main_mod._linkedin_profile_url_for_seat(
                "https://linkedin.com/in/jane") == "https://www.linkedin.com/in/jane/"
        finally:
            _reset(main_mod, resets)
        assert refused.value.status_code == 422
        assert refused.value.detail["code"] == "linkedin_profile_url_required"

    def test_mcp_is_never_wired_into_the_operator_exemption(self, main_mod: Any) -> None:
        import inspect

        source = inspect.getsource(main_mod._operator_may_approve)
        assert "MCP" not in source and "mcp" not in source


class TestThePreferencesReadForAnMcpSeat:
    """Through the REAL resolver with the dispatch binding: no inbound address, none minted."""

    def test_no_address_and_none_created(self, api_client: Any) -> None:
        from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret

        user = "cqc_lem.api.routers.user"
        token = "lem_mcp_" + "prefs" * 6
        mint = MagicMock(return_value="tok9")
        with patch("cqc_lem.api.main._db_resolve_session",
                   side_effect=lambda t: {"user_id": 81, "scope": "mcp"} if t == token
                   else None), \
             patch(f"{user}.get_engagement_preferences",
                   return_value={"reply_check_mode": "event"}), \
             patch(f"{user}.has_engagement_preferences", return_value=True), \
             patch(f"{user}.get_or_create_reply_inbound_token", mint), \
             patch("cqc_lem.api.main.get_gmail_forward_confirmation", return_value=None), \
             patch(f"{user}.max_catchup_touches_allowed", return_value=3), \
             patch("cqc_lem.app.engagement.feed.get_feed_funnel", return_value=None), \
             patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            resp = api_client.get("/api/user/engagement-preferences",
                                  params={"session_token": token},
                                  headers={DISPATCH_HEADER: dispatch_secret()})
        assert resp.status_code == 200, resp.text
        assert "reply_inbound_address" not in resp.json()["detail"]
        mint.assert_not_called()
