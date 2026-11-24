"""Queue-only machine seats: pending-only edits, text-only drafts, no inbound address (issue #2684).

MCP epic #2682, PR W1. Each route is driven through the REAL resolver with a session of each seat:

- `agent`, and `operator` on a `standard` account: held to pending-only edits, enforced in SQL by
  `update_pending_row`; a row that has left pending is a 409 `edit_requires_pending`.
- `operator` on an `internal`/`sandbox` account: the operator exemption, exactly today's path.
- a full human session: exactly today's path, `update_pending_row` never runs.

A path grants every verb, so the DELETE (soft-cancel) routes are edits too.

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

# (seat id, session scope, account kind)
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
    extra = {"/dm", "/catchup/touch", "/schedule_post", "/update_post", "/posts/bulk_update"}
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

def _route(method: str, path: str, kind: str, body: dict, stubs: list, legacy: str,
           row_id: int, fields: dict) -> dict:
    return {"method": method, "path": path, "kind": kind, "body": body, "stubs": stubs,
            "legacy": legacy, "row_id": row_id, "fields": fields}


def _routes() -> dict:
    from cqc_lem.utilities.db import (
        CatchupTouchStatus,
        ConnectionRequestStatus,
        OutreachStatus,
        ScheduledDmStatus,
    )

    dm_owner = [patch(f"{_M}.get_scheduled_dm_user_id", return_value=_UID)]
    request_owner = [patch(f"{_M}.get_connection_request_user_id", return_value=_UID)]
    target_owner = [patch(f"{_OUTREACH}.get_outreach_target_user_id", return_value=_UID)]
    return {
        "put_dm": _route("PUT", "/api/dm", "scheduled_dm", {"dm_id": 4, "message": "Hi Jane"},
                         dm_owner, f"{_M}.update_scheduled_dm", 4, {"message": "Hi Jane"}),
        "delete_dm": _route("DELETE", "/api/dm", "scheduled_dm", {"dm_id": 4}, dm_owner,
                            f"{_M}.update_scheduled_dm_status", 4,
                            {"status": ScheduledDmStatus.CANCELED}),
        "put_connection_request": _route(
            "PUT", "/api/connection_request", "connection_request",
            {"request_id": 5, "message": "Hello"}, request_owner,
            f"{_M}.update_connection_request", 5, {"message": "Hello"}),
        "delete_connection_request": _route(
            "DELETE", "/api/connection_request", "connection_request", {"request_id": 5},
            request_owner, f"{_M}.update_connection_request_status", 5,
            {"status": ConnectionRequestStatus.CANCELED, "failure_reason": None,
             "recipient_email": None}),
        "put_lead_signal": _route(
            "PUT", "/api/lead_signal", "lead_signal", {"signal_id": 6, "draft_response": "Thanks!"},
            [patch(f"{_M}.get_lead_signal", return_value={"user_id": _UID, "draft_response": "o"})],
            f"{_M}.update_lead_signal", 6, {"draft_response": "Thanks!"}),
        "put_catchup_touch": _route(
            "PUT", "/api/catchup/touch", "catchup_touch", {"touch_id": 7, "message": "Congrats!"},
            [patch(f"{_M}.get_catchup_touch", return_value={"user_id": _UID, "message": "old"})],
            f"{_M}.update_catchup_touch", 7, {"message": "Congrats!"}),
        "delete_catchup_touch": _route(
            "DELETE", "/api/catchup/touch", "catchup_touch", {"touch_id": 7},
            [patch(f"{_M}.get_catchup_touch_user_id", return_value=_UID)],
            f"{_M}.update_catchup_touch_status", 7, {"status": CatchupTouchStatus.CANCELED}),
        "put_outreach_target": _route(
            "PUT", "/api/outreach/target", "outreach_target",
            {"target_id": 8, "draft_text": "Great post"}, target_owner,
            f"{_OUTREACH}.update_outreach_target", 8, {"draft_text": "Great post"}),
        "delete_outreach_target": _route(
            "DELETE", "/api/outreach/target", "outreach_target", {"target_id": 8}, target_owner,
            f"{_OUTREACH}.update_outreach_target_status", 8, {"status": OutreachStatus.CANCELED}),
        # Content and slot only, so the stored type and media are never part of the write.
        "update_post": _route(
            "POST", "/api/update_post/?post_id=9", "post",
            {"content": "Revised", "scheduled_datetime": "2099-01-01T09:00:00Z",
             "rejection_reason": "nope"},
            [patch(f"{_M}.user_owns_posts", return_value=True)], f"{_M}.update_db_post", 9,
            {"content": "Revised", "scheduled_time": datetime(2099, 1, 1, 9, tzinfo=timezone.utc)}),
    }


_ROUTE_NAMES = sorted(_routes())


def _call(api_client: Any, route: dict, headers: Any = None) -> Any:
    return api_client.request(route["method"], route["path"],
                              json={"session_token": _TOKEN, **route["body"]}, headers=headers)


def _drive(api_client: Any, name: str, scope: str, kind: str = "standard",
           outcome: str = "UPDATED", headers: Any = None) -> tuple:
    route = _routes()[name]
    with ExitStack() as stack:
        for stub in route["stubs"]:
            stack.enter_context(stub)
        stack.enter_context(_session(scope, kind))
        stack.enter_context(_widened())
        pending = stack.enter_context(_pending(outcome))
        legacy = stack.enter_context(patch(route["legacy"], return_value=True))
        side = [stack.enter_context(patch(f"{_M}.update_post_use_avatar")),
                stack.enter_context(patch(f"{_M}.update_db_post_rejection_reason"))]
        resp = _call(api_client, route, headers=headers)
    return route, resp, pending, legacy, side


class TestEveryEditRouteIsPendingOnlyForAHeldSeat:
    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    @pytest.mark.parametrize("name", _ROUTE_NAMES)
    def test_the_edit_goes_through_the_pending_only_sql(self, api_client: Any, name: str,
                                                         seat: str, scope: str, kind: str) -> None:
        route, resp, pending, legacy, side = _drive(api_client, name, scope, kind)
        assert resp.status_code == 200, resp.text
        legacy.assert_not_called()
        for s in side:
            s.assert_not_called()
        assert pending.call_args.args == (route["kind"], route["row_id"], _UID, route["fields"])
        assert "approved_by" not in pending.call_args.args[3], "a machine edit stamps no approver"

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    @pytest.mark.parametrize("name", _ROUTE_NAMES)
    def test_an_approved_row_is_a_409_and_nothing_is_written(
            self, api_client: Any, name: str, seat: str, scope: str, kind: str) -> None:
        """NOT_PENDING is what the SQL answers for an approved (or sent, failed…) row."""
        _route_, resp, _pending_, legacy, side = _drive(api_client, name, scope, kind,
                                                       outcome="NOT_PENDING")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "edit_requires_pending"
        legacy.assert_not_called()
        for s in side:
            s.assert_not_called()

    @pytest.mark.parametrize("name", _ROUTE_NAMES)
    def test_a_database_fault_is_a_500(self, api_client: Any, name: str) -> None:
        _route_, resp, *_rest = _drive(api_client, name, "agent", outcome="FAILED")
        assert resp.status_code == 500


class TestTheOperatorExemptionAndHumansKeepTodaysPath:
    @pytest.mark.parametrize("seat,scope,kind", _EXEMPT, ids=[e[0] for e in _EXEMPT])
    @pytest.mark.parametrize("name", _ROUTE_NAMES)
    def test_an_exempt_operator_writes_through_the_original_updater(
            self, api_client: Any, name: str, seat: str, scope: str, kind: str) -> None:
        _route_, resp, pending, legacy, _side = _drive(api_client, name, scope, kind)
        assert resp.status_code == 200, resp.text
        legacy.assert_called_once()
        pending.assert_not_called()

    @pytest.mark.parametrize("name", _ROUTE_NAMES)
    def test_a_full_session_writes_through_the_original_updater(self, api_client: Any,
                                                                name: str) -> None:
        _route_, resp, pending, legacy, _side = _drive(api_client, name, "full", headers=_SPA)
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

    @pytest.mark.parametrize("path,body,stub,legacy", [
        ("/api/connection_request", {"request_id": 5}, "get_connection_request_user_id",
         "update_connection_request_status"),
        ("/api/dm", {"dm_id": 4}, "get_scheduled_dm_user_id", "update_scheduled_dm_status"),
        ("/api/catchup/touch", {"touch_id": 7}, "get_catchup_touch_user_id",
         "update_catchup_touch_status"),
    ])
    def test_a_full_session_delete_cancels_in_any_status_as_before(
            self, api_client: Any, path: str, body: dict, stub: str, legacy: str) -> None:
        with _session("full"), _pending() as pending, patch(f"{_M}.{stub}", return_value=_UID), \
             patch(f"{_M}.{legacy}", return_value=True) as cancel:
            resp = api_client.request("DELETE", path, headers=_SPA,
                                      json={"session_token": _TOKEN, **body})
        assert resp.status_code == 200, resp.text
        assert str(cancel.call_args.args[1]) == "canceled"
        pending.assert_not_called()


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


class TestTheBackfillEmailChallengeCarveOut:
    """backfill#44: an email-only PUT may land on an email-challenged `failed` request (#2684)."""

    @staticmethod
    def _put(api_client: Any, scope: str, outcome: str, kind: str = "standard",
             headers: Any = None, **body: Any) -> tuple:
        from cqc_lem.utilities.db import PendingEdit

        with _session(scope, kind), _pending("NOT_PENDING") as pending, \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID), \
             patch(f"{_M}.get_connection_request", return_value={"status": "pending"}), \
             patch(f"{_M}.attach_email_to_connection_request",
                   return_value=getattr(PendingEdit, outcome)) as attach, \
             patch(f"{_M}.update_connection_request", return_value=True) as legacy:
            resp = api_client.put("/api/connection_request", headers=headers,
                                  json={"session_token": _TOKEN, "request_id": 5, **body})
        return resp, attach, pending, legacy

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_an_email_only_put_goes_through_the_carve_out(self, api_client: Any, seat: str,
                                                          scope: str, kind: str) -> None:
        resp, attach, pending, legacy = self._put(api_client, scope, "UPDATED", kind,
                                                  recipient_email="jane@example.com")
        assert resp.status_code == 200, resp.text
        attach.assert_called_once_with(5, _UID, "jane@example.com")
        pending.assert_not_called()
        legacy.assert_not_called()

    def test_a_row_in_neither_state_is_a_409(self, api_client: Any) -> None:
        resp, attach, _pending_, legacy = self._put(api_client, "agent", "NOT_PENDING",
                                                    recipient_email="jane@example.com")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "edit_requires_pending"
        attach.assert_called_once()
        legacy.assert_not_called()

    @pytest.mark.parametrize("extra", [{"message": "hi"}, {"recipient_name": "Jane"},
                                       {"recipient_profile_url": _LI}, {"action": "cancel"}])
    def test_any_extra_field_takes_it_back_to_pending_only(self, api_client: Any,
                                                           extra: dict) -> None:
        """On a failed row the pending-only write matches nothing, so the extra field is a 409."""
        resp, attach, pending, legacy = self._put(api_client, "agent", "UPDATED",
                                                  recipient_email="jane@example.com", **extra)
        assert resp.status_code == 409, resp.text
        attach.assert_not_called()
        pending.assert_called_once()
        legacy.assert_not_called()

    def test_a_human_attach_still_revives_the_row(self, api_client: Any) -> None:
        """#1881's revive-on-attach is the human path, untouched."""
        from cqc_lem.utilities.db import EMAIL_VERIFICATION_REQUIRED_MESSAGE, ConnectionRequestStatus

        with _session("full"), \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID), \
             patch(f"{_M}.get_connection_request",
                   return_value={"status": "failed",
                                 "failure_reason": EMAIL_VERIFICATION_REQUIRED_MESSAGE}), \
             patch(f"{_M}.attach_email_to_connection_request") as attach, \
             patch(f"{_M}.update_connection_request", return_value=True) as legacy:
            resp = api_client.put("/api/connection_request", headers=_SPA, json={
                "session_token": _TOKEN, "request_id": 5, "recipient_email": "jane@example.com"})
        assert resp.status_code == 200, resp.text
        attach.assert_not_called()
        assert legacy.call_args.kwargs["status"] == ConnectionRequestStatus.APPROVED


