"""The operator seat cannot be used to act on every user from an ordinary account.

Review found a real privilege escalation: any account with a step-up could mint an operator token and
then call `POST /admin/automation-pause`, which pauses automation for EVERY user and can only be
lifted with the owner's secret. These tests pin the fix at both ends — the mint refuses an account
that is not internal, sandbox or admin, and every use of the admin routes re-checks it — plus the
bounds on `hours`, the approval guard on every approval-capable route for each account kind, the
audit paths, and cookie authentication on the admin routes.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_ADMIN = "cqc_lem.api.routers.admin"
_CURATED = "cqc_lem.api.routers.curated"
_OUTREACH = "cqc_lem.api.routers.outreach"
_RATE = "cqc_lem.utilities.linkedin.rate_limit"

_UID = 81
_TOKEN = "operator-privilege-token"
_SECRET = "break-glass-value"
_PROFILE = "https://example.com/in/someone"


def _kernel() -> Any:
    from cqc_lem.api import main

    return main


@pytest.fixture(autouse=True)
def _account_without_a_strong_factor() -> Any:
    """Pin the factor count so the real resolver runs without a database (see test_observer_scope)."""
    with patch("cqc_lem.utilities.auth_factors.count_auth_factors", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _quiet() -> Any:
    """No admin, no access row, no audit write unless a test captures it."""
    with patch(f"{_USER}.record_auth_event", return_value=True), \
         patch.object(_kernel(), "record_auth_event", return_value=True), \
         patch.object(_kernel(), "is_user_admin", return_value=False), \
         patch(f"{_M}.claim_operator_access", return_value=False), \
         patch.object(_kernel(), "get_session_id", return_value=1):
        yield


def _session(scope: str) -> Any:
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _kind(kind: Any) -> Any:
    return patch.object(_kernel(), "get_user_account_kind", return_value=kind)


def _admin(is_admin: bool) -> Any:
    return patch.object(_kernel(), "is_user_admin", return_value=is_admin)


_KINDS = ["internal", "sandbox"]


# ---------------------------------------------------------------------------
# The mint
# ---------------------------------------------------------------------------

class TestOperatorMintGate:
    @staticmethod
    def _mint(api_client: Any, kind: Any, is_admin: bool = False, ttl_days: int = 30) -> tuple[Any, Any, Any]:
        with _session("full"), _kind(kind), _admin(is_admin), \
             patch(f"{_USER}.step_up_satisfied", return_value=True) as step_up, \
             patch(f"{_USER}.create_session", return_value="minted") as create_session:
            resp = api_client.post("/api/user/operator-token",
                                   json={"session_token": _TOKEN, "ttl_days": ttl_days})
        return resp, create_session, step_up

    @pytest.mark.parametrize("kind", ["standard", "", None, "INTERNAL", "internal ", "admin", 0])
    def test_an_account_that_is_not_internal_sandbox_or_admin_cannot_mint(
            self, api_client: Any, kind: Any) -> None:
        resp, create_session, step_up = self._mint(api_client, kind)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "operator_not_permitted"
        create_session.assert_not_called()
        step_up.assert_not_called()  # refused before any ceremony is asked for

    @pytest.mark.parametrize("kind", _KINDS)
    def test_an_internal_or_sandbox_account_can_mint(self, api_client: Any, kind: str) -> None:
        resp, create_session, _ = self._mint(api_client, kind)
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["scope"] == "operator"

    def test_an_admin_on_a_standard_account_can_mint(self, api_client: Any) -> None:
        resp, create_session, _ = self._mint(api_client, "standard", is_admin=True)
        assert resp.status_code == 200
        create_session.assert_called_once()

    def test_the_gate_is_ahead_of_the_step_up_and_still_requires_it(self, api_client: Any) -> None:
        """An eligible account still needs a fresh factor."""
        with _session("full"), _kind("internal"), \
             patch(f"{_USER}.step_up_satisfied", return_value=False), \
             patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post("/api/user/operator-token", json={"session_token": _TOKEN})
        assert resp.status_code == 403
        assert resp.json()["detail"].get("code") != "operator_not_permitted"
        create_session.assert_not_called()

    def test_a_refused_mint_logs_a_warning_with_the_user_not_the_token(self, api_client: Any) -> None:
        with patch(f"{_USER}.log_warning") as warn:
            self._mint(api_client, "standard")
        warn.assert_called_once()
        assert _TOKEN not in repr(warn.call_args)

    def test_a_database_fault_reading_the_kind_cannot_mint(self, api_client: Any, fake_cursor: Any) -> None:
        import mysql.connector

        from cqc_lem.utilities import db

        conn, _ = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert db.get_user_account_kind(1) == "standard"

    @pytest.mark.parametrize("ttl_days,expected", [(0, 422), (91, 422), (365, 422), (1, 200), (90, 200)])
    def test_the_operator_ttl_is_capped_at_90_days(self, api_client: Any, ttl_days: int,
                                                   expected: int) -> None:
        resp, create_session, _ = self._mint(api_client, "internal", ttl_days=ttl_days)
        assert resp.status_code == expected
        if expected == 422:
            create_session.assert_not_called()

    def test_the_default_operator_ttl_is_30_days(self, api_client: Any) -> None:
        with _session("full"), _kind("internal"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="minted") as create_session:
            resp = api_client.post("/api/user/operator-token", json={"session_token": _TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["expires_in_days"] == 30
        assert create_session.call_args.kwargs["ttl_hours"] == 30 * 24


# ---------------------------------------------------------------------------
# Use time: the admin routes re-check the account
# ---------------------------------------------------------------------------

class TestOperatorUseTimeGate:
    """A token minted before the gate, or on an account since demoted, is refused at use time."""

    @staticmethod
    def _pause(api_client: Any, kind: Any, is_admin: bool = False, **extra: Any) -> tuple[Any, Any]:
        with _session("operator"), _kind(kind), _admin(is_admin), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause",
                                   params={"session_token": _TOKEN, "hours": 1, **extra})
        return resp, pause

    def test_an_operator_token_on_a_standard_account_cannot_pause_automation(
            self, api_client: Any) -> None:
        resp, pause = self._pause(api_client, "standard")
        assert resp.status_code == 403
        pause.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_internal_and_sandbox_accounts_can_pause(self, api_client: Any, kind: str) -> None:
        resp, pause = self._pause(api_client, kind)
        assert resp.status_code == 200
        pause.assert_called_once()
        assert pause.call_args.args[0] == 3600

    def test_an_admin_on_a_standard_account_can_pause(self, api_client: Any) -> None:
        resp, pause = self._pause(api_client, "standard", is_admin=True)
        assert resp.status_code == 200
        pause.assert_called_once()

    @pytest.mark.parametrize("kind", ["standard", "", None, "garbage"])
    def test_an_unreadable_or_unknown_kind_is_refused(self, api_client: Any, kind: Any) -> None:
        resp, pause = self._pause(api_client, kind)
        assert resp.status_code == 403
        pause.assert_not_called()

    def test_the_status_and_task_routes_are_gated_the_same_way(self, api_client: Any) -> None:
        with _session("operator"), _kind("standard"), \
             patch(f"{_RATE}.automation_pause_remaining", return_value=0) as remaining, \
             patch("cqc_lem.app.my_celery.app.AsyncResult") as result:
            status = api_client.get("/api/admin/automation-status", params={"session_token": _TOKEN})
            task = api_client.get("/api/admin/task-status/abc", params={"session_token": _TOKEN})
        assert status.status_code == 403 and task.status_code == 403
        remaining.assert_not_called()
        result.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_the_gated_routes_still_work_on_an_eligible_account(self, api_client: Any, kind: str) -> None:
        class _Result:
            state = "PENDING"

            @staticmethod
            def ready() -> bool:
                return False

        with _session("operator"), _kind(kind), \
             patch(f"{_RATE}.automation_pause_remaining", return_value=0), \
             patch(f"{_RATE}.rate_limit_cooldown_remaining", return_value=0), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            status = api_client.get("/api/admin/automation-status", params={"session_token": _TOKEN})
            task = api_client.get("/api/admin/task-status/abc", params={"session_token": _TOKEN})
        assert status.status_code == 200 and task.status_code == 200

    def test_the_owners_secret_still_works_for_a_demoted_operators_request(self, api_client: Any) -> None:
        """The secret is the break-glass; an ineligible token beside it does not defeat it."""
        with _session("operator"), _kind("standard"), patch(f"{_ADMIN}.ADMIN_SECRET", _SECRET), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause",
                                   params={"session_token": _TOKEN, "hours": 1},
                                   headers={"X-Admin-Secret": _SECRET})
        assert resp.status_code == 200
        pause.assert_called_once()

    def test_a_refusal_at_use_time_logs_a_warning(self, api_client: Any) -> None:
        with patch(f"{_ADMIN}.log_warning") as warn:
            self._pause(api_client, "standard")
        warn.assert_called_once()
        assert _TOKEN not in repr(warn.call_args)


# ---------------------------------------------------------------------------
# automation-pause hours
# ---------------------------------------------------------------------------

class TestPauseHoursAreBounded:
    @staticmethod
    def _pause(api_client: Any, hours: str, secret: bool = False) -> tuple[Any, Any]:
        with _session("operator"), _kind("internal"), patch(f"{_ADMIN}.ADMIN_SECRET", _SECRET), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            if secret:
                resp = api_client.post("/api/admin/automation-pause", params={"hours": hours},
                                       headers={"X-Admin-Secret": _SECRET})
            else:
                resp = api_client.post("/api/admin/automation-pause",
                                       params={"session_token": _TOKEN, "hours": hours})
        return resp, pause

    @pytest.mark.parametrize("hours", ["25", "24.0001", "1000", "-1", "-0.5", "inf", "-inf", "nan",
                                       "1e999"])
    def test_an_operator_value_out_of_range_or_not_finite_is_a_422(self, api_client: Any,
                                                                   hours: str) -> None:
        resp, pause = self._pause(api_client, hours)
        assert resp.status_code == 422, (hours, resp.status_code, resp.text)
        pause.assert_not_called()

    @pytest.mark.parametrize("hours,seconds", [("24", 86400), ("0.5", 1800), ("1", 3600), ("0", 3600)])
    def test_an_operator_value_in_range_pauses(self, api_client: Any, hours: str, seconds: int) -> None:
        resp, pause = self._pause(api_client, hours)
        assert resp.status_code == 200
        assert pause.call_args.args[0] == seconds

    @pytest.mark.parametrize("hours", ["inf", "nan"])
    def test_the_owners_secret_is_also_refused_a_non_finite_value_not_a_500(
            self, api_client: Any, hours: str) -> None:
        resp, pause = self._pause(api_client, hours, secret=True)
        assert resp.status_code == 422
        pause.assert_not_called()

    def test_the_owners_secret_is_not_capped_at_24_hours(self, api_client: Any) -> None:
        resp, pause = self._pause(api_client, "72", secret=True)
        assert resp.status_code == 200
        assert pause.call_args.args[0] == 72 * 3600

    def test_an_unauthenticated_caller_gets_a_403_not_a_validation_hint(self, api_client: Any) -> None:
        with patch(f"{_ADMIN}.ADMIN_SECRET", _SECRET), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause", params={"hours": "inf"})
        assert resp.status_code == 403
        pause.assert_not_called()


# ---------------------------------------------------------------------------
# Approval on every approval-capable route, per account kind
# ---------------------------------------------------------------------------

def _approvals(recorded: Any) -> list[Any]:
    return [c for c in recorded.call_args_list if str(c.args[0]) == "operator_approval"]


class TestApprovalOnEveryRoute:
    """Standard: 403, nothing written, nothing dispatched. Internal/sandbox: succeeds, one audit row."""

    # ---- PUT /connection_request: approve and retry --------------------------------------------
    @pytest.mark.parametrize("action", ["approve", "retry"])
    def test_connection_request_on_a_standard_account(self, api_client: Any, action: str) -> None:
        with _session("operator"), _kind("standard"), \
             patch.object(_kernel(), "get_connection_request_user_id", return_value=_UID), \
             patch.object(_kernel(), "get_connection_request", return_value={"status": "failed"}), \
             patch.object(_kernel(), "update_connection_request", return_value=True) as update:
            resp = api_client.put("/api/connection_request",
                                  json={"session_token": _TOKEN, "request_id": 5, "action": action})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        update.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("action", ["approve", "retry"])
    def test_connection_request_on_an_internal_or_sandbox_account(
            self, api_client: Any, kind: str, action: str) -> None:
        with _session("operator"), _kind(kind), \
             patch.object(_kernel(), "get_connection_request_user_id", return_value=_UID), \
             patch.object(_kernel(), "get_connection_request", return_value={"status": "failed"}), \
             patch.object(_kernel(), "update_connection_request", return_value=True) as update, \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/connection_request",
                                  json={"session_token": _TOKEN, "request_id": 5, "action": action})
        assert resp.status_code == 200, resp.text
        update.assert_called_once()
        assert len(_approvals(recorded)) == 1
        assert _approvals(recorded)[0].kwargs["details"]["via"] == "action"

    # ---- PUT /lead_signal approve ---------------------------------------------------------------
    def test_lead_signal_on_a_standard_account_dispatches_nothing(self, api_client: Any) -> None:
        with _session("operator"), _kind("standard"), \
             patch.object(_kernel(), "get_lead_signal",
                          return_value={"user_id": _UID, "draft_response": "hi"}), \
             patch.object(_kernel(), "update_lead_signal", return_value=True) as update, \
             patch.object(_kernel(), "send_lead_response") as send:
            resp = api_client.put("/api/lead_signal",
                                  json={"session_token": _TOKEN, "signal_id": 3, "action": "approve"})
        assert resp.status_code == 403
        update.assert_not_called()
        send.apply_async.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_lead_signal_on_an_internal_or_sandbox_account(self, api_client: Any, kind: str) -> None:
        with _session("operator"), _kind(kind), \
             patch.object(_kernel(), "get_lead_signal",
                          return_value={"user_id": _UID, "draft_response": "hi"}), \
             patch.object(_kernel(), "update_lead_signal", return_value=True) as update, \
             patch.object(_kernel(), "send_lead_response") as send, \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/lead_signal",
                                  json={"session_token": _TOKEN, "signal_id": 3, "action": "approve"})
        assert resp.status_code == 200, resp.text
        update.assert_called_once()
        send.apply_async.assert_called_once()
        assert len(_approvals(recorded)) == 1

    # ---- PUT /outreach/target approve -----------------------------------------------------------
    def test_outreach_target_on_a_standard_account(self, api_client: Any) -> None:
        with _session("operator"), _kind("standard"), \
             patch(f"{_OUTREACH}.get_outreach_target_user_id", return_value=_UID), \
             patch(f"{_OUTREACH}.update_outreach_target", return_value=True) as update:
            resp = api_client.put("/api/outreach/target",
                                  json={"session_token": _TOKEN, "target_id": 2, "action": "approve"})
        assert resp.status_code == 403
        update.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_outreach_target_on_an_internal_or_sandbox_account(self, api_client: Any, kind: str) -> None:
        with _session("operator"), _kind(kind), \
             patch(f"{_OUTREACH}.get_outreach_target_user_id", return_value=_UID), \
             patch(f"{_OUTREACH}.update_outreach_target", return_value=True) as update, \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/outreach/target",
                                  json={"session_token": _TOKEN, "target_id": 2, "action": "approve"})
        assert resp.status_code == 200, resp.text
        update.assert_called_once()
        assert len(_approvals(recorded)) == 1

    # ---- PUT /curated/source approve ------------------------------------------------------------
    @staticmethod
    def _curated_patches() -> list[Any]:
        from cqc_lem.platform.db.enums import PostStatus
        from cqc_lem.utilities.curated_sources import STATUS_DRAFTED

        return [
            patch(f"{_CURATED}.get_curated_source",
                  return_value={"id": 4, "status": STATUS_DRAFTED, "post_id": 9}),
            patch(f"{_CURATED}.get_post_curated_context",
                  return_value={"post_user_id": _UID, "post_status": PostStatus.PENDING.value}),
        ]

    def test_curated_source_on_a_standard_account(self, api_client: Any) -> None:
        a, b = self._curated_patches()
        with _session("operator"), _kind("standard"), a, b, \
             patch(f"{_CURATED}.update_db_post_status", return_value=True) as post, \
             patch(f"{_CURATED}.update_curated_source_status", return_value=True) as source:
            resp = api_client.put("/api/curated/source",
                                  json={"session_token": _TOKEN, "source_id": 4, "action": "approve"})
        assert resp.status_code == 403
        post.assert_not_called()
        source.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_curated_source_on_an_internal_or_sandbox_account(self, api_client: Any, kind: str) -> None:
        a, b = self._curated_patches()
        with _session("operator"), _kind(kind), a, b, \
             patch(f"{_CURATED}.update_db_post_status", return_value=True) as post, \
             patch(f"{_CURATED}.update_curated_source_status", return_value=True), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.put("/api/curated/source",
                                  json={"session_token": _TOKEN, "source_id": 4, "action": "approve"})
        assert resp.status_code == 200, resp.text
        post.assert_called_once()
        assert len(_approvals(recorded)) == 1

    def test_an_agent_cannot_approve_a_curated_source_either(self, api_client: Any) -> None:
        """Regression: an agent token once approved a curated source.

        The guard ran before the session was resolved, saw no scope at all, and let every caller
        through.
        """
        a, b = self._curated_patches()
        with _session("agent"), a, b, \
             patch(f"{_CURATED}.update_db_post_status", return_value=True) as post:
            resp = api_client.put("/api/curated/source",
                                  json={"session_token": _TOKEN, "source_id": 4, "action": "approve"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        post.assert_not_called()

    def test_a_human_session_can_still_approve_a_curated_source(self, api_client: Any) -> None:
        a, b = self._curated_patches()
        with _session("full"), a, b, \
             patch(f"{_CURATED}.update_db_post_status", return_value=True) as post, \
             patch(f"{_CURATED}.update_curated_source_status", return_value=True):
            resp = api_client.put("/api/curated/source",
                                  json={"session_token": _TOKEN, "source_id": 4, "action": "approve"},
                                  headers={"X-LEM-Client": "spa"})
        assert resp.status_code == 200, resp.text
        post.assert_called_once()

    # ---- PUT /dm and PUT /catchup/touch: guarded, and NOT on the operator surface ---------------
    @pytest.mark.parametrize("kind", ["standard", *_KINDS])
    @pytest.mark.parametrize("path,body", [
        ("/api/dm", {"dm_id": 1, "action": "approve"}),
        ("/api/catchup/touch", {"touch_id": 1, "action": "approve"}),
    ])
    def test_dm_and_catchup_approval_are_not_reachable_by_an_operator_on_any_kind(
            self, api_client: Any, kind: str, path: str, body: dict[str, Any]) -> None:
        """The resolver refuses the token before the handler: neither route is on the surface.

        Granting an operator DM or catch-up approval would be a widening, not a fix.
        """
        with _session("operator"), _kind(kind), \
             patch.object(_kernel(), "update_scheduled_dm", return_value=True) as dm, \
             patch.object(_kernel(), "update_catchup_touch", return_value=True) as touch:
            resp = api_client.put(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        dm.assert_not_called()
        touch.assert_not_called()

    @staticmethod
    def _as_operator(kind: Any) -> Any:
        main = _kernel()
        return (main._request_session_scope.set(main.SESSION_SCOPE_OPERATOR),
                main._request_operator.set({"account_kind": kind, "user_id": _UID}))

    @staticmethod
    def _restore(resets: Any) -> None:
        main = _kernel()
        main._request_operator.reset(resets[1])
        main._request_session_scope.reset(resets[0])

    @pytest.mark.parametrize("kind", ["standard", "", None])
    def test_the_dm_handler_itself_refuses_a_standard_operator(self, kind: Any) -> None:
        """Defence in depth: should either route ever be added to the surface, the handler guard holds."""
        from fastapi import HTTPException

        main = _kernel()
        resets = self._as_operator(kind)
        try:
            with patch.object(main, "get_session_user_id", return_value=_UID), \
                 patch.object(main, "get_scheduled_dm_user_id", return_value=_UID), \
                 patch.object(main, "update_scheduled_dm", return_value=True) as update:
                with pytest.raises(HTTPException) as exc:
                    main.update_scheduled_dm_endpoint(
                        main.UpdateDmRequest(session_token="t", dm_id=1, action="approve"))
        finally:
            self._restore(resets)
        assert exc.value.status_code == 403
        update.assert_not_called()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_the_dm_and_catchup_handlers_approve_for_an_internal_operator_and_audit_once(
            self, kind: str) -> None:
        main = _kernel()
        resets = self._as_operator(kind)
        try:
            with patch.object(main, "get_session_user_id", return_value=_UID), \
                 patch.object(main, "get_scheduled_dm_user_id", return_value=_UID), \
                 patch.object(main, "get_scheduled_dm",
                              return_value={"scheduled_time": datetime(2099, 1, 1, tzinfo=timezone.utc)}), \
                 patch.object(main, "scheduled_dm_is_stale", return_value=False), \
                 patch.object(main, "update_scheduled_dm", return_value=True) as dm, \
                 patch.object(main, "get_catchup_touch",
                              return_value={"user_id": _UID, "message": "hello"}), \
                 patch.object(main, "update_catchup_touch", return_value=True) as touch, \
                 patch.object(main, "record_auth_event", return_value=True) as recorded:
                main.update_scheduled_dm_endpoint(
                    main.UpdateDmRequest(session_token="t", dm_id=1, action="approve"))
                main.update_catchup_touch_endpoint(
                    main.UpdateCatchupTouchRequest(session_token="t", touch_id=1, action="approve"))
        finally:
            self._restore(resets)
        dm.assert_called_once()
        touch.assert_called_once()
        assert len(_approvals(recorded)) == 2, "one operator_approval row per approval"

    def test_the_catchup_handler_refuses_a_standard_operator(self) -> None:
        from fastapi import HTTPException

        main = _kernel()
        resets = self._as_operator("standard")
        try:
            with patch.object(main, "get_session_user_id", return_value=_UID), \
                 patch.object(main, "get_catchup_touch", return_value={"user_id": _UID, "message": "m"}), \
                 patch.object(main, "update_catchup_touch", return_value=True) as update:
                with pytest.raises(HTTPException) as exc:
                    main.update_catchup_touch_endpoint(
                        main.UpdateCatchupTouchRequest(session_token="t", touch_id=1, action="approve"))
        finally:
            self._restore(resets)
        assert exc.value.status_code == 403
        update.assert_not_called()


# ---------------------------------------------------------------------------
# Audit path
# ---------------------------------------------------------------------------

class TestOperatorAuditPaths:
    def test_a_non_get_to_task_status_stores_the_template_not_the_id(self) -> None:
        main = _kernel()
        resets = [(main._request_object,
                   main._request_object.set(SimpleNamespace(method="POST", headers={}, client=None))),
                  (main._request_path, main._request_path.set("/api/admin/task-status/secret-id-42")),
                  (main._request_operator, main._request_operator.set(None))]
        try:
            with patch.object(main, "get_user_account_kind", return_value="internal"), \
                 patch.object(main, "record_auth_event", return_value=True) as recorded:
                main._stamp_operator(_UID, "/api/admin/task-status/secret-id-42", None)
        finally:
            for var, reset in reversed(resets):
                var.reset(reset)
        rows = [c for c in recorded.call_args_list if str(c.args[0]) == "operator_action"]
        assert len(rows) == 1
        assert rows[0].kwargs["details"]["path"] == "/admin/task-status/{id}"
        assert "secret-id-42" not in repr(recorded.call_args_list)

    def test_an_operator_pause_writes_an_operator_action_row(self, api_client: Any) -> None:
        with _session("operator"), _kind("internal"), \
             patch(f"{_RATE}.pause_automation", return_value=True), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = api_client.post("/api/admin/automation-pause",
                                   params={"session_token": _TOKEN, "hours": 2})
        assert resp.status_code == 200
        rows = [c for c in recorded.call_args_list if str(c.args[0]) == "operator_action"]
        assert len(rows) == 1
        assert rows[0].kwargs["details"] == {"role": "operator", "method": "POST",
                                             "path": "/admin/automation-pause",
                                             "account_kind": "internal"}

    def test_an_approval_row_never_carries_a_templated_id(self, api_client: Any) -> None:
        with _session("operator"), _kind("internal"), \
             patch.object(_kernel(), "insert_connection_request", return_value=1), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": _PROFILE, "status": "approved"})
        details = _approvals(recorded)[0].kwargs["details"]
        assert details["path"] == "/connection_request"


# ---------------------------------------------------------------------------
# Authentication on the admin routes without a query token
# ---------------------------------------------------------------------------

class TestAdminRoutesAcceptTheSessionCookie:
    """`session_token` in a query string can reach an access log; the cookie form must work too."""

    def test_the_operator_can_call_every_operator_admin_route_with_only_the_cookie(
            self, api_client: Any) -> None:
        class _Result:
            state = "PENDING"

            @staticmethod
            def ready() -> bool:
                return False

        cookie = {"Cookie": f"lem_session={_TOKEN}", "X-LEM-Client": "spa"}
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "operator"} if t == _TOKEN else None), \
             _kind("internal"), \
             patch(f"{_RATE}.automation_pause_remaining", return_value=0), \
             patch(f"{_RATE}.rate_limit_cooldown_remaining", return_value=0), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause, \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            status = api_client.get("/api/admin/automation-status", headers=cookie)
            task = api_client.get("/api/admin/task-status/abc", headers=cookie)
            paused = api_client.post("/api/admin/automation-pause", params={"hours": 1}, headers=cookie)
        assert (status.status_code, task.status_code, paused.status_code) == (200, 200, 200)
        pause.assert_called_once()

    def test_a_state_changing_cookie_call_still_needs_the_client_header(self, api_client: Any) -> None:
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "operator"} if t == _TOKEN else None), \
             _kind("internal"), patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause", params={"hours": 1},
                                   headers={"Cookie": f"lem_session={_TOKEN}"})
        assert resp.status_code == 403
        pause.assert_not_called()

    def test_an_authorization_header_is_not_a_session_credential(self, api_client: Any) -> None:
        with _session("operator"), _kind("internal"), patch(f"{_ADMIN}.ADMIN_SECRET", _SECRET), \
             patch(f"{_RATE}.pause_automation", return_value=True) as pause:
            resp = api_client.post("/api/admin/automation-pause", params={"hours": 1},
                                   headers={"Authorization": f"Bearer {_TOKEN}"})
        assert resp.status_code == 403
        pause.assert_not_called()

