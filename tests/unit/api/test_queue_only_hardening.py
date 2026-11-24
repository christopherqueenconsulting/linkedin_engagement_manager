"""Queue-only machine seats: pending-only edits, text-only drafts, no inbound address (issue #2684).

MCP epic #2682, PR W1. Each route is driven through the REAL resolver with a session of each seat:

- `agent`, and `operator` on a `standard` account: held to pending-only edits, enforced in SQL by
  `update_pending_row`; a row that has left pending is a 409 `edit_requires_pending`.
- `operator` on an `internal`/`sandbox` account: the operator exemption, exactly today's path.
- a full human session: exactly today's path, `update_pending_row` never runs.

`/dm`, `/catchup/touch`, `/schedule_post/`, `/update_post/` and `/posts/bulk_update/` are on no
machine surface today. Their guards are defence in depth for the MCP seats that follow, so the tests
that drive them widen the agent surface for the length of one request (`_widened`).
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_CURATED = "cqc_lem.api.routers.curated"
_OUTREACH = "cqc_lem.api.routers.outreach"

_UID = 81
_TOKEN = "queue-only-hardening-token"
_LI = "https://www.linkedin.com/in/jane-doe-1a2b3c/"
_SPA = {"X-LEM-Client": "spa"}

# (seat id, session scope, account kind, held to pending-only?)
_HELD = [("agent", "agent", "standard"), ("operator-standard", "operator", "standard")]
_EXEMPT = [("operator-internal", "operator", "internal"), ("operator-sandbox", "operator", "sandbox")]


def _kernel() -> Any:
    from cqc_lem.api import main

    return main


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    """The real resolver runs without a database; no audit row is written unless captured."""
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0), \
         patch(f"{_USER}.record_auth_event", return_value=True), \
         patch.object(_kernel(), "record_auth_event", return_value=True), \
         patch.object(_kernel(), "is_user_admin", return_value=False), \
         patch(f"{_M}.claim_operator_access", return_value=False), \
         patch(f"{_M}.claim_observer_access", return_value=False), \
         patch.object(_kernel(), "get_session_id", return_value=1):
        yield


def _session(scope: str, kind: str = "standard") -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch(f"{_M}._db_resolve_session",
                              side_effect=lambda t: {"user_id": _UID, "scope": scope}
                              if t == _TOKEN else None))
    stack.enter_context(patch.object(_kernel(), "get_user_account_kind", return_value=kind))
    return stack


def _widened() -> Any:
    """Put the not-yet-reachable edit routes on the agent and operator surfaces for one request."""
    main = _kernel()
    extra = {"/dm", "/catchup/touch", "/schedule_post", "/update_post", "/posts/bulk_update",
             "/schedule_dm"}
    return patch.dict(main._SCOPE_SURFACES, {
        main.SESSION_SCOPE_AGENT: main._AGENT_SESSION_SURFACE | extra,
        main.SESSION_SCOPE_OPERATOR: main._OPERATOR_SESSION_SURFACE | extra,
    })


def _pending(outcome: str = "UPDATED") -> Any:
    from cqc_lem.utilities.db import PendingEdit

    return patch(f"{_M}.update_pending_row", return_value=getattr(PendingEdit, outcome))


# ---------------------------------------------------------------------------------------------
# One row per edit route: how to call it, and what today's (human) path writes through.
# ---------------------------------------------------------------------------------------------

def _dm_route() -> dict:
    return {"method": "put", "path": "/api/dm", "kind": "scheduled_dm",
            "body": {"dm_id": 4, "message": "Hi Jane"},
            "stubs": [patch(f"{_M}.get_scheduled_dm_user_id", return_value=_UID)],
            "legacy": f"{_M}.update_scheduled_dm", "row_id": 4,
            "fields": {"message": "Hi Jane"}}


def _connection_route() -> dict:
    return {"method": "put", "path": "/api/connection_request", "kind": "connection_request",
            "body": {"request_id": 5, "message": "Hello"},
            "stubs": [patch(f"{_M}.get_connection_request_user_id", return_value=_UID)],
            "legacy": f"{_M}.update_connection_request", "row_id": 5,
            "fields": {"message": "Hello"}}


def _lead_signal_route() -> dict:
    return {"method": "put", "path": "/api/lead_signal", "kind": "lead_signal",
            "body": {"signal_id": 6, "draft_response": "Thanks!"},
            "stubs": [patch(f"{_M}.get_lead_signal",
                            return_value={"user_id": _UID, "draft_response": "old"})],
            "legacy": f"{_M}.update_lead_signal", "row_id": 6,
            "fields": {"draft_response": "Thanks!"}}


def _catchup_route() -> dict:
    return {"method": "put", "path": "/api/catchup/touch", "kind": "catchup_touch",
            "body": {"touch_id": 7, "message": "Congrats!"},
            "stubs": [patch(f"{_M}.get_catchup_touch",
                            return_value={"user_id": _UID, "message": "old"})],
            "legacy": f"{_M}.update_catchup_touch", "row_id": 7,
            "fields": {"message": "Congrats!"}}


def _outreach_route() -> dict:
    return {"method": "put", "path": "/api/outreach/target", "kind": "outreach_target",
            "body": {"target_id": 8, "draft_text": "Great post"},
            "stubs": [patch(f"{_OUTREACH}.get_outreach_target_user_id", return_value=_UID)],
            "legacy": f"{_OUTREACH}.update_outreach_target", "row_id": 8,
            "fields": {"draft_text": "Great post"}}


def _update_post_route() -> dict:
    from cqc_lem.utilities.db import PostStatus, PostType

    return {"method": "post", "path": "/api/update_post/?post_id=9", "kind": "post",
            "body": {"content": "Revised", "scheduled_datetime": "2099-01-01T09:00:00Z",
                     "post_type": "video", "video_url": "https://evil.example/x.mp4",
                     "use_avatar": True, "rejection_reason": "nope"},
            "stubs": [patch(f"{_M}.user_owns_posts", return_value=True)],
            "legacy": f"{_M}.update_db_post", "row_id": 9,
            "fields": {"content": "Revised", "video_url": None,
                       "scheduled_time": datetime(2099, 1, 1, 9, tzinfo=timezone.utc),
                       "post_type": PostType.TEXT, "status": PostStatus.PENDING}}


_ROUTES = {"dm": _dm_route, "connection_request": _connection_route,
           "lead_signal": _lead_signal_route, "catchup_touch": _catchup_route,
           "outreach_target": _outreach_route, "update_post": _update_post_route}


def _call(api_client: Any, route: dict, headers: Any = None) -> Any:
    return getattr(api_client, route["method"])(route["path"],
                                                json={"session_token": _TOKEN, **route["body"]},
                                                headers=headers)


class TestEveryEditRouteIsPendingOnlyForAHeldSeat:
    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_the_edit_goes_through_the_pending_only_sql(self, api_client: Any, name: str,
                                                         seat: str, scope: str, kind: str) -> None:
        route = _ROUTES[name]()
        with ExitStack() as stack:
            for stub in route["stubs"]:
                stack.enter_context(stub)
            stack.enter_context(_session(scope, kind))
            stack.enter_context(_widened())
            pending = stack.enter_context(_pending("UPDATED"))
            legacy = stack.enter_context(patch(route["legacy"], return_value=True))
            resp = _call(api_client, route)
        assert resp.status_code == 200, resp.text
        legacy.assert_not_called()
        assert pending.call_args.args == (route["kind"], route["row_id"], _UID, route["fields"])
        assert "approved_by" not in pending.call_args.args[3], "a machine edit stamps no approver"

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_a_row_that_left_pending_is_a_409_and_nothing_is_written(
            self, api_client: Any, name: str, seat: str, scope: str, kind: str) -> None:
        route = _ROUTES[name]()
        with ExitStack() as stack:
            for stub in route["stubs"]:
                stack.enter_context(stub)
            stack.enter_context(_session(scope, kind))
            stack.enter_context(_widened())
            stack.enter_context(_pending("NOT_PENDING"))
            legacy = stack.enter_context(patch(route["legacy"], return_value=True))
            avatar = stack.enter_context(patch(f"{_M}.update_post_use_avatar"))
            reason = stack.enter_context(patch(f"{_M}.update_db_post_rejection_reason"))
            resp = _call(api_client, route)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "edit_requires_pending"
        legacy.assert_not_called()
        avatar.assert_not_called()
        reason.assert_not_called()

    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_a_database_fault_is_a_500(self, api_client: Any, name: str) -> None:
        route = _ROUTES[name]()
        with ExitStack() as stack:
            for stub in route["stubs"]:
                stack.enter_context(stub)
            stack.enter_context(_session("agent"))
            stack.enter_context(_widened())
            stack.enter_context(_pending("FAILED"))
            resp = _call(api_client, route)
        assert resp.status_code == 500


class TestTheOperatorExemptionAndHumansKeepTodaysPath:
    @pytest.mark.parametrize("seat,scope,kind", _EXEMPT, ids=[e[0] for e in _EXEMPT])
    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_an_exempt_operator_writes_through_the_original_updater(
            self, api_client: Any, name: str, seat: str, scope: str, kind: str) -> None:
        route = _ROUTES[name]()
        with ExitStack() as stack:
            for stub in route["stubs"]:
                stack.enter_context(stub)
            stack.enter_context(_session(scope, kind))
            stack.enter_context(_widened())
            pending = stack.enter_context(_pending())
            legacy = stack.enter_context(patch(route["legacy"], return_value=True))
            resp = _call(api_client, route)
        assert resp.status_code == 200, resp.text
        legacy.assert_called_once()
        pending.assert_not_called()

    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_a_full_session_writes_through_the_original_updater(self, api_client: Any,
                                                                name: str) -> None:
        route = _ROUTES[name]()
        with ExitStack() as stack:
            for stub in route["stubs"]:
                stack.enter_context(stub)
            stack.enter_context(_session("full"))
            pending = stack.enter_context(_pending())
            legacy = stack.enter_context(patch(route["legacy"], return_value=True))
            stack.enter_context(patch(f"{_M}.update_post_use_avatar"))
            stack.enter_context(patch(f"{_M}.update_db_post_rejection_reason"))
            resp = _call(api_client, route, headers=_SPA)
        assert resp.status_code == 200, resp.text
        legacy.assert_called_once()
        pending.assert_not_called()

    def test_an_exempt_operator_approval_still_writes_its_audit_row(self, api_client: Any) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        with _session("operator", "internal"), _pending() as pending, \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID), \
             patch(f"{_M}.update_connection_request", return_value=True) as update, \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _TOKEN, "request_id": 5, "action": "approve"})
        assert resp.status_code == 200, resp.text
        update.assert_called_once()
        pending.assert_not_called()
        events = [c.args[0] if c.args else c.kwargs.get("event") for c in recorded.call_args_list]
        assert events.count(AuthAuditEvent.OPERATOR_APPROVAL) == 1

    def test_the_full_session_post_edit_is_byte_identical(self, api_client: Any) -> None:
        """Every argument today's path passed, still passed: status, type and the avatar choice."""
        from cqc_lem.utilities.db import PostStatus, PostType

        with _session("full"), patch(f"{_M}.user_owns_posts", return_value=True), \
             patch(f"{_M}.update_db_post", return_value=True) as update, \
             patch(f"{_M}.update_post_use_avatar") as avatar:
            resp = api_client.post("/api/update_post/?post_id=9", headers=_SPA, json={
                "session_token": _TOKEN, "content": "c", "post_type": "video",
                "scheduled_datetime": "2099-01-01T09:00:00Z", "status": "approved",
                "use_avatar": True})
        assert resp.status_code == 200
        assert update.call_args.args[3] == PostType.VIDEO
        assert update.call_args.args[5] == PostStatus.APPROVED
        avatar.assert_called_once_with(9, True)