class TestCuratedRejectIsPendingOnly:
    _SOURCE = {"id": 4, "status": "drafted", "post_id": 9}

    def _put(self, api_client: Any, scope: str, kind: str, context: Any, outcome: str,
             headers: Any = None) -> tuple:
        from cqc_lem.utilities.db import PendingEdit

        with _session(scope, kind), \
             patch(f"{_CURATED}.get_curated_source", return_value=self._SOURCE), \
             patch(f"{_CURATED}.get_post_curated_context", return_value=context), \
             patch(f"{_CURATED}.reject_pending_curated_source",
                   return_value=getattr(PendingEdit, outcome)) as reject, \
             patch(f"{_CURATED}.bulk_update_posts") as bulk, \
             patch(f"{_CURATED}.update_curated_source_status", return_value=True) as status:
            resp = api_client.put("/api/curated/source", headers=headers, json={
                "session_token": _TOKEN, "source_id": 4, "action": "reject"})
        return resp, reject, bulk, status

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    def test_a_held_seat_rejects_through_the_one_transaction(self, api_client: Any, seat: str,
                                                             scope: str, kind: str) -> None:
        resp, reject, bulk, status = self._put(
            api_client, scope, kind, {"post_user_id": _UID, "post_status": "pending"}, "UPDATED")
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"] == {"source_id": 4, "post_id": 9, "status": "rejected"}
        reject.assert_called_once_with(4, 9, _UID, "curated source rejected")
        bulk.assert_not_called()
        status.assert_not_called()

    def test_a_decided_row_is_a_409_and_nothing_is_written(self, api_client: Any) -> None:
        resp, _reject, bulk, status = self._put(
            api_client, "agent", "standard", {"post_user_id": _UID, "post_status": "approved"},
            "NOT_PENDING")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "edit_requires_pending"
        bulk.assert_not_called()
        status.assert_not_called()

    def test_a_source_with_no_owned_post_passes_no_post(self, api_client: Any) -> None:
        resp, reject, _bulk, _status = self._put(api_client, "agent", "standard", None,
                                                 "UPDATED")
        assert resp.status_code == 200
        assert reject.call_args.args[1] is None

    def test_a_human_still_withdraws_an_approved_curated_post(self, api_client: Any) -> None:
        resp, reject, bulk, status = self._put(
            api_client, "full", "standard", {"post_user_id": _UID, "post_status": "approved"},
            "UPDATED", headers=_SPA)
        assert resp.status_code == 200, resp.text
        reject.assert_not_called()
        bulk.assert_called_once()
        status.assert_called_once()


