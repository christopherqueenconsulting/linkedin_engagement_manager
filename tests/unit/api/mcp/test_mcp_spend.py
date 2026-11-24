"""The AI/media spend tools, end to end, plus the sub-cap and provenance (W2b, issue #2689).

Owner ruling O4: `lem_generate_post`, `lem_generate_post_image` and `lem_generate_post_video` spend
under the free-trial daily AI cap AND an MCP sub-cap (10 / 3 / 1 a day, env `MCP_SPEND_*`, 0
refuses); media only for a PENDING post THIS token created; everything lands PENDING; nothing runs
inline. Every tool call here posts JSON-RPC to `/api/mcp`, so the inner `POST` runs through the
real gate, loopback, `_scope_checked` and route. Only the route's own DB reads and the Celery
dispatch are patched; no LLM is ever reached (`mock_openai_client` is asserted untouched).
"""

import asyncio
import dataclasses
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import calls, spend, tools as registry
from cqc_lem.api.mcp.limits import LimitsUnavailable
from cqc_lem.api.routers.mcp_spend import PLACEHOLDER_CONTENT

from ._admitted import TOKEN, UID, admitted, call, structured

pytestmark = pytest.mark.unit

_R = "cqc_lem.api.routers.mcp_spend"
_SESSION_ID = 901  # what `_admitted.admitted` makes `main.get_session_id` answer
_TOPIC = "Why small teams should automate their weekly reporting"


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    """Reads and writes on (env fallback), the in-memory Redis behind every limit and cap."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")
    for env in spend.ENV_SPEND.values():
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def mcp(resolver: MagicMock) -> Iterator[MagicMock]:
    """An admitted `mcp` token."""
    with admitted(resolver, "mcp") as inner:
        yield inner


@pytest.fixture
def tasks() -> Iterator[dict[str, MagicMock]]:
    """The three Celery dispatches, captured instead of sent."""
    from cqc_lem.app import run_content_plan as rcp

    with patch.object(rcp.generate_agent_post_task, "apply_async") as text, \
            patch.object(rcp.generate_agent_post_image_task, "apply_async") as image, \
            patch.object(rcp.generate_agent_post_video_task, "apply_async") as video:
        yield {"text": text, "image": image, "video": video}


@contextmanager
def post_row(*, owner: int = UID, status: str = "pending", post_type: Any = "text",
             content: Optional[str] = "A finished draft about reporting.",
             image_url: Optional[str] = None, video_url: Optional[str] = None,
             ) -> Iterator[MagicMock]:
    """The post a media tool names, as the route reads it: owner-scoped, so a stranger's is None."""
    row = {"status": status, "post_type": post_type, "content": content, "image_url": image_url,
           "video_url": video_url}
    with patch(f"{_R}.get_post_for_user",
               side_effect=lambda user_id, post_id: dict(row, id=post_id) if user_id == owner
               else None) as read:
        yield read


@contextmanager
def new_post(post_id: int = 501) -> Iterator[MagicMock]:
    """The insert `lem_generate_post` makes."""
    with patch(f"{_R}.get_user_email", return_value="owner@example.com"), \
            patch(f"{_R}.insert_post_returning_id", return_value=post_id) as insert:
        yield insert


def _error_code(result: dict) -> Optional[str]:
    assert result["isError"] is True, result
    return structured(result)["error"]["code"]


# --- lem_generate_post ----------------------------------------------------------------------------


