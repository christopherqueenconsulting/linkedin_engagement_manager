"""`lem_add_story_draft` end to end through the gate, SDK, registry, loopback and route (#2804).

The entry lands UNVERIFIED and only ever as a CREATE: the grants give an MCP seat `POST` on the draft
path and `GET` on the bank, nothing that edits, confirms or deletes.
"""

from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api import main
from cqc_lem.api.mcp import tools as registry
from cqc_lem.api.mcp.tools_story import STORY_BODY_MAX, STORY_TITLE_MAX, UNVERIFIED_NOTE
from cqc_lem.api.routers.user import _LEN_STORY_BODY, _LEN_STORY_TITLE
from cqc_lem.utilities.db import MAX_UNVERIFIED_STORY_DRAFTS, AuthAuditEvent

from ._admitted import UID, admitted, call, structured

pytestmark = pytest.mark.unit

_W = "cqc_lem.api.mcp.writes"
_R = "cqc_lem.api.routers.mcp_story"
TOOL = "lem_add_story_draft"
_ARGS = {"kind": "client_win", "title": "Saved a launch", "body": "We shipped on time for Acme.",
         "happened_at": "2026-03-01"}


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")
    monkeypatch.setenv("MCP_STORY_DRAFTS_ENABLED", "true")


@pytest.fixture
def audit() -> Iterator[MagicMock]:
    with patch(f"{_W}.record_auth_event", return_value=True) as mock:
        yield mock


@pytest.fixture(params=["mcp", "mcp_outreach"])
def seat(request: Any, resolver: MagicMock, audit: MagicMock) -> Iterator[MagicMock]:
    with admitted(resolver, request.param) as inner, \
            patch(f"{_W}.get_user_account_kind", return_value="standard"):
        yield inner


@pytest.fixture
def db() -> Iterator[dict[str, MagicMock]]:
    with patch(f"{_R}.count_unverified_story_drafts", return_value=0) as count, \
            patch(f"{_R}.insert_story_draft", return_value=61) as insert:
        yield {"count": count, "insert": insert}


def _error(result: dict) -> dict:
    assert result["isError"] is True, result
    return structured(result)["error"]


class TestTheTool:
    def test_both_scopes_list_it_and_it_is_a_write(self) -> None:
        spec = registry.TOOLS_BY_NAME[TOOL]
        assert spec.scopes == registry.BOTH_SCOPES and spec.write and not spec.approval
        assert spec.calls == (("POST", "/user/story-bank/draft"),)
        assert spec.flag == "mcp-story-drafts"
        assert registry.routed_through_run_write(spec)

    def test_the_description_says_unverified_and_only_what_the_user_told_it(self) -> None:
        text = registry.TOOLS_BY_NAME[TOOL].description
        assert "UNVERIFIED" in text and "actually told you" in text and "FACT" in text

    def test_it_creates_one_unverified_entry_and_audits_its_own_event(
            self, api_client: Any, seat: MagicMock, db: dict, audit: MagicMock) -> None:
        body = structured(call(api_client, TOOL, _ARGS))
        assert body["id"] == 61 and body["verified"] is False and body["status"] == "unverified"
        assert body["note"] == UNVERIFIED_NOTE and body["review_url"].endswith("/account?section=content")
        db["insert"].assert_called_once_with(UID, "client_win", "Saved a launch",
                                             "We shipped on time for Acme.", "2026-03-01")
        event = audit.call_args.args[0]
        assert event is AuthAuditEvent.MCP_STORY_DRAFT and event.value == "mcp_story_draft"
        assert audit.call_args.kwargs["details"]["tool"] == TOOL
        assert "Acme" not in str(audit.call_args)  # never the body

    def test_it_takes_no_id_status_or_active_argument(self) -> None:
        fields = set(registry.TOOLS_BY_NAME[TOOL].args_model.model_fields)
        assert fields == {"client_request_id", "kind", "title", "body", "happened_at"}

    @pytest.mark.parametrize("extra", [{"id": 4}, {"active": False}, {"verified": True},
                                       {"kind": "bogus"}, {"body": ""}, {"happened_at": "March"}])
    def test_bad_or_addressing_arguments_are_refused_before_any_call(
            self, api_client: Any, seat: MagicMock, db: dict, extra: dict) -> None:
        result = call(api_client, TOOL, {**_ARGS, **extra})
        assert _error(result)["code"] == "invalid_arguments"
        db["insert"].assert_not_called()

    def test_argument_caps_match_the_spa_validation(self) -> None:
        assert (STORY_TITLE_MAX, STORY_BODY_MAX) == (_LEN_STORY_TITLE, _LEN_STORY_BODY)

    def test_the_same_request_id_replays_without_a_second_entry(
            self, api_client: Any, seat: MagicMock, db: dict, audit: MagicMock) -> None:
        first = call(api_client, TOOL, {**_ARGS, "client_request_id": "story-1"})
        second = call(api_client, TOOL, {**_ARGS, "client_request_id": "story-1"})
        assert db["insert"].call_count == 1 and audit.call_count == 1
        assert structured(second)["id"] == structured(first)["id"] and structured(second)["replayed"]