_MEDIA_FIELDS = [
    ("post_type", {"post_type": "video"}),
    ("post_type", {"post_type": "carousel"}),
    ("video_url", {"video_url": "https://evil.example/x.mp4"}),
    ("carousel_slides", {"carousel_slides": ["a", "b"]}),
    ("image_url", {"image_url": "https://lem.example/api/assets/p.png"}),
    ("use_avatar", {"use_avatar": True}),
    ("use_avatar", {"use_avatar": False}),
    ("video_quality", {"video_quality": "premium_top"}),
]


class TestPostsAreTextOnlyForAHeldSeat:
    _BODY = {"content": "Hello", "scheduled_datetime": "2099-01-01T09:00:00Z"}

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
    def test_a_text_draft_lands_pending_and_returns_its_id(self, api_client: Any, seat: str,
                                                           scope: str, kind: str) -> None:
        from cqc_lem.utilities.db import PostStatus, PostType

        resp, insert = self._schedule(api_client, scope, kind, post_type="text",
                                      video_quality="standard")
        assert resp.status_code == 200, resp.text
        assert resp.json()["post_id"] == 321
        args, kwargs = insert.call_args
        assert args[3] == PostType.TEXT
        assert kwargs["status"] == PostStatus.PENDING
        assert (kwargs["video_url"], kwargs["carousel_slides"], kwargs["use_avatar"],
                kwargs["image_url"], kwargs["video_quality"]) == (None, None, None, None,
                                                                  "standard")

    @pytest.mark.parametrize("seat,scope,kind", _HELD, ids=[h[0] for h in _HELD])
    @pytest.mark.parametrize("field,extra", _MEDIA_FIELDS)
    def test_any_media_field_is_a_422_not_a_silent_downgrade(
            self, api_client: Any, seat: str, scope: str, kind: str, field: str,
            extra: dict) -> None:
        resp, insert = self._schedule(api_client, scope, kind, **extra)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "machine_seat_text_only"
        assert resp.json()["detail"]["fields"] == [field]
        insert.assert_not_called()

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

        resp, insert = self._schedule(api_client, scope, kind, status="approved",
                                      post_type="video", use_avatar=True)
        assert resp.status_code == 200, resp.text
        args, kwargs = insert.call_args
        assert args[3] == PostType.VIDEO
        assert kwargs["status"] == PostStatus.APPROVED
        assert kwargs["use_avatar"] is True

    def test_a_full_session_is_unchanged_but_for_the_returned_id(self, api_client: Any) -> None:
        from cqc_lem.utilities.db import PostType

        resp, insert = self._schedule(api_client, "full", "standard", headers=_SPA,
                                      post_type="video", video_url="https://evil.example/x.mp4",
                                      video_quality="premium_top", use_avatar=True,
                                      image_url="https://lem.example/api/assets/p.png")
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
                "session_token": _TOKEN, **self._BODY, "status": "approved"})
        assert resp.status_code == 403
        pending.assert_not_called()

    @pytest.mark.parametrize("field,extra", _MEDIA_FIELDS)
    def test_update_post_refuses_any_type_or_media_change(self, api_client: Any, field: str,
                                                          extra: dict) -> None:
        with _session("agent"), _widened(), _pending() as pending, \
             patch(f"{_M}.user_owns_posts", return_value=True):
            resp = api_client.post("/api/update_post/?post_id=9", json={
                "session_token": _TOKEN, **self._BODY, **extra})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "machine_seat_text_only"
        pending.assert_not_called()

    def test_revising_a_pending_carousel_writes_neither_its_type_nor_its_media(
            self, api_client: Any) -> None:
        """The stored carousel (or video) keeps its type and slides: only content and slot move.

        The real-database half is `test_queue_only_edits_db.py`, against a stored carousel row.
        """
        with _session("agent"), _widened(), _pending() as pending, \
             patch(f"{_M}.user_owns_posts", return_value=True):
            resp = api_client.post("/api/update_post/?post_id=9", json={
                "session_token": _TOKEN, **self._BODY})
        assert resp.status_code == 200, resp.text
        assert set(pending.call_args.args[3]) == {"content", "scheduled_time"}


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
    "https://www.linkedin.com/company/acme",
    "https://www.linkedin.com/in/jane/details/",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com.evil.example/in/jane",
    "https://evil.example/www.linkedin.com/in/jane",
    "https://user:pw@www.linkedin.com/in/jane",
    "https://www.linkedin.com:8443/in/jane",
    "https://www.linkedin.com:notaport/in/jane",
    "https://[::1/in/jane",
    "ftp://www.linkedin.com/in/jane",
    "javascript:alert(1)",
    "",
]