class TestGeneratePost:
    def test_it_lands_a_pending_text_placeholder_and_queues_the_writer(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any,
            mock_openai_client: MagicMock) -> None:
        with new_post(501) as insert:
            result = call(api_client, "lem_generate_post", {"topic": _TOPIC})
        out = structured(result)
        assert result["isError"] is False
        assert out["post_id"] == 501 and out["status"] == "queued"
        assert out["post_status"] == "pending" and out["review_url"].endswith("/content")
        args, kwargs = insert.call_args
        assert args[1] == PLACEHOLDER_CONTENT and args[3].value == "text"
        assert kwargs["status"].value == "pending"
        assert set(kwargs) == {"status"}  # no media, no avatar, no approver
        tasks["text"].assert_called_once_with(kwargs={"post_id": 501, "guidance": _TOPIC})
        assert fake_redis.store["mcp:spend:post:501"] == f"{UID}:{_SESSION_ID}"
        mock_openai_client.chat.completions.create.assert_not_called()

    def test_it_writes_one_mcp_write_audit_row_without_the_topic(
            self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        with new_post(501), patch("cqc_lem.api.main._safe_auth_event") as audit:
            call(api_client, "lem_generate_post", {"topic": _TOPIC})
        writes = [c for c in audit.call_args_list if c.args[0].value == "mcp_write"]
        assert len(writes) == 1
        assert writes[0].kwargs["details"] == {"tool": "lem_generate_post", "target_id": 501}
        assert _TOPIC not in repr(writes[0])

    def test_a_schedule_outside_the_window_is_refused_before_any_spend(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any) -> None:
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with new_post() as insert:
            result = call(api_client, "lem_generate_post",
                          {"topic": _TOPIC, "scheduled_datetime": soon})
        # The route wrote this 422, so its own code and message reach the agent.
        assert _error_code(result) == "schedule_out_of_range"
        assert "1 hour and 90 days" in structured(result)["error"]["message"]
        insert.assert_not_called()
        assert not any(k.startswith("mcp:spend:text") for k in fake_redis.store)

    def test_a_framework_validation_422_still_hides_its_body(self, api_client: Any,
                                                             mcp: MagicMock) -> None:
        """FastAPI's own 422 echoes the input, so it collapses to `invalid_arguments`."""
        from cqc_lem.api.mcp.tools_spend import SPEND_POST_PATH, post_route

        with pytest.raises(Exception) as err:
            asyncio.run(post_route(_loopback(MagicMock(user_id=UID)), SPEND_POST_PATH,
                                   {"topic": "zz-canary-short"[:3]}))
        assert err.value.code == "invalid_arguments"
        assert "zz-" not in err.value.message

    def test_a_named_slot_is_kept(self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        when = datetime.now(timezone.utc) + timedelta(days=3)
        with new_post() as insert:
            call(api_client, "lem_generate_post", {"topic": _TOPIC,
                                                   "scheduled_datetime": when.isoformat()})
        assert abs((insert.call_args.args[2] - when).total_seconds()) < 1

    def test_a_failed_dispatch_marks_the_placeholder_error(
            self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        tasks["text"].side_effect = RuntimeError("broker down")
        with new_post(502), patch(f"{_R}.fail_agent_post_placeholder") as fail:
            result = call(api_client, "lem_generate_post", {"topic": _TOPIC})
        assert _error_code(result) == "upstream_error"
        fail.assert_called_once_with(502, UID, PLACEHOLDER_CONTENT)  # guarded: placeholder only

    def test_no_row_means_no_queue(self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        with patch(f"{_R}.get_user_email", return_value=None), \
                patch(f"{_R}.insert_post_returning_id") as insert:
            result = call(api_client, "lem_generate_post", {"topic": _TOPIC})
        assert _error_code(result) == "upstream_error"
        insert.assert_not_called()
        tasks["text"].assert_not_called()

    def test_unknown_arguments_and_a_short_topic_are_refused(
            self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        assert _error_code(call(api_client, "lem_generate_post",
                                {"topic": _TOPIC, "status": "approved"})) == "invalid_arguments"
        assert _error_code(call(api_client, "lem_generate_post",
                                {"topic": "short"})) == "invalid_arguments"
        tasks["text"].assert_not_called()


# --- the caps -------------------------------------------------------------------------------------


class TestTheCaps:
    def test_text_refuses_at_ten(self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        with new_post():
            results = [call(api_client, "lem_generate_post", {"topic": _TOPIC}) for _ in range(11)]
        assert [r["isError"] for r in results] == [False] * 10 + [True]
        assert _error_code(results[-1]) == "mcp_spend_cap"
        assert tasks["text"].call_count == 10

    @pytest.mark.parametrize("tool,kind,cap", [("lem_generate_post_image", "image", 3),
                                               ("lem_generate_post_video", "video", 1)])
    def test_media_refuses_at_its_cap(self, api_client: Any, mcp: MagicMock, tasks: dict,
                                      tool: str, kind: str, cap: int) -> None:
        # A different post each time: the same post twice is `generation_in_progress` (below).
        for post_id in range(7, 7 + cap + 1):
            spend.mark_created_post(post_id, UID, _SESSION_ID)
        with post_row():
            results = [call(api_client, tool, {"post_id": post_id})
                       for post_id in range(7, 7 + cap + 1)]
        assert [r["isError"] for r in results] == [False] * cap + [True]
        assert _error_code(results[-1]) == "mcp_spend_cap"
        assert tasks[kind].call_count == cap

    @pytest.mark.parametrize("env,tool,arguments", [
        ("MCP_SPEND_TEXT", "lem_generate_post", {"topic": _TOPIC}),
        ("MCP_SPEND_IMAGE", "lem_generate_post_image", {"post_id": 7}),
        ("MCP_SPEND_VIDEO", "lem_generate_post_video", {"post_id": 7}),
    ])
    def test_zero_refuses_every_call(self, api_client: Any, mcp: MagicMock, tasks: dict,
                                     monkeypatch: pytest.MonkeyPatch, env: str, tool: str,
                                     arguments: dict) -> None:
        monkeypatch.setenv(env, "0")
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(), new_post() as insert:
            result = call(api_client, tool, arguments)
        assert _error_code(result) == "mcp_spend_cap"
        insert.assert_not_called()
        assert not any(t.called for t in tasks.values())

    def test_the_daily_ai_cap_maps_to_daily_ai_cap_and_spends_no_sub_cap_unit(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any) -> None:
        with new_post() as insert, \
                patch("cqc_lem.utilities.ai_spend_cap.cap_status",
                      return_value=("reached", 0.62, 0.5)), \
                patch("cqc_lem.utilities.ai_spend_cap._first_reach_today", return_value=False):
            result = call(api_client, "lem_generate_post", {"topic": _TOPIC})
        assert _error_code(result) == "daily_ai_cap"
        error = structured(result)["error"]
        assert error["outcome"] == "rate_limited"
        # The route's `Retry-After` rides through: seconds to the next UTC midnight.
        assert 0 < error["retry_after"] <= 24 * 60 * 60
        insert.assert_not_called()
        tasks["text"].assert_not_called()
        assert not any(k.startswith("mcp:spend:text") for k in fake_redis.store)

    def test_the_daily_ai_cap_also_guards_media(self, api_client: Any, mcp: MagicMock,
                                                tasks: dict) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(), patch("cqc_lem.utilities.ai_spend_cap.cap_status",
                               return_value=("reached", 0.62, 0.5)), \
                patch("cqc_lem.utilities.ai_spend_cap._first_reach_today", return_value=False):
            result = call(api_client, "lem_generate_post_video", {"post_id": 7})
        assert _error_code(result) == "daily_ai_cap"
        tasks["video"].assert_not_called()

    def test_unreadable_limits_fail_closed(self, api_client: Any, mcp: MagicMock,
                                           tasks: dict) -> None:
        # Only the sub-cap faults: a broken `incr` would stop the gate's own budget first.
        with new_post() as insert, patch(f"{_R}.claim_spend", side_effect=LimitsUnavailable()):
            result = call(api_client, "lem_generate_post", {"topic": _TOPIC})
        assert _error_code(result) == "mcp_unavailable"
        insert.assert_not_called()


# --- media: only for a pending post THIS token created ---------------------------------------------


class TestMediaProvenance:
    @pytest.mark.parametrize("tool,kind", [("lem_generate_post_image", "image"),
                                           ("lem_generate_post_video", "video")])
    def test_a_pending_post_this_token_created_is_queued(
            self, api_client: Any, mcp: MagicMock, tasks: dict, tool: str, kind: str,
            mock_openai_client: MagicMock) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row():
            result = call(api_client, tool, {"post_id": 7})
        out = structured(result)
        assert result["isError"] is False
        assert out == {"post_id": 7, "kind": kind, "status": "queued", "post_status": "pending",
                       "review_url": "http://testserver/content", "note": out["note"]}
        tasks[kind].assert_called_once_with(kwargs={"post_id": 7})
        mock_openai_client.chat.completions.create.assert_not_called()

    @pytest.mark.parametrize("tool", ["lem_generate_post_image", "lem_generate_post_video"])
    def test_a_post_another_token_created_is_refused(self, api_client: Any, mcp: MagicMock,
                                                    tasks: dict, tool: str) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID + 1)
        with post_row():
            assert _error_code(call(api_client, tool, {"post_id": 7})) == "not_created_by_this_token"
        assert not any(t.called for t in tasks.values())

    @pytest.mark.parametrize("tool", ["lem_generate_post_image", "lem_generate_post_video"])
    def test_a_post_no_token_created_is_refused(self, api_client: Any, mcp: MagicMock,
                                               tasks: dict, tool: str) -> None:
        with post_row():
            assert _error_code(call(api_client, tool, {"post_id": 7})) == "not_created_by_this_token"

    @pytest.mark.parametrize("status", ["approved", "scheduled", "posted", "rejected", "error"])
    @pytest.mark.parametrize("tool", ["lem_generate_post_image", "lem_generate_post_video"])
    def test_a_post_that_is_not_pending_is_refused(self, api_client: Any, mcp: MagicMock,
                                                  tasks: dict, tool: str, status: str) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(status=status):
            assert _error_code(call(api_client, tool, {"post_id": 7})) == "requires_pending"
        assert not any(t.called for t in tasks.values())

    def test_another_users_post_is_not_found(self, api_client: Any, mcp: MagicMock,
                                             tasks: dict) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(owner=UID + 1):
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "not_found"

    def test_a_post_still_being_written_is_refused(self, api_client: Any, mcp: MagicMock,
                                                   tasks: dict) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(content=PLACEHOLDER_CONTENT):
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "post_still_generating"

    def test_a_video_post_is_refused(self, api_client: Any, mcp: MagicMock, tasks: dict) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(post_type="video"):
            assert _error_code(call(api_client, "lem_generate_post_video",
                                    {"post_id": 7})) == "requires_text_post"

    def test_an_unreadable_mark_fails_closed(self, api_client: Any, mcp: MagicMock, tasks: dict,
                                            fake_redis: Any) -> None:
        with post_row(), patch(f"{_R}.created_by_session", side_effect=LimitsUnavailable()):
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "mcp_unavailable"
        assert not any(t.called for t in tasks.values())


# --- one render per (post, kind), and a post that already has the media ---------------------------


class TestOneRenderAtATime:
    @pytest.mark.parametrize("tool,kind", [("lem_generate_post_image", "image"),
                                           ("lem_generate_post_video", "video")])
    def test_a_second_request_while_one_is_in_flight_is_refused_without_spend(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any, tool: str,
            kind: str) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row():
            first = call(api_client, tool, {"post_id": 7})
            second = call(api_client, tool, {"post_id": 7})
        assert first["isError"] is False
        assert _error_code(second) == "generation_in_progress"
        assert tasks[kind].call_count == 1
        spent = [k for k in fake_redis.store if k.startswith(f"mcp:spend:{kind}:")]
        assert [fake_redis.store[k] for k in spent] == [1]  # the refusal claimed no sub-cap unit

    def test_the_marker_is_released_when_the_request_is_refused_after_claiming_it(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SPEND_IMAGE", "0")
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row():
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "mcp_spend_cap"
        assert "mcp:spend:inflight:image:7" not in fake_redis.store

    def test_the_marker_is_released_when_the_dispatch_fails(
            self, api_client: Any, mcp: MagicMock, tasks: dict, fake_redis: Any) -> None:
        tasks["image"].side_effect = RuntimeError("broker down")
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row():
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "upstream_error"
        assert "mcp:spend:inflight:image:7" not in fake_redis.store

    @pytest.mark.parametrize("tool,column", [("lem_generate_post_image", "image_url"),
                                             ("lem_generate_post_video", "video_url")])
    def test_a_post_that_already_has_the_media_is_refused(
            self, api_client: Any, mcp: MagicMock, tasks: dict, tool: str, column: str) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(**{column: "/api/assets/existing"}):
            assert _error_code(call(api_client, tool, {"post_id": 7})) == "already_generated"
        assert not any(t.called for t in tasks.values())

    def test_an_unreadable_post_is_503_not_404(self, api_client: Any, mcp: MagicMock,
                                               tasks: dict) -> None:
        from cqc_lem.utilities.db import OwnershipUnprovable

        with patch(f"{_R}.get_post_for_user", side_effect=OwnershipUnprovable("db down")):
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "mcp_unavailable"
        assert not any(t.called for t in tasks.values())

    def test_an_unreadable_marker_fails_closed(self, api_client: Any, mcp: MagicMock,
                                               tasks: dict) -> None:
        spend.mark_created_post(7, UID, _SESSION_ID)
        with post_row(), patch(f"{_R}.claim_inflight", side_effect=LimitsUnavailable()):
            assert _error_code(call(api_client, "lem_generate_post_image",
                                    {"post_id": 7})) == "mcp_unavailable"
        assert not any(t.called for t in tasks.values())


class TestTheTruthTheAgentIsTold:
    def test_lem_explain_lem_names_the_spend_tools_caps_pending_and_credits(self) -> None:
        text = registry.EXPLANATIONS["agent_capabilities"]
        for fragment in ("lem_generate_post", "10 posts, 3 images and 1 video", "daily AI limit",
                         "PENDING", "premium video credits"):
            assert fragment in text, fragment
        assert "read-only" not in text
        assert "it can only read" not in registry.EXPLANATIONS["approval"]

    def test_every_spend_tool_states_its_caps_and_that_it_lands_pending(self) -> None:
        for spec in registry.TOOLS:
            if spec.self_guarded:
                assert "10 posts, 3 images and 1 video" in spec.description, spec.name
                assert "PENDING" in spec.description, spec.name
        assert "premium video credits" in registry.TOOLS_BY_NAME[
            "lem_generate_post_video"].description


# --- the rollout flag, the seat rule and W1's untouched guard ------------------------------------


class TestWhoMaySpend:
    def test_writes_off_refuses_before_any_inner_call(
            self, api_client: Any, mcp: MagicMock, tasks: dict,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")
        with new_post() as insert:
            assert _error_code(call(api_client, "lem_generate_post",
                                    {"topic": _TOPIC})) == "writes_disabled"
        insert.assert_not_called()

    def test_the_route_refuses_writes_off_on_its_own(self, api_client: Any, mcp: MagicMock,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        """The route does not trust the tool: writes off is refused server-side too."""
        from cqc_lem.api.mcp.tools_spend import SPEND_POST_PATH, post_route

        monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "false")
        principal = MagicMock(user_id=UID)
        lb = _loopback(principal)
        with pytest.raises(Exception) as err:
            asyncio.run(post_route(lb, SPEND_POST_PATH, {"topic": _TOPIC}))
        assert getattr(err.value, "code", None) == "mcp_writes_disabled"

    @pytest.mark.parametrize("scope", ["full", "agent", "operator"])
    def test_every_other_seat_is_refused_mcp_only(self, api_client: Any, resolver: MagicMock,
                                                  tasks: dict, scope: str) -> None:
        row = {"user_id": UID, "scope": scope, "session_id": 5, "expires_at": None}
        with patch("cqc_lem.api.main._db_resolve_session", return_value=row), \
                patch("cqc_lem.api.main._safe_auth_event"), new_post() as insert:
            resp = api_client.post("/api/user/mcp-spend/post",
                                   json={"session_token": TOKEN, "topic": _TOPIC},
                                   headers={"X-LEM-Client": "spa"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] in ("mcp_only", "session_scope_forbidden")
        insert.assert_not_called()
        tasks["text"].assert_not_called()

    def test_schedule_post_media_from_an_mcp_seat_is_still_a_422(
            self, api_client: Any, mcp: MagicMock) -> None:
        """W1's text-only guard is untouched: even a seat granted `/schedule_post/` gets the 422."""
        from cqc_lem.api.main import _scope_allows

        def allow_schedule(scope: Any, path: Any, method: Any = None) -> bool:
            if str(path).rstrip("/").endswith("/schedule_post"):
                return True
            return _scope_allows(scope, path, method)

        lb_headers = _inner_headers()
        with patch("cqc_lem.api.main._scope_allows", side_effect=allow_schedule), \
                patch("cqc_lem.api.main.insert_post_returning_id") as insert:
            resp = api_client.post("/api/schedule_post/", headers=lb_headers, json={
                "session_token": "cookie", "content": "Draft", "post_type": "text",
                "scheduled_datetime": "2099-01-01T10:00:00+00:00",
                "image_url": "https://example.com/x.png"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "machine_seat_text_only"
        insert.assert_not_called()

    def test_a_text_draft_from_an_mcp_seat_is_marked_as_this_tokens(
            self, api_client: Any, mcp: MagicMock, fake_redis: Any) -> None:
        from cqc_lem.api.main import _scope_allows

        def allow_schedule(scope: Any, path: Any, method: Any = None) -> bool:
            if str(path).rstrip("/").endswith("/schedule_post"):
                return True
            return _scope_allows(scope, path, method)

        with patch("cqc_lem.api.main._scope_allows", side_effect=allow_schedule), \
                patch("cqc_lem.api.main.get_user_email", return_value="owner@example.com"), \
                patch("cqc_lem.api.main.insert_post_returning_id", return_value=88):
            resp = api_client.post("/api/schedule_post/", headers=_inner_headers(), json={
                "session_token": "cookie", "content": "Draft", "post_type": "text",
                "scheduled_datetime": "2099-01-01T10:00:00+00:00"})
        assert resp.status_code == 200
        assert spend.created_by_session(88, UID, _SESSION_ID) is True


def _inner_headers() -> dict[str, str]:
    """What the loopback presents on an inner call: cookie, client header, dispatch secret."""
    from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret

    return {"Cookie": f"lem_session={TOKEN}", "X-LEM-Client": "mcp",
            DISPATCH_HEADER: dispatch_secret()}


def _loopback(principal: Any) -> Any:
    from cqc_lem.api.mcp.loopback import Loopback

    return Loopback(principal=principal, token=TOKEN)


# --- the registry, timeouts and logs --------------------------------------------------------------


class TestTheSpendToolsInTheRegistry:
    def test_both_scopes_list_all_three(self) -> None:
        for scope in ("mcp", "mcp_outreach"):
            names = {s.name for s in registry.tools_for_scope(scope)}
            assert {"lem_generate_post", "lem_generate_post_image",
                    "lem_generate_post_video"} <= names

    def test_a_write_that_runs_out_of_time_is_unknown_never_a_retryable_timeout(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def slow(_lb: Any, _args: Any) -> dict:
            await asyncio.sleep(1)
            return {}

        spec = registry.TOOLS_BY_NAME["lem_generate_post"]
        monkeypatch.setitem(registry.TOOLS_BY_NAME, "lem_generate_post",
                            dataclasses.replace(spec, run=slow))
        monkeypatch.setattr(calls, "TOOL_TIMEOUT_SECONDS", 0.01)
        request = MagicMock()
        request.scope = {"state": {"mcp_principal": MagicMock(user_id=UID, scope="mcp",
                                                              session_id=_SESSION_ID)}}
        with patch.object(calls, "loopback_for", return_value=MagicMock(aclose=_async_none)), \
                patch.object(calls, "_report"):
            result = asyncio.run(calls.call_tool(request, "lem_generate_post", {"topic": _TOPIC}))
        assert result.is_error is True
        assert result.structured_content["error"]["outcome"] == "unknown"

    def test_the_token_reaches_no_log_record(self, api_client: Any, mcp: MagicMock, tasks: dict,
                                             caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        spend.mark_created_post(7, UID, _SESSION_ID)
        with new_post(), post_row():
            call(api_client, "lem_generate_post", {"topic": _TOPIC})
            call(api_client, "lem_generate_post_image", {"post_id": 7})
            call(api_client, "lem_generate_post_video", {"post_id": 7})
            call(api_client, "lem_generate_post_video", {"post_id": 7})  # refused: in progress
        assert caplog.records
        for record in caplog.records:
            assert TOKEN not in record.getMessage()
            assert TOKEN not in repr(record.__dict__)


async def _async_none() -> None:
    return None


# --- spend.py directly ----------------------------------------------------------------------------


class TestSubCapCounters:
    def test_defaults_are_ten_three_one(self) -> None:
        assert [spend.spend_limit(k) for k in (spend.SPEND_TEXT, spend.SPEND_IMAGE,
                                               spend.SPEND_VIDEO)] == [10, 3, 1]

    @pytest.mark.parametrize("raw,expected", [("5", 5), ("0", 0), ("-2", 3), ("lots", 3), (" ", 3)])
    def test_a_malformed_value_never_lifts_the_cap(self, monkeypatch: pytest.MonkeyPatch,
                                                   raw: str, expected: int) -> None:
        monkeypatch.setenv("MCP_SPEND_IMAGE", raw)
        assert spend.spend_limit(spend.SPEND_IMAGE) == expected

    def test_the_ttl_is_set_once_per_day_counter(self, fake_redis: Any) -> None:
        assert spend.claim_spend(UID, spend.SPEND_IMAGE) is True
        assert spend.claim_spend(UID, spend.SPEND_IMAGE) is True
        keys = [k for k in fake_redis.store if k.startswith("mcp:spend:image:")]
        assert len(keys) == 1
        assert fake_redis.ttl_writes.count(keys[0]) == 1
        assert fake_redis.expiries[keys[0]] > 24 * 60 * 60

    def test_zero_never_touches_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SPEND_VIDEO", "0")
        with patch("cqc_lem.api.mcp.limits._redis", side_effect=AssertionError("read")):
            assert spend.claim_spend(UID, spend.SPEND_VIDEO) is False

    def test_users_never_share_a_counter(self, fake_redis: Any) -> None:
        assert spend.claim_spend(UID, spend.SPEND_VIDEO) is True
        assert spend.claim_spend(UID + 1, spend.SPEND_VIDEO) is True
        assert spend.claim_spend(UID, spend.SPEND_VIDEO) is False

    def test_no_redis_fails_closed(self) -> None:
        with patch("cqc_lem.api.mcp.limits._redis", return_value=None):
            with pytest.raises(LimitsUnavailable):
                spend.claim_spend(UID, spend.SPEND_TEXT)
            with pytest.raises(LimitsUnavailable):
                spend.created_by_session(7, UID, _SESSION_ID)
            assert spend.mark_created_post(7, UID, _SESSION_ID) is False


class TestProvenanceMark:
    def test_only_the_same_user_and_token_match(self, fake_redis: Any) -> None:
        assert spend.mark_created_post(7, UID, _SESSION_ID) is True
        assert spend.created_by_session(7, UID, _SESSION_ID) is True
        assert spend.created_by_session(7, UID, _SESSION_ID + 1) is False
        assert spend.created_by_session(7, UID + 1, _SESSION_ID) is False
        assert spend.created_by_session(8, UID, _SESSION_ID) is False
        assert fake_redis.expiries["mcp:spend:post:7"] == 90 * 24 * 60 * 60

    def test_a_stored_bytes_value_is_read(self, fake_redis: Any) -> None:
        fake_redis.store["mcp:spend:post:7"] = f"{UID}:{_SESSION_ID}".encode()
        assert spend.created_by_session(7, UID, _SESSION_ID) is True

    @pytest.mark.parametrize("post_id,user_id,session_id", [(0, UID, 1), (7, 0, 1), (7, UID, None)])
    def test_a_missing_part_is_never_a_match(self, fake_redis: Any, post_id: int, user_id: int,
                                             session_id: Optional[int]) -> None:
        assert spend.mark_created_post(post_id, user_id, session_id) is False
        assert spend.created_by_session(post_id, user_id, session_id) is False

    def test_a_read_fault_raises(self, fake_redis: Any) -> None:
        fake_redis.raises = True
        with pytest.raises(LimitsUnavailable):
            spend.created_by_session(7, UID, _SESSION_ID)
