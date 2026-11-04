"""The `operator` scope, held to the lessons the observer's reviews taught.

Written up front rather than after review: operator refusals are logged at WARNING, operator access is
recorded at most once per session per day, no route the operator gained returns a third party's
email or phone, the mint's label is validated, and the operator can neither mint a token nor reach a
credential, a cap or an approval mode — on any account kind — while an approval on a `standard`
account is a 403.
"""

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_ADMIN = "cqc_lem.api.routers.admin"

_UID = 71
_TOKEN = "operator-hardening-token"
_EMAIL = "lead@example.com"
_PHONE = "+1-555-0100"


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
    with patch(f"{_USER}.record_auth_event", return_value=True), \
         patch.object(_kernel(), "record_auth_event", return_value=True), \
         patch(f"{_M}.claim_operator_access", return_value=False), \
         patch.object(_kernel(), "get_session_id", return_value=1):
        yield


def _session(scope: str) -> Any:
    return patch(f"{_M}._db_resolve_session",
                 side_effect=lambda t: {"user_id": _UID, "scope": scope} if t == _TOKEN else None)


def _kind(kind: str) -> Any:
    return patch(f"{_M}.get_user_account_kind", return_value=kind)


# ---------------------------------------------------------------------------
# Refusals are WARNINGs
# ---------------------------------------------------------------------------

class TestOperatorRefusalsAreLoggedAtWarning:
    _MSG = "Machine session refused outside its scope"

    @staticmethod
    def _refuse(method: str, path: str, token: str = "op-tok-123") -> Any:
        from fastapi import HTTPException

        main = _kernel()
        resets = [(main._request_object, main._request_object.set(SimpleNamespace(method=method))),
                  (main._request_path, main._request_path.set(path))]
        try:
            with pytest.raises(HTTPException) as exc:
                main._scope_checked({"user_id": _UID, "scope": "operator"}, token)
        finally:
            for var, reset in reversed(resets):
                var.reset(reset)
        return exc.value

    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/user/operator-token"), ("GET", "/api/user/linkedin-cookie"),
        ("POST", "/api/admin/automation-resume"), ("POST", "/api/user/sessions/revoke"),
        ("GET", "/api/admin/task-status/a/b"),
    ])
    def test_an_operator_refusal_warns_with_a_stable_template(self, method: str, path: str) -> None:
        with patch.object(_kernel(), "log_warning") as warn:
            exc = self._refuse(method, path)
        assert exc.status_code == 403
        warn.assert_called_once()
        assert warn.call_args.args[0] == self._MSG
        kwargs = warn.call_args.kwargs
        assert kwargs["scope"] == "operator"
        assert kwargs["method"] == method
        assert "op-tok-123" not in repr(warn.call_args)

    def test_a_refusal_writes_no_audit_row(self) -> None:
        with patch.object(_kernel(), "record_auth_event") as recorded:
            self._refuse("POST", "/api/user/operator-token")
        recorded.assert_not_called()

    def test_the_operator_is_among_the_warned_scopes(self) -> None:
        main = _kernel()
        assert main._WARN_ON_REFUSAL_SCOPES == {main.SESSION_SCOPE_AGENT, main.SESSION_SCOPE_OBSERVER,
                                                main.SESSION_SCOPE_OPERATOR}