# What the scrapers and suppliers actually store, and the one spelling each becomes.
_NORMALISED = [
    ("https://linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe/"),
    ("https://www.linkedin.com/in/jane-doe?originalSubdomain=uk",
     "https://www.linkedin.com/in/jane-doe/"),
    ("https://www.linkedin.com/in/jérôme-x_1/", "https://www.linkedin.com/in/jérôme-x_1/"),
    ("https://www.linkedin.com/in/j%C3%A9r%C3%B4me/", "https://www.linkedin.com/in/j%C3%A9r%C3%B4me/"),
    ("http://WWW.LinkedIn.com/in/Jane#about", "https://www.linkedin.com/in/Jane/"),
    ("  https://www.linkedin.com/in/jane  ", "https://www.linkedin.com/in/jane/"),
    (_LI, _LI),
]

_CREATE_PATHS = ["/api/connection_request", "/api/schedule_dm", "/api/outreach/target"]


class TestMachineSeatsNameOnlyLinkedInProfiles:
    def _create(self, api_client: Any, scope: str, path: str, url: str,
                headers: Any = None) -> tuple:
        field = "target_profile_url" if "outreach" in path else "recipient_profile_url"
        body = {"session_token": _TOKEN, field: url, "message": "hi",
                "scheduled_datetime": "2099-01-01T09:00:00Z"}
        with _session(scope), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=1) as conn, \
             patch(f"{_M}.insert_scheduled_dm", return_value=2) as dm, \
             patch(f"{_OUTREACH}.get_outreach_target_by_url", return_value=None) as dup, \
             patch(f"{_OUTREACH}.get_outreach_target_by_profile", return_value=None) as by_profile, \
             patch(f"{_OUTREACH}.insert_outreach_target", return_value=3) as target:
            resp = api_client.post(path, json=body, headers=headers)
        return resp, {"conn": conn, "dm": dm, "target": target, "dup": dup,
                      "by_profile": by_profile}

    @staticmethod
    def _stored(path: str, mocks: dict) -> str:
        insert = {"/api/connection_request": mocks["conn"], "/api/schedule_dm": mocks["dm"],
                  "/api/outreach/target": mocks["target"]}[path]
        return insert.call_args.args[1]

    @pytest.mark.parametrize("scope", ["agent", "operator"])
    @pytest.mark.parametrize("path", _CREATE_PATHS)
    @pytest.mark.parametrize("url", _BAD_URLS)
    def test_anything_else_is_a_422_and_nothing_is_queued(self, api_client: Any, scope: str,
                                                          path: str, url: str) -> None:
        resp, mocks = self._create(api_client, scope, path, url)
        assert resp.status_code == 422, resp.text
        if url.strip():  # the outreach route answers an empty URL with its own 422 first
            assert resp.json()["detail"]["code"] == "linkedin_profile_url_required"
        for name in ("conn", "dm", "target"):
            mocks[name].assert_not_called()

    @pytest.mark.parametrize("raw,stored", _NORMALISED)
    @pytest.mark.parametrize("path", _CREATE_PATHS)
    def test_the_scraper_forms_are_stored_normalised(self, api_client: Any, path: str, raw: str,
                                                     stored: str) -> None:
        resp, mocks = self._create(api_client, "agent", path, raw)
        assert resp.status_code == 200, resp.text
        assert self._stored(path, mocks) == stored

    def test_the_outreach_duplicate_lookup_sees_the_normalised_url(self, api_client: Any) -> None:
        resp, mocks = self._create(api_client, "agent", "/api/outreach/target",
                                   "https://linkedin.com/in/jane-doe?trk=x")
        assert resp.status_code == 200, resp.text
        assert mocks["by_profile"].call_args.args == (_UID, "https://www.linkedin.com/in/jane-doe/")
        mocks["dup"].assert_not_called()

    def test_a_legacy_spelling_row_is_a_duplicate_not_a_second_row(self, api_client: Any,
                                                                  fake_cursor: Any) -> None:
        """An old row stored as the scraper saw it still blocks a normalised create (409).

        Drives the REAL `get_outreach_target_by_profile` over a fake cursor holding that row.
        """
        conn, cur = fake_cursor(fetch_all=[{"id": 77, "target_profile_url":
                                            "https://linkedin.com/in/jane-doe"}])
        with _session("agent"), \
             patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch(f"{_OUTREACH}.insert_outreach_target", return_value=3) as insert:
            resp = api_client.post("/api/outreach/target", json={
                "session_token": _TOKEN,
                "target_profile_url": "https://www.linkedin.com/in/Jane-Doe?originalSubdomain=uk"})
        assert resp.status_code == 409, resp.text
        insert.assert_not_called()
        sql, params = cur.execute.call_args.args
        assert "LOWER(target_profile_url) LIKE %s" in sql
        assert params[0] == _UID and "%/in/jane-doe%" in params

    def test_a_human_create_keeps_the_exact_match(self, api_client: Any) -> None:
        resp, mocks = self._create(api_client, "full", "/api/outreach/target",
                                   "https://linkedin.com/in/jane-doe", headers=_SPA)
        assert resp.status_code == 200, resp.text
        assert mocks["dup"].call_args.args == (_UID, "https://linkedin.com/in/jane-doe")
        mocks["by_profile"].assert_not_called()

    @pytest.mark.parametrize("path", _CREATE_PATHS)
    def test_a_human_is_neither_held_to_it_nor_rewritten(self, api_client: Any, path: str) -> None:
        resp, mocks = self._create(api_client, "full", path, "https://example.com/in/jane",
                                   headers=_SPA)
        assert resp.status_code == 200, resp.text
        assert self._stored(path, mocks) == "https://example.com/in/jane"

    @pytest.mark.parametrize("path,body,stub", [
        ("/api/connection_request", {"request_id": 5}, f"{_M}.get_connection_request_user_id"),
        ("/api/dm", {"dm_id": 4}, f"{_M}.get_scheduled_dm_user_id"),
    ])
    def test_an_edit_naming_a_new_recipient_is_held_and_normalised(
            self, api_client: Any, path: str, body: dict, stub: str) -> None:
        with _session("agent"), _widened(), _pending() as pending, \
             patch(stub, return_value=_UID):
            bad = api_client.put(path, json={"session_token": _TOKEN, **body,
                                             "recipient_profile_url": "https://evil.example/x"})
            good = api_client.put(path, json={"session_token": _TOKEN, **body,
                                              "recipient_profile_url": "linkedin.com/in/jane"})
            canonical = api_client.put(path, json={
                "session_token": _TOKEN, **body,
                "recipient_profile_url": "https://linkedin.com/in/jane?originalSubdomain=uk"})
        assert bad.status_code == 422
        assert good.status_code == 422, "a URL with no scheme is not one"
        assert canonical.status_code == 200, canonical.text
        assert pending.call_count == 1
        assert pending.call_args.args[3]["recipient_profile_url"] == \
            "https://www.linkedin.com/in/jane/"