class TestCancelAndDismissArePendingOnlyToo:
    @pytest.mark.parametrize("path,body,kind,status", [
        ("/api/dm", {"dm_id": 4, "action": "cancel"}, "scheduled_dm", "canceled"),
        ("/api/catchup/touch", {"touch_id": 7, "action": "cancel"}, "catchup_touch", "canceled"),
        ("/api/lead_signal", {"signal_id": 6, "action": "dismiss"}, "lead_signal", "dismissed"),
        ("/api/outreach/target", {"target_id": 8, "action": "cancel"}, "outreach_target",
         "canceled"),
    ])
    def test_a_status_move_lands_only_on_a_pending_row(self, api_client: Any, path: str,
                                                       body: dict, kind: str, status: str) -> None:
        with _session("agent"), _widened(), _pending() as pending, \
             patch(f"{_M}.get_scheduled_dm_user_id", return_value=_UID), \
             patch(f"{_M}.get_catchup_touch", return_value={"user_id": _UID, "message": "m"}), \
             patch(f"{_M}.get_lead_signal", return_value={"user_id": _UID}), \
             patch(f"{_OUTREACH}.get_outreach_target_user_id", return_value=_UID):
            resp = api_client.put(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 200, resp.text
        assert pending.call_args.args[0] == kind
        assert str(pending.call_args.args[3]["status"]) == status

    def test_a_connection_request_cancel_clears_what_the_human_path_clears(self,
                                                                          api_client: Any) -> None:
        from cqc_lem.utilities.db import ConnectionRequestStatus

        with _session("agent"), _pending() as pending, \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID):
            resp = api_client.put("/api/connection_request", json={
                "session_token": _TOKEN, "request_id": 5, "action": "cancel"})
        assert resp.status_code == 200, resp.text
        assert pending.call_args.args[3] == {"status": ConnectionRequestStatus.CANCELED,
                                             "failure_reason": None, "recipient_email": None}