# ---------------------------------------------------------------------------
# Access record: at most once per session per day
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.calls.append({"key": key, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class TestOperatorAccessRecord:
    @pytest.fixture
    def redis(self) -> Any:
        from cqc_lem.utilities import observer_access

        fake = _FakeRedis()
        with patch.object(observer_access, "shared_redis_client", return_value=fake), \
             patch(f"{_M}.claim_operator_access", observer_access.claim_operator_access):
            yield fake

    @staticmethod
    def _rows(recorded: Any, name: str) -> list[Any]:
        return [c for c in recorded.call_args_list if str(c.args[0]) == name]

    @staticmethod
    def _read(api_client: Any, token: str = _TOKEN) -> Any:
        with _session("operator"), _kind("standard"), \
             patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "operator"}), \
             patch(f"{_M}.get_scheduled_dms",
                   return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}):
            return api_client.get("/api/dms", params={"session_token": token})

    def test_a_read_writes_one_access_row_the_first_time_and_none_after(
            self, api_client: Any, redis: _FakeRedis) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             patch.object(_kernel(), "get_session_id", return_value=55):
            statuses = [self._read(api_client).status_code for _ in range(3)]
        assert statuses == [200, 200, 200]
        rows = self._rows(recorded, "operator_access")
        assert len(rows) == 1
        kwargs = rows[0].kwargs
        assert kwargs["user_id"] == _UID and kwargs["session_id"] == 55
        assert kwargs["details"] == {"scope": "operator", "path": "/dms", "account_kind": "standard"}
        assert set(kwargs) == {"user_id", "session_id", "details"}, "no body, address or user agent"

    def test_it_is_per_session_and_per_day_with_its_own_key_space(self, redis: _FakeRedis) -> None:
        from cqc_lem.utilities import observer_access

        assert observer_access.claim_operator_access("s-a") is True
        assert observer_access.claim_operator_access("s-a") is False
        assert observer_access.claim_operator_access("s-b") is True
        assert observer_access.claim_observer_access("s-a") is True, "observer keys are separate"
        assert re.fullmatch(r"lem:operator_access:\d{8}:s-a", redis.calls[0]["key"])
        assert redis.calls[0]["ex"] == 25 * 3600 and redis.calls[0]["nx"] is True

    def test_a_task_id_never_reaches_the_audit_path(self, api_client: Any, redis: _FakeRedis) -> None:
        class _Result:
            state = "PENDING"

            @staticmethod
            def ready() -> bool:
                return False

        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             _session("operator"), _kind("standard"), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            resp = api_client.get("/api/admin/task-status/secret-task-id-9",
                                  params={"session_token": _TOKEN})
        assert resp.status_code == 200
        details = self._rows(recorded, "operator_access")[0].kwargs["details"]
        assert details["path"] == "/admin/task-status/{id}"
        assert "secret-task-id-9" not in repr(recorded.call_args_list)

    def test_a_redis_outage_still_serves_the_request(self, api_client: Any) -> None:
        from cqc_lem.utilities import observer_access

        class _Down:
            def set(self, *args: Any, **kwargs: Any) -> None:
                raise ConnectionError("redis is down")

        with patch.object(observer_access, "shared_redis_client", return_value=_Down()), \
             patch(f"{_M}.claim_operator_access", observer_access.claim_operator_access), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded:
            resp = self._read(api_client)
        assert resp.status_code == 200
        assert len(self._rows(recorded, "operator_access")) == 1

    @pytest.mark.parametrize("failure", ["record", "session_id"])
    def test_a_failed_audit_write_never_turns_a_request_into_an_error(
            self, api_client: Any, redis: _FakeRedis, failure: str) -> None:
        boom = RuntimeError("db down")
        with patch.object(_kernel(), "record_auth_event",
                          side_effect=boom if failure == "record" else None), \
             patch.object(_kernel(), "get_session_id",
                          side_effect=boom if failure == "session_id" else None, return_value=1):
            resp = self._read(api_client)
        assert resp.status_code == 200

    def test_an_action_still_writes_its_own_row_in_addition(self, api_client: Any,
                                                            redis: _FakeRedis) -> None:
        with patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             _session("operator"), _kind("standard"), \
             patch(f"{_M}.get_engagement_preferences", return_value={}), \
             patch(f"{_M}.insert_connection_request", return_value=90):
            api_client.post("/api/connection_request", json={
                "session_token": _TOKEN, "recipient_profile_url": "https://example.com/in/x"})
        assert len(self._rows(recorded, "operator_access")) == 1
        assert len(self._rows(recorded, "operator_action")) == 1

    def test_the_events_are_varchar_values(self) -> None:
        from cqc_lem.utilities.db import AuthAuditEvent

        assert AuthAuditEvent.OPERATOR_ACCESS == "operator_access"
        assert len("operator_access") <= 50  # auth_audit_log.event is VARCHAR(50)

    def test_the_observers_record_is_unchanged(self, api_client: Any, redis: _FakeRedis) -> None:
        from cqc_lem.utilities import observer_access

        with patch(f"{_M}.claim_observer_access", observer_access.claim_observer_access), \
             patch.object(_kernel(), "record_auth_event", return_value=True) as recorded, \
             patch.object(_kernel(), "get_session_id", return_value=3), \
             patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "observer"}), \
             patch(f"{_M}.get_scheduled_dms",
                   return_value={"dms": [], "total": 0, "page": 1, "page_size": 25}):
            api_client.get("/api/dms", params={"session_token": _TOKEN})
        rows = self._rows(recorded, "observer_access")
        assert len(rows) == 1
        assert rows[0].kwargs["details"] == {"scope": "observer", "path": "/dms"}