class TestThePreferencesReadNeverMintsTheInboundAddressForAMachine:
    @staticmethod
    def _get(api_client: Any, scope: str, headers: Any = None) -> tuple:
        with _session(scope), \
             patch(f"{_USER}.get_engagement_preferences",
                   return_value={"reply_check_mode": "event"}), \
             patch(f"{_USER}.has_engagement_preferences", return_value=True), \
             patch(f"{_USER}.get_or_create_reply_inbound_token", return_value="tok9") as mint, \
             patch(f"{_M}.get_gmail_forward_confirmation",
                   return_value={"auto_confirmed": False, "code": "123456"}) as gmail, \
             patch(f"{_USER}.max_catchup_touches_allowed", return_value=3), \
             patch("cqc_lem.app.engagement.feed.get_feed_funnel", return_value=None), \
             patch.dict("os.environ", {"LINKEDIN_PARSE_DOMAIN": "parse.example.com"}):
            resp = api_client.get("/api/user/engagement-preferences",
                                  params={"session_token": _TOKEN}, headers=headers)
        return resp, mint, gmail

    @pytest.mark.parametrize("scope", ["agent", "operator", "observer"])
    def test_a_machine_seat_gets_no_address_and_none_is_created(self, api_client: Any,
                                                                scope: str) -> None:
        resp, mint, gmail = self._get(api_client, scope)
        assert resp.status_code == 200, resp.text
        assert "reply_inbound_address" not in resp.json()["detail"]
        assert "gmail_forward_confirmation" not in resp.json()["detail"]
        mint.assert_not_called()
        gmail.assert_not_called()

    def test_a_human_still_gets_it(self, api_client: Any) -> None:
        resp, mint, gmail = self._get(api_client, "full", headers=_SPA)
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["reply_inbound_address"] == "reply+tok9@parse.example.com"
        assert resp.json()["detail"]["gmail_forward_confirmation"] == {"auto_confirmed": False,
                                                                       "code": "123456"}
        mint.assert_called_once_with(_UID)
        gmail.assert_called_once_with(_UID)


