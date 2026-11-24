"""The W2 write tools, end to end through the real gate, SDK, registry, loopback and routes.

`docs/agent-mcp-plan.md` §4 "Write tools", §5, §7 and §9 "W2 tests", plus owner ruling O1. Every
test posts JSON-RPC to `/api/mcp`, so each inner write really runs `_scope_checked` with the dispatch
binding and the real route handler, with its approval guards and pending-only edits; only the
handlers' own database calls are patched.
"""

import json
import logging
import time
from contextlib import ExitStack
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import tools as registry, writes
from cqc_lem.api.mcp.tools_write import PENDING_NOTE
from cqc_lem.utilities.db import (
    AuthAuditEvent,
    ConnectionRequestStatus,
    OutreachStatus,
    PendingEdit,
    PostStatus,
    PostType,
    ScheduledDmStatus,
)

from ._admitted import TOKEN, UID, admitted, call, rpc, structured

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_O = "cqc_lem.api.routers.outreach"
_W = "cqc_lem.api.mcp.writes"
_URL = "https://www.linkedin.com/in/jane-doe-42/"
_WHEN = "2099-03-01T15:00:00+00:00"
_BODY_CANARY = "zz-body-canary-73"

WRITE_NAMES = {"lem_draft_post", "lem_revise_post_draft", "lem_queue_dm",
               "lem_queue_connection_request", "lem_add_outreach_target",
               "lem_edit_lead_signal_draft", "lem_edit_catchup_draft", "lem_update_lead",
               "lem_approve_item"}


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    """Both rollout flags on (env fallback) and the in-memory Redis behind every limit."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")


@pytest.fixture
def audit() -> Iterator[MagicMock]:
    """The `mcp_write` audit write."""
    with patch(f"{_W}.record_auth_event", return_value=True) as mock:
        yield mock


@pytest.fixture
def outreach(resolver: MagicMock, audit: MagicMock) -> Iterator[MagicMock]:
    """An admitted `mcp_outreach` token on a STANDARD account (the default for every account)."""
    with admitted(resolver, "mcp_outreach") as inner, \
            patch(f"{_W}.get_user_account_kind", return_value="standard"), \
            patch(f"{_M}.get_user_account_kind", return_value="standard"):
        yield inner


@pytest.fixture
def mcp(resolver: MagicMock, audit: MagicMock) -> Iterator[MagicMock]:
    """An admitted `mcp` token on a standard account."""
    with admitted(resolver, "mcp") as inner, \
            patch(f"{_W}.get_user_account_kind", return_value="standard"):
        yield inner


def _dm_args(**extra: Any) -> dict:
    return {"recipient_profile_url": _URL, "recipient_name": "Jane", "message": "Hi " + _BODY_CANARY,
            "scheduled_datetime": _WHEN, **extra}


def _error(result: dict) -> dict:
    assert result["isError"] is True, result
    return structured(result)["error"]


class TestTheWriteRegistry:
    def test_the_write_tools_are_exactly_the_plan_s(self) -> None:
        assert {s.name for s in registry.TOOLS if s.write} == WRITE_NAMES

    def test_only_lem_approve_item_approves_and_it_is_outreach_only(self) -> None:
        approvals = [s for s in registry.TOOLS if s.approval]
        assert [s.name for s in approvals] == ["lem_approve_item"]
        assert approvals[0].scopes == registry.OUTREACH_ONLY

    def test_post_drafting_is_on_both_scopes_and_outreach_writes_are_not(self) -> None:
        both = {s.name for s in registry.TOOLS if s.write and s.scopes == registry.BOTH_SCOPES}
        assert both == {"lem_draft_post", "lem_revise_post_draft"}

    def test_every_write_tool_takes_a_client_request_id(self) -> None:
        for spec in registry.TOOLS:
            if spec.write:
                assert "client_request_id" in spec.args_model.model_fields, spec.name

    def test_the_post_draft_has_no_media_type_or_status(self) -> None:
        fields = set(registry.TOOLS_BY_NAME["lem_draft_post"].args_model.model_fields)
        assert fields == {"content", "scheduled_datetime", "client_request_id"}

    def test_approve_never_names_a_post(self) -> None:
        kinds = registry.TOOLS_BY_NAME["lem_approve_item"].args_model.model_fields["kind"].annotation
        assert "post" not in kinds.__args__
        assert set(kinds.__args__) == {"dm", "connection_request", "lead_signal",
                                        "catchup_touch", "outreach_target"}

    @pytest.mark.parametrize("url", ["https://evil.example/in/jane", "http://www.linkedin.com/in/j",
                                     "https://www.linkedin.com/company/acme",
                                     "https://www.linkedin.com/in/jane/extra"])
    def test_a_recipient_must_be_a_linkedin_profile_url(self, url: str) -> None:
        from pydantic import ValidationError

        model = registry.TOOLS_BY_NAME["lem_queue_dm"].args_model
        with pytest.raises(ValidationError):
            model.model_validate(_dm_args(recipient_profile_url=url))


class TestEveryWriteLandsPending:
    def test_lem_queue_dm_inserts_a_pending_mcp_row(self, api_client: Any, outreach: MagicMock,
                                                    audit: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", return_value=501) as insert:
            result = call(api_client, "lem_queue_dm", _dm_args())
        assert result["isError"] is False, result
        body = structured(result)
        assert body["id"] == 501 and body["status"] == "pending" and body["source"] == "mcp"
        assert body["note"] == PENDING_NOTE
        assert body["review_url"].endswith("/content?tab=dms")
        assert insert.call_args.kwargs["status"] == ScheduledDmStatus.PENDING
        assert insert.call_args.kwargs["source"] == "mcp"
        audit.assert_called_once()
        assert audit.call_args.args[0] == AuthAuditEvent.MCP_WRITE
        assert audit.call_args.kwargs["details"] == {"tool": "lem_queue_dm", "target_id": 501,
                                                     "scope": "mcp_outreach"}
        assert _BODY_CANARY not in json.dumps(audit.call_args.kwargs)

    def test_a_connection_request_is_pending_mcp_whatever_the_account_mode(
            self, api_client: Any, outreach: MagicMock) -> None:
        with patch(f"{_M}.insert_connection_request", return_value=88) as insert, \
                patch(f"{_M}.get_engagement_preferences",
                      return_value={"connection_request_mode": "auto_approve"}):
            result = call(api_client, "lem_queue_connection_request",
                          {"recipient_profile_url": _URL, "message": "Hello"})
        assert structured(result)["id"] == 88
        assert insert.call_args.kwargs["status"] == ConnectionRequestStatus.PENDING
        assert insert.call_args.kwargs["source"] == "mcp"

    def test_an_outreach_target_is_created_pending(self, api_client: Any,
                                                   outreach: MagicMock) -> None:
        with patch(f"{_O}.get_outreach_target_by_profile", return_value=None), \
                patch(f"{_O}.insert_outreach_target", return_value=9) as insert:
            result = call(api_client, "lem_add_outreach_target",
                          {"target_profile_url": _URL, "draft_text": "Nice post",
                           "context_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/"})
        assert structured(result)["id"] == 9
        assert insert.call_args.kwargs["status"] == OutreachStatus.PENDING

    def test_a_lead_signal_draft_is_a_pending_only_edit(self, api_client: Any,
                                                        outreach: MagicMock) -> None:
        with patch(f"{_M}.get_lead_signal", return_value={"user_id": UID, "draft_response": "a"}), \
                patch(f"{_M}.update_pending_row", return_value=PendingEdit.UPDATED) as edit, \
                patch(f"{_M}.update_lead_signal") as human_path:
            result = call(api_client, "lem_edit_lead_signal_draft",
                          {"signal_id": 4, "draft_response": "Thanks for asking"})
        assert structured(result)["status"] == "pending"
        edit.assert_called_once_with("lead_signal", 4, UID, {"draft_response": "Thanks for asking"})
        human_path.assert_not_called()

    def test_a_catchup_draft_is_a_pending_only_edit(self, api_client: Any,
                                                    outreach: MagicMock) -> None:
        with patch(f"{_M}.get_catchup_touch", return_value={"user_id": UID, "message": "x"}), \
                patch(f"{_M}.update_pending_row", return_value=PendingEdit.UPDATED) as edit:
            result = call(api_client, "lem_edit_catchup_draft", {"touch_id": 3, "message": "Congrats"})
        assert result["isError"] is False
        edit.assert_called_once_with("catchup_touch", 3, UID, {"message": "Congrats"})

    def test_an_edit_on_a_row_that_left_pending_is_409(self, api_client: Any,
                                                       outreach: MagicMock) -> None:
        with patch(f"{_M}.get_catchup_touch", return_value={"user_id": UID, "message": "x"}), \
                patch(f"{_M}.update_pending_row", return_value=PendingEdit.NOT_PENDING):
            result = call(api_client, "lem_edit_catchup_draft", {"touch_id": 3, "message": "Hi"})
        assert _error(result)["code"] == "edit_requires_pending"

    def test_a_lead_update_dismisses_without_any_action_argument(self, api_client: Any,
                                                                 outreach: MagicMock) -> None:
        with patch(f"{_M}.get_lead", return_value={"user_id": UID}), \
                patch(f"{_M}.update_lead", return_value=True) as update:
            result = call(api_client, "lem_update_lead", {"lead_id": 2, "dismissed": True,
                                                          "notes": "met at conf"})
        assert structured(result)["updated"] is True
        assert update.call_args.kwargs == {"notes": "met at conf", "manual_stage": None,
                                           "dismissed": True, "deal_value": None}

    def test_lem_draft_post_is_a_pending_text_post(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.get_user_email", return_value="me@example.com"), \
                patch(f"{_M}.insert_post_returning_id", return_value=33) as insert:
            result = call(api_client, "lem_draft_post", {"content": "A post", "scheduled_datetime": _WHEN})
        body = structured(result)
        assert body["id"] == 33 and body["status"] == "pending" and body["post_type"] == "text"
        args, kwargs = insert.call_args
        assert args[3] == PostType.TEXT
        assert kwargs["status"] == PostStatus.PENDING
        assert kwargs["image_url"] is None and kwargs["use_avatar"] is None

    def test_lem_revise_post_draft_reads_the_slot_back_and_edits_pending_only(
            self, api_client: Any, mcp: MagicMock) -> None:
        row = {"id": 33, "content": "old", "scheduled_time": "2099-03-01 15:00:00",
               "status": "pending", "post_type": "text", "curated_source_id": None}
        with patch(f"{_M}._require_own_posts"), \
                patch(f"{_M}._post_row", return_value={"post_id": 33, "content": "old",
                                                       "scheduled_time": _WHEN}), \
                patch(f"{_M}.get_post_for_user", return_value=row), \
                patch(f"{_M}.update_pending_row", return_value=PendingEdit.UPDATED) as edit, \
                patch(f"{_M}.update_db_post") as human_path:
            result = call(api_client, "lem_revise_post_draft", {"post_id": 33, "content": "new"})
        assert structured(result)["status"] == "pending"
        kind, post_id, user_id, fields = edit.call_args.args
        assert (kind, post_id, user_id, fields["content"]) == ("post", 33, UID, "new")
        assert set(fields) == {"content", "scheduled_time"}
        human_path.assert_not_called()

    @pytest.mark.parametrize("tool,args", [
        ("lem_queue_dm", _dm_args(status="approved")),
        ("lem_edit_lead_signal_draft", {"signal_id": 4, "draft_response": "x", "action": "approve"}),
        ("lem_edit_catchup_draft", {"touch_id": 4, "message": "x", "action": "approve"}),
        ("lem_draft_post", {"content": "x", "scheduled_datetime": _WHEN, "post_type": "video"}),
    ])
    def test_an_approval_or_media_argument_is_refused_before_any_call(
            self, api_client: Any, outreach: MagicMock, tool: str, args: dict) -> None:
        with patch(f"{_M}.update_pending_row") as edit, patch(f"{_M}.insert_scheduled_dm") as insert:
            result = call(api_client, tool, args)
        assert _error(result)["code"] == "invalid_arguments"
        edit.assert_not_called()
        insert.assert_not_called()

    def test_an_mcp_token_cannot_queue_outreach(self, api_client: Any, mcp: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm") as insert:
            result = call(api_client, "lem_queue_dm", _dm_args())
        assert _error(result)["code"] == "unknown_tool"
        insert.assert_not_called()


def _approve_patches(stack: ExitStack, kind: str) -> MagicMock:
    """The route-side reads and the write each approval kind reaches; returns the write mock."""
    if kind == "dm":
        stack.enter_context(patch(f"{_M}.get_scheduled_dm_user_id", return_value=UID))
        stack.enter_context(patch(f"{_M}.get_scheduled_dm", return_value={"scheduled_time": None}))
        return stack.enter_context(patch(f"{_M}.update_scheduled_dm", return_value=True))
    if kind == "connection_request":
        stack.enter_context(patch(f"{_M}.get_connection_request_user_id", return_value=UID))
        return stack.enter_context(patch(f"{_M}.update_connection_request", return_value=True))
    if kind == "lead_signal":
        stack.enter_context(patch(f"{_M}.get_lead_signal",
                                  return_value={"user_id": UID, "draft_response": "Thanks"}))
        stack.enter_context(patch(f"{_M}.send_lead_response"))
        return stack.enter_context(patch(f"{_M}.update_lead_signal", return_value=True))
    if kind == "catchup_touch":
        stack.enter_context(patch(f"{_M}.get_catchup_touch",
                                  return_value={"user_id": UID, "message": "Congrats"}))
        return stack.enter_context(patch(f"{_M}.update_catchup_touch", return_value=True))
    stack.enter_context(patch(f"{_O}.get_outreach_target_user_id", return_value=UID))
    return stack.enter_context(patch(f"{_O}.update_outreach_target", return_value=True))


_KINDS = ["dm", "connection_request", "lead_signal", "catchup_touch", "outreach_target"]
_APPROVED = {"dm": ScheduledDmStatus.APPROVED, "connection_request": ConnectionRequestStatus.APPROVED,
             "outreach_target": OutreachStatus.APPROVED}


class TestApproveItem:
    """Owner ruling O1: listed and allowed only on internal/sandbox accounts; audited."""

    @staticmethod
    @pytest.fixture
    def as_kind(resolver: MagicMock, audit: MagicMock) -> Any:
        def _enter(stack: ExitStack, kind: str, scope: str = "mcp_outreach") -> MagicMock:
            stack.enter_context(admitted(resolver, scope))
            stack.enter_context(patch(f"{_W}.get_user_account_kind", return_value=kind))
            stack.enter_context(patch(f"{_M}.get_user_account_kind", return_value=kind))
            return stack.enter_context(patch(f"{_M}._safe_auth_event"))
        return _enter

    def _listed(self, api_client: Any) -> list[str]:
        return [t["name"] for t in rpc(api_client, "tools/list").json()["result"]["tools"]]

    @pytest.mark.parametrize("kind", ["internal", "sandbox"])
    def test_it_is_listed_on_internal_and_sandbox(self, api_client: Any, as_kind: Any,
                                                  kind: str) -> None:
        with ExitStack() as stack:
            as_kind(stack, kind)
            assert "lem_approve_item" in self._listed(api_client)

    @pytest.mark.parametrize("kind", ["standard", "", "admin"])
    def test_it_is_absent_on_every_other_account(self, api_client: Any, as_kind: Any,
                                                 kind: str) -> None:
        with ExitStack() as stack:
            as_kind(stack, kind)
            listed = self._listed(api_client)
        assert "lem_approve_item" not in listed
        assert "lem_queue_dm" in listed

    def test_an_mcp_token_never_lists_it_even_on_internal(self, api_client: Any,
                                                          as_kind: Any) -> None:
        with ExitStack() as stack:
            as_kind(stack, "internal", scope="mcp")
            assert "lem_approve_item" not in self._listed(api_client)

    @pytest.mark.parametrize("item", _KINDS)
    def test_a_standard_account_is_refused_and_nothing_is_written(
            self, api_client: Any, as_kind: Any, audit: MagicMock, item: str) -> None:
        with ExitStack() as stack:
            route_audit = as_kind(stack, "standard")
            write = _approve_patches(stack, item)
            result = call(api_client, "lem_approve_item", {"kind": item, "id": 12})
        assert _error(result)["code"] == "agent_may_not_approve"
        write.assert_not_called()
        audit.assert_not_called()
        assert not [c for c in route_audit.call_args_list
                    if c.args and c.args[0] == AuthAuditEvent.MCP_APPROVAL]

    @pytest.mark.parametrize("item", _KINDS)
    def test_the_route_refuses_a_standard_account_even_if_the_tool_let_it_through(
            self, api_client: Any, as_kind: Any, item: str) -> None:
        """Defense in depth: the route's own guard decides again, from the account kind it reads."""
        with ExitStack() as stack:
            as_kind(stack, "standard")
            stack.enter_context(patch(f"{_W}.account_may_approve", return_value=True))
            write = _approve_patches(stack, item)
            result = call(api_client, "lem_approve_item", {"kind": item, "id": 12})
        assert _error(result)["code"] == "agent_may_not_approve"
        write.assert_not_called()

    @pytest.mark.parametrize("account", ["internal", "sandbox"])
    @pytest.mark.parametrize("item", _KINDS)
    def test_internal_and_sandbox_approve_and_are_audited(
            self, api_client: Any, as_kind: Any, audit: MagicMock, account: str, item: str) -> None:
        with ExitStack() as stack:
            route_audit = as_kind(stack, account)
            write = _approve_patches(stack, item)
            result = call(api_client, "lem_approve_item", {"kind": item, "id": 12})
        body = structured(result)
        assert result["isError"] is False, result
        assert body["status"] == "approved" and body["kind"] == item and body["id"] == 12
        write.assert_called_once()
        if item in _APPROVED:
            assert write.call_args.kwargs["status"] == _APPROVED[item]
        approvals = [c for c in route_audit.call_args_list
                     if c.args and c.args[0] == AuthAuditEvent.MCP_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0].kwargs["details"]["account_kind"] == account
        assert approvals[0].kwargs["details"]["role"] == "mcp_outreach"
        assert audit.call_args.kwargs["details"]["tool"] == "lem_approve_item"

    def test_an_internal_lead_signal_approval_dispatches_the_send(
            self, api_client: Any, as_kind: Any) -> None:
        """The approval takes the human path, so the send task the approval exists for is queued."""
        with ExitStack() as stack:
            as_kind(stack, "internal")
            _approve_patches(stack, "lead_signal")
            from cqc_lem.api import main
            result = call(api_client, "lem_approve_item", {"kind": "lead_signal", "id": 12})
            main.send_lead_response.apply_async.assert_called_once_with(kwargs={"signal_id": 12})
        assert result["isError"] is False

    def test_an_internal_account_still_edits_pending_only(self, api_client: Any,
                                                          as_kind: Any) -> None:
        """Only the approval leaves the pending-only path; an edit on an internal account does not."""
        with ExitStack() as stack:
            as_kind(stack, "internal")
            stack.enter_context(patch(f"{_M}.get_catchup_touch",
                                      return_value={"user_id": UID, "message": "x"}))
            edit = stack.enter_context(patch(f"{_M}.update_pending_row",
                                             return_value=PendingEdit.NOT_PENDING))
            human = stack.enter_context(patch(f"{_M}.update_catchup_touch"))
            result = call(api_client, "lem_edit_catchup_draft", {"touch_id": 3, "message": "Hi"})
        assert _error(result)["code"] == "edit_requires_pending"
        edit.assert_called_once()
        human.assert_not_called()