class TestCuratedRejectIsPendingOnly:
    _SOURCE = {"id": 4, "status": "drafted", "post_id": 9}

    def _put(self, api_client: Any, scope: str, kind: str, context: dict, outcomes: list,
             headers: Any = None) -> tuple:
        from cqc_lem.utilities.db import PendingEdit

        with _session(scope, kind), \
             patch(f"{_CURATED}.get_curated_source", return_value=self._SOURCE), \
             patch(f"{_CURATED}.get_post_curated_context", return_value=context), \
             patch(f"{_M}.update_pending_row",
                   side_effect=[getattr(PendingEdit, o) for o in outcomes]) as pending, \
             patch(f"{_CURATED}.bulk_update_posts") as bulk, \
             patch(f"{_CURATED}.update_curated_source_status", return_value=True) as status:
            resp = api_client.put("/api/curated/source", headers=headers, json={
                "session_token": _TOKEN, "source_id": 4, "action": "reject"})
        return resp, pending, bulk, status

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_a_held_seat_rejects_only_a_pending_draft(self, api_client: Any, seat: str,
                                                      scope: str, kind: str) -> None:
        from cqc_lem.utilities.db import PostStatus

        resp, pending, bulk, status = self._put(
            api_client, scope, kind, {"post_user_id": _UID, "post_status": "pending"},
            ["UPDATED", "UPDATED"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"] == {"source_id": 4, "post_id": 9, "status": "rejected"}
        post_call, source_call = pending.call_args_list
        assert post_call.args == ("post", 9, _UID, {"status": PostStatus.REJECTED,
                                                    "rejection_reason": "curated source rejected"})
        assert source_call.args == ("curated_source", 4, _UID, {"status": "rejected"})
        bulk.assert_not_called()
        status.assert_not_called()

    def test_an_approved_curated_post_is_a_409_for_a_held_seat(self, api_client: Any) -> None:
        resp, pending, bulk, status = self._put(
            api_client, "agent", "standard", {"post_user_id": _UID, "post_status": "approved"},
            ["NOT_PENDING"])
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "edit_requires_pending"
        assert pending.call_count == 1, "the source is not touched once the post refused"
        bulk.assert_not_called()
        status.assert_not_called()

    def test_a_source_with_no_owned_post_is_guarded_on_the_source_alone(self,
                                                                       api_client: Any) -> None:
        resp, pending, _bulk, _status = self._put(api_client, "agent", "standard", None,
                                                  ["NOT_PENDING"])
        assert resp.status_code == 409
        assert pending.call_args.args[0] == "curated_source"

    def test_a_human_still_withdraws_an_approved_curated_post(self, api_client: Any) -> None:
        resp, pending, bulk, status = self._put(
            api_client, "full", "standard", {"post_user_id": _UID, "post_status": "approved"},
            [], headers=_SPA)
        assert resp.status_code == 200, resp.text
        pending.assert_not_called()
        bulk.assert_called_once()
        status.assert_called_once()


class TestSchedulePostDraftsTextOnlyForAHeldSeat:
    _BODY = {"content": "Hello", "scheduled_datetime": "2099-01-01T09:00:00Z",
             "post_type": "video", "video_url": "https://evil.example/x.mp4",
             "video_quality": "premium_top", "use_avatar": True,
             "carousel_slides": ["a", "b"], "image_url": "https://lem.example/api/assets/p.png"}

    def _schedule(self, api_client: Any, scope: str, kind: str, headers: Any = None,
                  **extra: Any) -> tuple:
        with _session(scope, kind), _widened(), \
             patch(f"{_M}.get_user_email", return_value="jane@example.test"), \
             patch(f"{_M}.owns_post_image_url", return_value=True), \
             patch(f"{_M}.insert_post_returning_id", return_value=321) as insert:
            resp = api_client.post("/api/schedule_post/", headers=headers,
                                   json={"session_token": _TOKEN, **self._BODY, **extra})
        return resp, insert

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_media_is_dropped_and_the_row_is_a_pending_text_post(
            self, api_client: Any, seat: str, scope: str, kind: str) -> None:
        from cqc_lem.utilities.db import PostStatus, PostType

        resp, insert = self._schedule(api_client, scope, kind)
        assert resp.status_code == 200, resp.text
        assert resp.json()["post_id"] == 321
        args, kwargs = insert.call_args
        assert args[3] == PostType.TEXT
        assert kwargs["status"] == PostStatus.PENDING
        assert kwargs["video_url"] is None
        assert kwargs["carousel_slides"] is None
        assert kwargs["use_avatar"] is None
        assert kwargs["image_url"] is None
        assert kwargs["video_quality"] == "standard"

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_asking_for_approved_is_a_403(self, api_client: Any, seat: str, scope: str,
                                          kind: str) -> None:
        resp, insert = self._schedule(api_client, scope, kind, status="approved")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        insert.assert_not_called()

    @pytest.mark.parametrize("seat,scope,kind", _EXEMPT, ids=[e[0] for e in _EXEMPT])
    def test_an_exempt_operator_keeps_media_and_may_approve(self, api_client: Any, seat: str,
                                                            scope: str, kind: str) -> None:
        from cqc_lem.utilities.db import PostStatus, PostType

        resp, insert = self._schedule(api_client, scope, kind, status="approved")
        assert resp.status_code == 200, resp.text
        args, kwargs = insert.call_args
        assert args[3] == PostType.VIDEO
        assert kwargs["status"] == PostStatus.APPROVED
        assert kwargs["use_avatar"] is True

    def test_a_full_session_is_unchanged_but_for_the_returned_id(self, api_client: Any) -> None:
        from cqc_lem.utilities.db import PostType

        resp, insert = self._schedule(api_client, "full", "standard", headers=_SPA)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status_code": 200, "detail": "Post scheduled successfully",
                               "post_id": 321}
        args, kwargs = insert.call_args
        assert args[3] == PostType.VIDEO
        assert kwargs["video_url"] == "https://evil.example/x.mp4"
        assert kwargs["video_quality"] == "premium_top"
        assert kwargs["use_avatar"] is True
        assert kwargs["image_url"] == "https://lem.example/api/assets/p.png"

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_update_post_refuses_approved_for_a_held_seat(self, api_client: Any, seat: str,
                                                          scope: str, kind: str) -> None:
        with _session(scope, kind), _widened(), _pending() as pending, \
             patch(f"{_M}.user_owns_posts", return_value=True):
            resp = api_client.post("/api/update_post/?post_id=9", json={
                "session_token": _TOKEN, "content": "c", "status": "approved",
                "scheduled_datetime": "2099-01-01T09:00:00Z"})
        assert resp.status_code == 403
        pending.assert_not_called()


class TestBulkUpdateIsRefusedForEveryQueueOnlySeat:
    @pytest.mark.parametrize("scope,kind", [("agent", "standard"), ("operator", "standard"),
                                            ("operator", "internal")])
    def test_it_is_a_403_before_anything_is_read(self, api_client: Any, scope: str,
                                                 kind: str) -> None:
        with _session(scope, kind), _widened(), \
             patch(f"{_M}.user_owns_posts", return_value=True) as owns, \
             patch(f"{_M}.bulk_update_posts", return_value=True) as bulk:
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": _TOKEN, "post_ids": [1], "status": "rejected"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_bulk_update"
        owns.assert_not_called()
        bulk.assert_not_called()


_BAD_URLS = [
    "https://example.com/in/jane",
    "http://www.linkedin.com/in/jane",
    "https://linkedin.com/in/jane",
    "https://www.linkedin.com/company/acme",
    "https://www.linkedin.com/in/jane?trk=x",
    "https://www.linkedin.com/in/jane/details/",
    "https://www.linkedin.com.evil.example/in/jane",
    "javascript:alert(1)",
    "",
]


class TestMachineSeatsNameOnlyLinkedInProfiles:
    def _create(self, api_client: Any, scope: str, path: str, url: str,
                headers: Any = None) -> tuple:
        field = "target_profile_url" if "outreach" in path else "recipient_profile_url"
        body = {"session_token": _TOKEN, field: url, "message": "hi",
                "scheduled_datetime": "2099-01-01T09:00:00Z"}
        with _session(scope), _widened(), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=1) as conn, \
             patch(f"{_M}.insert_scheduled_dm", return_value=2) as dm, \
             patch(f"{_OUTREACH}.get_outreach_target_by_url", return_value=None), \
             patch(f"{_OUTREACH}.insert_outreach_target", return_value=3) as target:
            resp = api_client.post(path, json=body, headers=headers)
        return resp, (conn, dm, target)

    @pytest.mark.parametrize("scope", ["agent", "operator"])
    @pytest.mark.parametrize("path", ["/api/connection_request", "/api/schedule_dm",
                                      "/api/outreach/target"])
    @pytest.mark.parametrize("url", _BAD_URLS)
    def test_anything_else_is_a_422_and_nothing_is_queued(self, api_client: Any, scope: str,
                                                          path: str, url: str) -> None:
        resp, inserts = self._create(api_client, scope, path, url)
        assert resp.status_code == 422, resp.text
        if url.strip():  # the outreach route answers an empty URL with its own 422 first
            assert resp.json()["detail"]["code"] == "linkedin_profile_url_required"
        for insert in inserts:
            insert.assert_not_called()

    @pytest.mark.parametrize("path", ["/api/connection_request", "/api/schedule_dm"])
    def test_surrounding_whitespace_is_not_a_profile_url(self, api_client: Any, path: str) -> None:
        """Only the outreach create normalises its URL (it strips first); these two store it as is."""
        resp, _inserts = self._create(api_client, "agent", path, f" {_LI}")
        assert resp.status_code == 422

    @pytest.mark.parametrize("url", [_LI, "https://www.linkedin.com/in/jane",
                                     "https://www.linkedin.com/in/j%C3%A9r%C3%B4me-x_1/"])
    @pytest.mark.parametrize("path", ["/api/connection_request", "/api/schedule_dm",
                                      "/api/outreach/target"])
    def test_a_profile_url_is_queued(self, api_client: Any, path: str, url: str) -> None:
        resp, _inserts = self._create(api_client, "agent", path, url)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path", ["/api/connection_request", "/api/schedule_dm",
                                      "/api/outreach/target"])
    def test_a_human_is_not_held_to_it(self, api_client: Any, path: str) -> None:
        resp, _inserts = self._create(api_client, "full", path, "https://example.com/in/jane",
                                      headers=_SPA)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path,body,stub", [
        ("/api/connection_request", {"request_id": 5}, f"{_M}.get_connection_request_user_id"),
        ("/api/dm", {"dm_id": 4}, f"{_M}.get_scheduled_dm_user_id"),
    ])
    def test_an_edit_naming_a_new_recipient_is_held_to_it_too(self, api_client: Any, path: str,
                                                              body: dict, stub: str) -> None:
        with _session("agent"), _widened(), _pending() as pending, \
             patch(stub, return_value=_UID):
            bad = api_client.put(path, json={"session_token": _TOKEN, **body,
                                             "recipient_profile_url": "https://evil.example/x"})
            good = api_client.put(path, json={"session_token": _TOKEN, **body,
                                              "recipient_profile_url": _LI})
        assert bad.status_code == 422
        assert good.status_code == 200, good.text
        assert pending.call_count == 1