class TestTheGuardsStayInTheHandlers:
    """The post routes are on no machine surface, so `HANDLER_GUARDED_ROUTES` cannot see them.

    Pinned from source instead, so a deleted guard fails before a surface ever widens.
    """

    _URL = "_linkedin_profile_url_for_seat("

    @pytest.mark.parametrize("handler,guards", [
        ("schedule_post", ("_refuse_agent_approved_status(post.status)", "_edits_pending_only()",
                           "_refuse_machine_media(post, user_id=user_id)")),
        ("update_post", ("_refuse_agent_approved_status(post.status)", "_edits_pending_only()",
                         "_refuse_machine_media(post, user_id=user_id)", '_pending_edit_or_raise("post"')),
        ("bulk_update_posts_endpoint", ("_agent_scoped()",)),
        ("update_scheduled_dm_endpoint", ('_pending_edit_or_raise("scheduled_dm"', _URL)),
        ("delete_scheduled_dm_endpoint", ('_pending_edit_or_raise("scheduled_dm"',)),
        ("update_catchup_touch_endpoint", ('_pending_edit_or_raise("catchup_touch"',)),
        ("delete_catchup_touch_endpoint", ('_pending_edit_or_raise("catchup_touch"',)),
        ("update_connection_request_endpoint", ('_pending_edit_or_raise("connection_request"',
                                                "attach_email_to_connection_request(", _URL)),
        ("delete_connection_request_endpoint", ('_pending_edit_or_raise("connection_request"',)),
        ("update_lead_signal_endpoint", ('_pending_edit_or_raise("lead_signal"',)),
        ("schedule_dm_endpoint", (_URL,)),
        ("create_connection_request_endpoint", (_URL,)),
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
        assert '_pending_edit_or_raise("outreach_target"' in inspect.getsource(
            outreach.delete_outreach_target_endpoint)
        create = inspect.getsource(outreach.create_outreach_target_endpoint)
        assert create.index(self._URL) < create.index("lookup(user_id")
        assert "get_outreach_target_by_profile if _main._agent_scoped()" in create
        assert "_edits_pending_only()" in inspect.getsource(curated.act_on_curated_source)
        assert "reject_pending_curated_source(" in inspect.getsource(curated._queue_only_reject)

    def test_the_held_seats_are_exactly_the_non_exempt_queue_only_ones(self) -> None:
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
            assert main._linkedin_profile_url_for_seat(None, user_id=_UID,
                                                       action_type="x") is None
        finally:
            main._request_session_scope.reset(reset)

    def test_a_plain_text_body_passes_the_media_check(self) -> None:
        main = _kernel()
        main._refuse_machine_media(main.PostRequest(content="c",
                                                    scheduled_datetime=datetime(2099, 1, 1)),
                                   user_id=_UID)

    def test_every_media_field_is_named_in_one_refusal(self) -> None:
        from fastapi import HTTPException

        main = _kernel()
        post = main.PostRequest(content="c", scheduled_datetime=datetime(2099, 1, 1),
                                post_type="video", video_url="v", use_avatar=False,
                                video_quality="premium", image_url="i", carousel_slides=["s"])
        with pytest.raises(HTTPException) as exc:
            main._refuse_machine_media(post, user_id=_UID)
        assert exc.value.status_code == 422
        assert exc.value.detail["fields"] == ["post_type", "video_url", "carousel_slides",
                                              "image_url", "use_avatar", "video_quality"]



class TestEveryAgentWriteIsClassified:
    """`docs/identity-and-sessions.md` names every write the agent surface reaches; this holds it.

    A new reachable write route fails here until someone says whether it is a pending-only edit, a
    create, or a reviewed exception like `PUT /lead`.
    """

    PENDING_ONLY = frozenset({
        ("PUT", "/connection_request"), ("DELETE", "/connection_request"),
        ("PUT", "/lead_signal"), ("PUT", "/curated/source"),
        ("PUT", "/outreach/target"), ("DELETE", "/outreach/target"),
    })
    CREATES = frozenset({("POST", "/schedule_dm"), ("POST", "/connection_request"),
                         ("POST", "/outreach/target"), ("POST", "/curated/source")})
    EXCEPTIONS = frozenset({("PUT", "/lead"), ("PUT", "/user/engagement-preferences")})

    def test_the_agent_reaches_exactly_the_classified_writes(self) -> None:
        main = _kernel()
        reached = set()
        for route in main._walk_routes(main.app.routes):
            path = main._scope_path(getattr(route, "path", None) or "")
            for method in getattr(route, "methods", None) or ():
                if method not in ("GET", "HEAD", "OPTIONS") and \
                        main._scope_allows(main.SESSION_SCOPE_AGENT, path, method):
                    reached.add((method, path))
        assert reached == self.PENDING_ONLY | self.CREATES | self.EXCEPTIONS


@pytest.fixture
def lem_log(caplog: Any) -> Iterator[Any]:
    """caplog, wired to the `cqc-lem` logger, which does not propagate to the root handler."""
    import logging

    lem = logging.getLogger("cqc-lem")
    previous = lem.level
    caplog.handler.setLevel(logging.INFO)
    lem.addHandler(caplog.handler)
    lem.setLevel(logging.INFO)
    try:
        yield caplog
    finally:
        lem.removeHandler(caplog.handler)
        lem.setLevel(previous)


class TestEveryRefusalLeavesOneInfoLine:
    """`Queue-only seat refused: <code> | user_id=… action_type=…`, and no URL or email in it."""

    _URLISH = ("http", "linkedin.com", "@", "evil.example")

    def _refusals(self, caplog: Any, code: str) -> list:
        return [r for r in caplog.records if r.getMessage() == f"Queue-only seat refused: {code}"]

    def _assert_clean(self, record: Any, action_type: str) -> None:
        import logging

        assert record.levelno == logging.INFO
        assert record.user_id == _UID
        assert record.action_type == action_type
        text = [record.getMessage()] + [v for v in vars(record).values() if isinstance(v, str)]
        leaked = [t for t in text for needle in self._URLISH if needle in t]
        assert leaked == [], f"a refusal line carried a URL or an email: {leaked}"

    def test_edit_requires_pending(self, api_client: Any, lem_log: Any) -> None:
        with _session("agent"), _pending("NOT_PENDING"), \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID):
            resp = api_client.put("/api/connection_request", json={
                "session_token": _TOKEN, "request_id": 5, "message": "hi",
                "recipient_profile_url": _LI, "recipient_email": "jane@example.com"})
        assert resp.status_code == 409
        (record,) = self._refusals(lem_log, "edit_requires_pending")
        self._assert_clean(record, "connection_request")

    def test_machine_seat_text_only(self, api_client: Any, lem_log: Any) -> None:
        with _session("agent"), _widened(), \
             patch(f"{_M}.get_user_email", return_value="jane@example.test"), \
             patch(f"{_M}.insert_post_returning_id", return_value=1) as insert:
            resp = api_client.post("/api/schedule_post/", json={
                "session_token": _TOKEN, "content": "c", "scheduled_datetime": "2099-01-01T09:00:00Z",
                "video_url": "https://evil.example/x.mp4"})
        assert resp.status_code == 422
        insert.assert_not_called()
        (record,) = self._refusals(lem_log, "machine_seat_text_only")
        self._assert_clean(record, "post")

    def test_linkedin_profile_url_required(self, api_client: Any, lem_log: Any) -> None:
        with _session("agent"), \
             patch(f"{_M}.insert_connection_request", return_value=1) as insert:
            resp = api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": "https://evil.example/jane@example.com",
                "recipient_email": "jane@example.com"})
        assert resp.status_code == 422
        insert.assert_not_called()
        (record,) = self._refusals(lem_log, "linkedin_profile_url_required")
        self._assert_clean(record, "connection_request")

    def test_agent_may_not_bulk_update(self, api_client: Any, lem_log: Any) -> None:
        with _session("agent"), _widened():
            resp = api_client.post("/api/posts/bulk_update/", json={
                "session_token": _TOKEN, "post_ids": [1], "status": "rejected"})
        assert resp.status_code == 403
        (record,) = self._refusals(lem_log, "agent_may_not_bulk_update")
        self._assert_clean(record, "post")

    def test_a_human_request_logs_no_refusal(self, api_client: Any, lem_log: Any) -> None:
        with _session("full"), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=1):
            resp = api_client.post("/api/connection_request", headers=_SPA, json={
                "session_token": _TOKEN, "recipient_profile_url": "https://example.com/in/x"})
        assert resp.status_code == 200, resp.text
        assert not [r for r in lem_log.records
                    if r.getMessage().startswith("Queue-only seat refused")]

    def test_the_documented_grep_matches_the_real_file_line(self, api_client: Any,
                                                            lem_log: Any) -> None:
        """`docs/identity-and-sessions.md` "Healthy or broken" greps this line; hold it to that."""
        import re

        from cqc_lem.utilities.logger import _LevelFormatter

        with _session("agent"), _widened():
            api_client.post("/api/posts/bulk_update/", json={
                "session_token": _TOKEN, "post_ids": [1], "status": "rejected"})
        (record,) = self._refusals(lem_log, "agent_may_not_bulk_update")
        line = _LevelFormatter().format(record)
        assert "Queue-only seat refused: " in line
        # The doc's `grep -o` pattern, in basic-regex terms (`|` is a literal there).
        found = re.search(r"Queue-only seat refused: [a-z_]* \| user_id=[0-9]*", line)
        assert found and found.group(0) == f"Queue-only seat refused: agent_may_not_bulk_update | user_id={_UID}"
        assert "action_type=post" in line