class TestRefusals:
    def test_the_story_flag_off_refuses_before_any_budget_or_write(
            self, api_client: Any, seat: MagicMock, db: dict, monkeypatch: pytest.MonkeyPatch,
            fake_redis: Any) -> None:
        monkeypatch.setenv("MCP_STORY_DRAFTS_ENABLED", "false")
        result = call(api_client, TOOL, _ARGS)
        assert _error(result)["code"] == "unknown_tool"
        db["insert"].assert_not_called()
        spent = [k for k in fake_redis.store if k.startswith("mcp:budget:writes")
                 or k.startswith("mcp:dedupe:")]
        assert spent == [], fake_redis.store

    def test_the_writes_flag_off_refuses_too(self, api_client: Any, seat: MagicMock, db: dict,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")
        assert _error(call(api_client, TOOL, _ARGS))["code"] == "writes_disabled"
        db["insert"].assert_not_called()

    def test_the_route_checks_the_flag_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enforcement lives in server code: the route refuses even if the tool layer were bypassed."""
        from fastapi import HTTPException

        from cqc_lem.api.routers import mcp_story as route

        monkeypatch.setenv("MCP_STORY_DRAFTS_ENABLED", "false")
        with patch.object(route._main, "require_session_user_id", return_value=UID), \
                patch.object(route._main, "_request_session_scope") as scope, \
                patch(f"{_R}.insert_story_draft") as insert:
            scope.get.return_value = "mcp"
            with pytest.raises(HTTPException) as exc:
                route.add_story_draft(route.StoryDraftRequest(body="b"))
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "mcp_story_drafts_disabled"
        insert.assert_not_called()

    def test_a_non_mcp_session_is_refused(self) -> None:
        from fastapi import HTTPException

        from cqc_lem.api.routers import mcp_story as route

        with patch.object(route._main, "require_session_user_id", return_value=UID), \
                patch.object(route._main, "_request_session_scope") as scope, \
                patch(f"{_R}.insert_story_draft") as insert:
            scope.get.return_value = "full"
            with pytest.raises(HTTPException) as exc:
                route.add_story_draft(route.StoryDraftRequest(body="b"))
        assert exc.value.detail["code"] == "mcp_only"
        insert.assert_not_called()

    def test_too_many_waiting_drafts_is_a_409(self, api_client: Any, seat: MagicMock,
                                              db: dict) -> None:
        db["count"].return_value = MAX_UNVERIFIED_STORY_DRAFTS
        result = call(api_client, TOOL, _ARGS)
        error = _error(result)
        assert error["code"] == "too_many_unverified_drafts", error
        db["insert"].assert_not_called()

    def test_an_unreadable_bank_fails_closed(self, api_client: Any, seat: MagicMock,
                                             db: dict) -> None:
        db["count"].return_value = None
        assert _error(call(api_client, TOOL, _ARGS))["code"] == "mcp_unavailable"
        db["insert"].assert_not_called()


class TestRoutePolicy:
    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_an_agent_can_read_and_create_but_never_edit_confirm_or_delete(self, scope: str) -> None:
        grants = main._SCOPE_PATH_METHODS[scope]
        assert grants["/user/story-bank"] == frozenset({"GET"})
        assert grants["/user/story-bank/draft"] == frozenset({"POST"})
        assert "/user/story-bank/confirm" not in grants
        assert "/user/story-bank/confirm" not in main._SCOPE_SURFACES[scope]

    def test_the_confirm_route_exists_for_the_signed_in_user_only(self) -> None:
        paths = {(m, r.path) for r in main._walk_routes(main.app.routes)
                 for m in (getattr(r, "methods", None) or ())}
        assert ("POST", "/api/user/story-bank/confirm") in paths
        assert ("POST", "/api/user/story-bank/draft") in paths


class TestConfirmRouteIsHumanOnly:
    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach", "agent", "observer", "operator"])
    def test_a_machine_scope_is_refused_and_confirms_nothing(self, scope: str) -> None:
        from fastapi import HTTPException

        from cqc_lem.api.routers import user as route

        with patch.object(route._main, "get_session_user_id", return_value=UID), \
                patch.object(route._main, "_request_session_scope") as current, \
                patch.object(route, "confirm_story_bank_entry") as confirm:
            current.get.return_value = scope
            with pytest.raises(HTTPException) as exc:
                route.confirm_story_bank_endpoint(
                    route.StoryBankConfirmRequest(session_token="t", entry_id=3))
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "human_only"
        confirm.assert_not_called()

    def test_a_full_session_confirms(self) -> None:
        from cqc_lem.api.routers import user as route

        with patch.object(route._main, "get_session_user_id", return_value=UID), \
                patch.object(route._main, "_request_session_scope") as current, \
                patch.object(route, "confirm_story_bank_entry", return_value=True) as confirm:
            current.get.return_value = "full"
            route.confirm_story_bank_endpoint(
                route.StoryBankConfirmRequest(session_token="t", entry_id=3))
        confirm.assert_called_once_with(UID, 3)