class TestThePreferencesReadNeverMintsTheInboundAddressForAMachine:
    @staticmethod
    def _get(api_client: Any, scope: str, headers: Any = None) -> tuple:
        with _session(scope), \
             patch(f"{_USER}.get_engagement_preferences",
                   return_value={"reply_check_mode": "event"}), \
             patch(f"{_USER}.has_engagement_preferences", return_value=True), \
             patch(f"{_USER}.get_or_create_reply_inbound_token", return_value="tok9") as mint, \
             patch(f"{_M}.get_gmail_forward_confirmation", return_value=None), \
             patch(f"{_USER}.max_catchup_touches_allowed", return_value=3), \
             patch("cqc_lem.app.engagement.feed.get_feed_funnel", return_value=None), \
             patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            resp = api_client.get("/api/user/engagement-preferences",
                                  params={"session_token": _TOKEN}, headers=headers)
        return resp, mint

    @pytest.mark.parametrize("scope", ["agent", "operator", "observer"])
    def test_a_machine_seat_gets_no_address_and_none_is_created(self, api_client: Any,
                                                                scope: str) -> None:
        resp, mint = self._get(api_client, scope)
        assert resp.status_code == 200, resp.text
        assert "reply_inbound_address" not in resp.json()["detail"]
        mint.assert_not_called()

    def test_a_human_still_gets_it(self, api_client: Any) -> None:
        resp, mint = self._get(api_client, "full", headers=_SPA)
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["reply_inbound_address"] == "reply+tok9@parse.example.com"
        mint.assert_called_once_with(_UID)