class TestTheDraftSlotCanNeverBeBlanked:
    """A queue-only `update_post` never writes NULL into `content` or `scheduled_time`."""

    def test_omitting_the_slot_writes_only_the_content(self, api_client: Any) -> None:
        """MCP W2: a queue-only seat sends only what it revises (`PostUpdateRequest`).

        The omitted slot is left exactly as stored, never written as NULL and never written back
        from a stale read, so a slot moved meanwhile survives a content-only revise.
        """
        with _session("agent"), _widened(), _pending() as pending, \
             patch(f"{_M}.user_owns_posts", return_value=True):
            resp = api_client.post("/api/update_post/?post_id=9", json={
                "session_token": _TOKEN, "content": "c"})
        assert resp.status_code == 200, resp.text
        assert pending.call_args.args[3] == {"content": "c"}

    def test_a_human_session_still_needs_both_fields(self, api_client: Any) -> None:
        """The human path writes the whole row, so it still refuses a body without the slot."""
        with patch(f"{_M}.require_session_user_id", return_value=_UID), \
             patch(f"{_M}._require_own_posts"), patch(f"{_M}.update_db_post") as write:
            resp = api_client.post("/api/update_post/?post_id=9", json={
                "session_token": _TOKEN, "content": "c"})
        assert resp.status_code == 422
        write.assert_not_called()

    @staticmethod
    def _call_handler(**body: Any) -> Any:
        main = _kernel()
        reset = main._request_session_scope.set(main.SESSION_SCOPE_AGENT)
        try:
            with patch.object(main, "require_session_user_id", return_value=_UID), \
                 patch.object(main, "_require_own_posts"), \
                 _pending() as pending:
                # model_construct skips validation: the defensive filter must hold even when a
                # None slips past the model.
                outcome = None
                try:
                    main.update_post(9, main.PostRequest.model_construct(session_token="t", **body))
                except Exception as exc:  # noqa: BLE001 - the HTTPException is the outcome
                    outcome = exc
        finally:
            main._request_session_scope.reset(reset)
        return pending, outcome

    def test_a_none_slot_is_dropped_not_written_as_null(self) -> None:
        pending, outcome = self._call_handler(content="revised", scheduled_datetime=None)
        assert outcome is None
        assert pending.call_args.args[3] == {"content": "revised"}

    def test_nothing_left_to_write_is_a_422(self) -> None:
        pending, outcome = self._call_handler(content=None, scheduled_datetime=None)
        assert getattr(outcome, "status_code", None) == 422
        pending.assert_not_called()