class TestTheRouteRefusesADirectApproval:
    """A direct `PUT /dm` approval by an mcp seat on a standard account is the route's own 403.

    Through the dispatch binding, so `_scope_checked` admits it; the handler refuses it whatever
    the tool layer did.
    """

    def test_put_dm_approve_is_403_on_a_standard_account(self, api_client: Any) -> None:
        from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret

        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": UID, "scope": "mcp_outreach"} if t == TOKEN
                   else None), \
                patch(f"{_M}.get_user_account_kind", return_value="standard"), \
                patch(f"{_M}.get_scheduled_dm_user_id", return_value=UID), \
                patch(f"{_M}.update_scheduled_dm") as write, \
                patch(f"{_M}._safe_auth_event"):
            resp = api_client.put("/api/dm", json={"session_token": TOKEN, "dm_id": 5,
                                                   "action": "approve"},
                                  headers={DISPATCH_HEADER: dispatch_secret(), "X-LEM-Client": "mcp"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        write.assert_not_called()


class TestDedupe:
    def test_the_same_request_id_and_args_replay_without_a_second_write(
            self, api_client: Any, outreach: MagicMock, audit: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", return_value=501) as insert:
            first = call(api_client, "lem_queue_dm", _dm_args(client_request_id="req-1"))
            second = call(api_client, "lem_queue_dm", _dm_args(client_request_id="req-1"))
        assert insert.call_count == 1
        assert audit.call_count == 1
        assert structured(second)["id"] == structured(first)["id"] == 501
        assert structured(second)["replayed"] is True

    def test_a_server_derived_key_catches_an_identical_retry(self, api_client: Any,
                                                             outreach: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", return_value=501) as insert:
            call(api_client, "lem_queue_dm", _dm_args())
            again = call(api_client, "lem_queue_dm", _dm_args())
        assert insert.call_count == 1
        assert structured(again)["replayed"] is True

    def test_the_same_id_with_different_args_is_refused(self, api_client: Any,
                                                        outreach: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", return_value=501) as insert:
            call(api_client, "lem_queue_dm", _dm_args(client_request_id="req-1"))
            other = call(api_client, "lem_queue_dm",
                         _dm_args(client_request_id="req-1", message="different"))
        assert _error(other)["code"] == "client_request_id_reused"
        assert insert.call_count == 1

    def test_different_args_without_an_id_are_two_writes(self, api_client: Any,
                                                         outreach: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", side_effect=[1, 2]) as insert:
            call(api_client, "lem_queue_dm", _dm_args())
            call(api_client, "lem_queue_dm", _dm_args(message="another"))
        assert insert.call_count == 2

    def test_two_users_never_share_a_dedupe_key(self, api_client: Any, resolver: MagicMock,
                                                audit: MagicMock) -> None:
        with patch(f"{_M}.insert_scheduled_dm", side_effect=[1, 2]) as insert, \
                patch(f"{_W}.get_user_account_kind", return_value="standard"):
            with admitted(resolver, "mcp_outreach", user_id=UID):
                call(api_client, "lem_queue_dm", _dm_args(client_request_id="same"))
            with admitted(resolver, "mcp_outreach", user_id=UID + 1):
                second = call(api_client, "lem_queue_dm", _dm_args(client_request_id="same"))
        assert insert.call_count == 2
        assert "replayed" not in structured(second)
        assert writes.dedupe_key(1, 9, "t", "r") != writes.dedupe_key(2, 9, "t", "r")

    def test_a_refused_write_releases_its_claim(self, api_client: Any, outreach: MagicMock,
                                                fake_redis: Any) -> None:
        with patch(f"{_M}.get_catchup_touch", return_value={"user_id": UID, "message": "x"}), \
                patch(f"{_M}.update_pending_row",
                      side_effect=[PendingEdit.NOT_PENDING, PendingEdit.UPDATED]) as edit:
            first = call(api_client, "lem_edit_catchup_draft", {"touch_id": 3, "message": "Hi"})
            second = call(api_client, "lem_edit_catchup_draft", {"touch_id": 3, "message": "Hi"})
        assert _error(first)["code"] == "edit_requires_pending"
        assert second["isError"] is False
        assert edit.call_count == 2

    def test_a_timeout_is_unknown_and_a_retry_is_refused(self, api_client: Any,
                                                        outreach: MagicMock) -> None:
        def slow(*_a: Any, **_k: Any) -> int:
            time.sleep(0.3)
            return 501

        with patch("cqc_lem.api.mcp.calls.TOOL_TIMEOUT_SECONDS", 0.05), \
                patch(f"{_M}.insert_scheduled_dm", side_effect=slow) as insert:
            first = call(api_client, "lem_queue_dm", _dm_args(client_request_id="t-1"))
            retry = call(api_client, "lem_queue_dm", _dm_args(client_request_id="t-1"))
            time.sleep(0.35)
        error = _error(first)
        assert error["code"] == "outcome_unknown" and error["outcome"] == "unknown"
        assert "Do NOT retry" in error["message"]
        assert _error(retry)["outcome"] == "unknown"
        assert insert.call_count == 1


class TestFlagBudgetAndAvailability:
    def test_writes_flag_off_refuses_every_write(self, api_client: Any, outreach: MagicMock,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")
        with patch(f"{_M}.insert_scheduled_dm") as insert:
            result = call(api_client, "lem_queue_dm", _dm_args())
        assert _error(result)["code"] == "writes_disabled"
        insert.assert_not_called()

    def test_a_spent_write_budget_refuses_and_releases_the_claim(
            self, api_client: Any, outreach: MagicMock, monkeypatch: pytest.MonkeyPatch,
            fake_redis: Any) -> None:
        monkeypatch.setenv("MCP_RATE_WRITES_PER_HOUR", "1")
        with patch(f"{_M}.insert_scheduled_dm", side_effect=[1, 2]) as insert:
            call(api_client, "lem_queue_dm", _dm_args())
            second = call(api_client, "lem_queue_dm", _dm_args(message="second"))
        error = _error(second)
        assert error["code"] == "write_budget_exhausted" and error["outcome"] == "rate_limited"
        assert insert.call_count == 1
        claims = [k for k in fake_redis.store if k.startswith("mcp:dedupe:")]
        assert len(claims) == 1

    def test_a_zero_daily_budget_refuses_all(self, api_client: Any, outreach: MagicMock,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_RATE_WRITES_PER_DAY", "0")
        with patch(f"{_M}.insert_scheduled_dm") as insert:
            result = call(api_client, "lem_queue_dm", _dm_args())
        assert _error(result)["code"] == "write_budget_exhausted"
        insert.assert_not_called()

    def test_the_default_caps_are_30_an_hour_and_150_a_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cqc_lem.api.mcp import limits

        monkeypatch.delenv("MCP_RATE_WRITES_PER_HOUR", raising=False)
        monkeypatch.delenv("MCP_RATE_WRITES_PER_DAY", raising=False)
        assert [limits.limit(b.limit_env) for b in limits.WRITE_BUDGETS] == [30, 150]

    def test_an_unreadable_dedupe_store_fails_closed(self) -> None:
        from .conftest import FakeRedis

        with patch(f"{_W}._redis", return_value=FakeRedis(raises=True)):
            with pytest.raises(writes.LimitsUnavailable):
                writes.claim_write("k", "h")
        with patch(f"{_W}._redis", return_value=None):
            with pytest.raises(writes.LimitsUnavailable):
                writes.claim_write("k", "h")

    def test_a_malformed_record_reads_as_unknown(self) -> None:
        from .conftest import FakeRedis

        fake = FakeRedis()
        fake.store["k"] = b"not json"
        with patch(f"{_W}._redis", return_value=fake):
            assert writes.claim_write("k", "h") == {"state": "unknown"}

    def test_settle_and_release_are_best_effort(self) -> None:
        from .conftest import FakeRedis

        for client in (None, FakeRedis(raises=True)):
            with patch(f"{_W}._redis", return_value=client):
                writes.settle_write("k", "h", "done", {"id": 1})
                writes.release_write("k")


class TestNothingSecretIsLogged:
    def test_no_token_or_body_reaches_a_log_record(self, api_client: Any, outreach: MagicMock,
                                                   caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        with patch(f"{_M}.insert_scheduled_dm", return_value=501):
            call(api_client, "lem_queue_dm", _dm_args())
            call(api_client, "lem_queue_dm", _dm_args())  # the dedupe hit logs too
            call(api_client, "lem_queue_dm", _dm_args(recipient_profile_url="https://x.example/"))
        text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert caplog.records, "nothing was logged: this test would pass vacuously"
        assert TOKEN not in text
        assert _BODY_CANARY not in text


class TestPrompts:
    def test_an_outreach_token_lists_both_write_prompts(self, api_client: Any,
                                                        outreach: MagicMock) -> None:
        names = {p["name"] for p in rpc(api_client, "prompts/list").json()["result"]["prompts"]}
        assert {"fill_my_content_plan", "prepare_outreach_batch"} <= names

    def test_an_mcp_token_cannot_get_the_outreach_prompt(self, api_client: Any,
                                                         mcp: MagicMock) -> None:
        got = rpc(api_client, "prompts/get", {"name": "prepare_outreach_batch"}).json()
        assert "error" in got

    def test_fill_my_content_plan_clamps_weeks_and_says_pending(self, api_client: Any,
                                                                mcp: MagicMock) -> None:
        got = rpc(api_client, "prompts/get", {"name": "fill_my_content_plan",
                                              "arguments": {"weeks": "9"}}).json()
        text = got["result"]["messages"][0]["content"]["text"]
        assert "weeks=4" in text and "PENDING" in text and "lem_draft_post" in text

    def test_the_outreach_prompt_text_is_served_to_outreach(self, api_client: Any,
                                                            outreach: MagicMock) -> None:
        got = rpc(api_client, "prompts/get", {"name": "prepare_outreach_batch"}).json()
        text = got["result"]["messages"][0]["content"]["text"]
        assert "third_party" in text and "PENDING" in text