class TestTheGuardsStayInTheHandlers:
    """The post routes are on no machine surface, so `HANDLER_GUARDED_ROUTES` cannot see them.

    Pinned from source instead, so a deleted guard fails before a surface ever widens.
    """

    @pytest.mark.parametrize("handler,guards", [
        ("schedule_post", ("_refuse_agent_approved_status(post.status)", "_edits_pending_only()",
                           "_text_only_draft(post)")),
        ("update_post", ("_refuse_agent_approved_status(post.status)", "_edits_pending_only()",
                         '_pending_edit_or_raise("post"')),
        ("bulk_update_posts_endpoint", ("_agent_scoped()",)),
        ("update_scheduled_dm_endpoint", ('_pending_edit_or_raise("scheduled_dm"',
                                          "_require_linkedin_profile_url(")),
        ("update_catchup_touch_endpoint", ('_pending_edit_or_raise("catchup_touch"',)),
        ("update_connection_request_endpoint", ('_pending_edit_or_raise("connection_request"',
                                                "_require_linkedin_profile_url(")),
        ("update_lead_signal_endpoint", ('_pending_edit_or_raise("lead_signal"',)),
        ("schedule_dm_endpoint", ("_require_linkedin_profile_url(",)),
        ("create_connection_request_endpoint", ("_require_linkedin_profile_url(",)),
    ])
    def test_the_handler_still_carries_its_guard(self, handler: str, guards: tuple) -> None:
        import inspect

        source = inspect.getsource(getattr(_kernel(), handler))
        assert [g for g in guards if g not in source] == []

    def test_the_router_handlers_still_carry_theirs(self) -> None:
        import inspect

        from cqc_lem.api.routers import curated, outreach

        assert '_pending_edit_or_raise("outreach_target"' in inspect.getsource(
            outreach.update_outreach_target_endpoint)
        assert "_require_linkedin_profile_url(" in inspect.getsource(
            outreach.create_outreach_target_endpoint)
        assert "_edits_pending_only()" in inspect.getsource(curated.act_on_curated_source)

    def test_the_held_test_seats_are_exactly_the_non_exempt_queue_only_ones(self) -> None:
        main = _kernel()
        for scope, kind, held in (("agent", "internal", True), ("operator", "standard", True),
                                  ("operator", "internal", False), ("full", "standard", False),
                                  (None, "standard", False)):
            reset = main._request_session_scope.set(scope)
            state = main._request_operator.set({"account_kind": kind, "user_id": _UID})
            try:
                assert main._edits_pending_only() is held, (scope, kind)
            finally:
                main._request_operator.reset(state)
                main._request_session_scope.reset(reset)

    def test_a_queue_only_seat_with_no_url_field_is_not_refused(self) -> None:
        main = _kernel()
        reset = main._request_session_scope.set(main.SESSION_SCOPE_AGENT)
        try:
            main._require_linkedin_profile_url(None)
        finally:
            main._request_session_scope.reset(reset)

    def test_the_text_only_draft_drops_every_media_field(self) -> None:
        main = _kernel()
        post = main.PostRequest(content="c", scheduled_datetime=datetime(2099, 1, 1),
                                post_type="video", video_url="v", use_avatar=True,
                                video_quality="premium", image_url="i", carousel_slides=["s"],
                                status="approved")
        draft = main._text_only_draft(post)
        assert (draft.post_type, draft.status, draft.video_url, draft.use_avatar,
                draft.video_quality, draft.image_url, draft.carousel_slides) == (
            main.PostType.TEXT, main.PostStatus.PENDING, None, None, "standard", None, None)
        assert post.post_type == main.PostType.VIDEO, "the caller's body is not mutated"