class TestTheCarveOutEmailIsValidated:
    """The backfill#44 attach writes a third party's address, so it must look like one."""

    @pytest.mark.parametrize("email", ["not-an-email", "jane@", "@example.com", "jane example@x.com",
                                       "a" * 250 + "@example.com"])
    def test_a_malformed_or_overlong_address_is_a_422_and_nothing_is_written(
            self, api_client: Any, email: str) -> None:
        with _session("agent"), \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID), \
             patch(f"{_M}.attach_email_to_connection_request") as attach:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _TOKEN, "request_id": 5, "recipient_email": email})
        assert resp.status_code == 422, resp.text
        attach.assert_not_called()

    def test_surrounding_whitespace_is_trimmed_before_it_is_stored(self, api_client: Any) -> None:
        from cqc_lem.utilities.db import PendingEdit

        with _session("agent"), \
             patch(f"{_M}.get_connection_request_user_id", return_value=_UID), \
             patch(f"{_M}.attach_email_to_connection_request",
                   return_value=PendingEdit.UPDATED) as attach:
            resp = api_client.put("/api/connection_request", json={
                "session_token": _TOKEN, "request_id": 5, "recipient_email": "  jane@example.com "})
        assert resp.status_code == 200, resp.text
        attach.assert_called_once_with(5, _UID, "jane@example.com")