# ---------------------------------------------------------------------------
# No new route returns a third party's email or phone
# ---------------------------------------------------------------------------

class TestNoOperatorOnlyRouteReturnsContactDetails:
    @staticmethod
    def _assert_clean(text: str) -> None:
        assert _EMAIL not in text and _PHONE not in text and "555-0100" not in text

    def test_task_status_withholds_the_result_text_from_an_operator(self, api_client: Any) -> None:
        class _Result:
            state = "SUCCESS"
            result = f"sent to {_EMAIL} / {_PHONE}"

            @staticmethod
            def ready() -> bool:
                return True

        with _session("operator"), _kind("standard"), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            resp = api_client.get("/api/admin/task-status/abc", params={"session_token": _TOKEN})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["state"] == "SUCCESS" and detail["result_withheld"] is True
        assert "result" not in detail
        self._assert_clean(resp.text)

    def test_the_owners_bearer_and_secret_pair_still_gets_the_result(self, api_client: Any) -> None:
        class _Result:
            state = "SUCCESS"
            result = "done"

            @staticmethod
            def ready() -> bool:
                return True

        with patch(f"{_ADMIN}.ADMIN_SECRET", "break-glass"), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=_Result()):
            resp = api_client.get("/api/admin/task-status/abc",
                                  headers={"X-Admin-Secret": "break-glass"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["result"] == "done"

    def test_the_other_operator_only_routes_return_counters_and_fixed_text(
            self, api_client: Any) -> None:
        with _session("operator"), _kind("standard"), \
             patch(f"{_ADMIN}.ADMIN_SECRET", "x"), \
             patch("cqc_lem.utilities.linkedin.rate_limit.automation_pause_remaining", return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining", return_value=0), \
             patch("cqc_lem.utilities.linkedin.rate_limit.pause_automation", return_value=True), \
             patch("cqc_lem.app.run_scheduler.rebuild_leads_for_user.apply_async"):
            bodies = [
                api_client.get("/api/admin/automation-status", params={"session_token": _TOKEN}),
                api_client.post("/api/admin/automation-pause",
                                params={"session_token": _TOKEN, "hours": 1}),
                api_client.post("/api/leads/refresh", json={"session_token": _TOKEN}),
            ]
        for resp in bodies:
            assert resp.status_code == 200, resp.text
            self._assert_clean(resp.text)
        assert bodies[0].json()["detail"].keys() == {"paused", "pause_remaining_s", "breaker_remaining_s"}
        assert bodies[1].json()["detail"].keys() == {"paused", "seconds"}


# ---------------------------------------------------------------------------
# Mint: label validation
# ---------------------------------------------------------------------------

class TestOperatorTokenLabel:
    def _post(self, api_client: Any, body: dict[str, Any]) -> tuple[Any, Any]:
        with _session("full"), \
             patch(f"{_USER}.step_up_satisfied", return_value=True), \
             patch(f"{_USER}.create_session", return_value="minted") as create_session:
            resp = api_client.post("/api/user/operator-token", json={"session_token": _TOKEN, **body})
        return resp, create_session

    @pytest.mark.parametrize("label", [
        "owner@example.com", "line one\nline two", "tab\there", "<script>", "a" * 121,
        "trailing newline\n", "café", "ops (prod)",
    ])
    def test_a_bad_label_is_a_422_and_mints_nothing(self, api_client: Any, label: str) -> None:
        resp, create_session = self._post(api_client, {"label": label})
        assert resp.status_code == 422, label
        create_session.assert_not_called()

    @pytest.mark.parametrize("label", ["ops maintenance", "monitor.v2", "ops:prod-1", "a" * 120, ""])
    def test_a_plain_label_is_accepted(self, api_client: Any, label: str) -> None:
        resp, create_session = self._post(api_client, {"label": label})
        assert resp.status_code == 200
        assert create_session.call_args.kwargs["label"] == (label or "Maintenance operator")

    @pytest.mark.parametrize("ttl_days", [0, 366])
    def test_a_ttl_outside_1_to_365_is_a_422(self, api_client: Any, ttl_days: int) -> None:
        resp, create_session = self._post(api_client, {"ttl_days": ttl_days})
        assert resp.status_code == 422
        create_session.assert_not_called()

    def test_the_label_field_says_what_it_is_for(self, user_mod: Any) -> None:
        description = user_mod.OperatorTokenRequest.model_fields["label"].description or ""
        for phrase in ("Security card", "audit log", "personal data"):
            assert phrase in description, phrase

    @pytest.fixture
    def user_mod(self) -> Any:
        from cqc_lem.api.routers import user

        return user


# ---------------------------------------------------------------------------
# The operator can mint nothing, reach no credential, no cap and no approval mode
# ---------------------------------------------------------------------------

class TestOperatorCannotMintOrConfigure:
    @pytest.mark.parametrize("path", ["/api/user/operator-token", "/api/user/agent-token",
                                      "/api/user/observer-token", "/api/user/extension-token"])
    @pytest.mark.parametrize("kind", ["standard", "internal", "sandbox"])
    def test_no_mint_endpoint_is_reachable(self, api_client: Any, path: str, kind: str) -> None:
        with _session("operator"), _kind(kind), patch(f"{_USER}.create_session") as create_session:
            resp = api_client.post(path, json={"session_token": _TOKEN})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        create_session.assert_not_called()

    @pytest.mark.parametrize("kind", ["standard", "internal", "sandbox"])
    @pytest.mark.parametrize("body", [{"tone": "casual"}, {"max_comments_per_day": 999},
                                      {"connection_request_mode": "auto_approve"}])
    def test_caps_and_approval_modes_are_refused_on_every_account_kind(
            self, api_client: Any, kind: str, body: dict[str, Any]) -> None:
        with _session("operator"), _kind(kind), \
             patch(f"{_USER}.update_engagement_preferences") as upd:
            resp = api_client.put("/api/user/engagement-preferences",
                                  json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_configure"
        upd.assert_not_called()

    @pytest.mark.parametrize("verb,path,body", [
        ("post", "/api/user/linkedin-cookie", {"li_at": "a" * 40}),
        ("put", "/api/user/linkedin-password", {"linkedin_password": "x"}),
        ("post", "/api/user/passkeys/register/begin", {}),
        ("post", "/api/user/totp/enroll/begin", {}),
        ("post", "/api/user/recovery-codes/regenerate", {}),
        ("post", "/api/user/email/change/init", {"new_email": "new@example.com"}),
        ("post", "/api/user/sessions/revoke", {"all_others": True}),
    ])
    def test_credential_account_mover_and_revocation_paths_are_refused(
            self, api_client: Any, verb: str, path: str, body: dict[str, Any]) -> None:
        with _session("operator"), _kind("internal"), \
             patch(f"{_USER}.store_linkedin_li_at") as store, \
             patch(f"{_USER}.revoke_other_sessions") as revoke:
            resp = getattr(api_client, verb)(path, json={"session_token": _TOKEN, **body})
        assert resp.status_code == 403
        store.assert_not_called()
        revoke.assert_not_called()

    @pytest.mark.parametrize("path", ["/api/connection_request", "/api/schedule_dm",
                                      "/api/outreach/target"])
    def test_an_approved_create_on_a_standard_account_is_a_403(self, api_client: Any, path: str) -> None:
        body = {"session_token": _TOKEN, "status": "approved",
                "recipient_profile_url": "https://example.com/in/x", "target_profile_url":
                "https://example.com/in/x", "message": "hi", "scheduled_datetime": "2099-01-01T09:00:00"}
        with _session("operator"), _kind("standard"), \
             patch(f"{_M}.insert_connection_request") as c, \
             patch(f"{_M}.insert_scheduled_dm") as d, \
             patch("cqc_lem.api.routers.outreach.insert_outreach_target") as o:
            resp = api_client.post(path, json=body)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "agent_may_not_approve"
        for inserted in (c, d, o):
            inserted.assert_not_called()
