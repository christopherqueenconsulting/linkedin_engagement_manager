"""Suggest-only accounts connect by OAuth only: no LinkedIn password, cookie or PIN is stored (#2368).

Every write path is driven for a suggest account, a trial account whose stored mode is `automate`,
and three unreadable readings of the mode (fail closed), and once for a paid `automate` account to
prove it is unchanged. `TestEveryCredentialWriteIsGated` reads the API package and fails when a new
route reaches a credential setter without the refusal.
"""

import ast
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_PIN = "cqc_lem.utilities.linkedin.verification_pin"
_STATE = "cqc_lem.utilities.engagement_mode.get_user_engagement_state"
_UID = 42
_TOKEN = "tok_test"
_LI_AT = "AQEDAReallyLongLinkedInSessionTokenValue1234567890"
_REFUSAL = "suggest_mode_oauth_only"


def _state(mode: str = "automate", status: str = "active", tier: str = "professional") -> dict:
    return {"engagement_mode": mode, "subscription_status": status, "subscription_tier": tier}


#: Readings that must all refuse: suggest, a trial on `automate`, and three unreadable answers.
REFUSING_READINGS = [
    pytest.param({"return_value": _state("suggest")}, id="suggest"),
    pytest.param({"return_value": _state("automate", status="trial")}, id="trial-status"),
    pytest.param({"return_value": _state("automate", tier="free_trial")}, id="trial-tier"),
    pytest.param({"return_value": None}, id="missing-row"),
    pytest.param({"return_value": {"engagement_mode": "automate"}}, id="no-subscription-half"),
    pytest.param({"side_effect": TypeError("DB_PORT unset")}, id="read-fault"),
]


def _post_credentials(api_client: Any, method: str, path: str, body: dict) -> Any:
    return getattr(api_client, method)(path, json={"session_token": _TOKEN, **body})


CREDENTIAL_ROUTES = [
    pytest.param("put", "/api/user/linkedin-password", {"linkedin_password": "hunter2-not-real"},
                 id="password"),
    pytest.param("post", "/api/user/linkedin-cookie", {"li_at": _LI_AT}, id="cookie"),
    pytest.param("post", "/api/user/linkedin-cookie", {"li_at": _LI_AT, "drop_password": True},
                 id="cookie-drop-password"),
    pytest.param("post", "/api/user/extension-token", {}, id="extension-token"),
]


class TestApiRefusesCredentialWrites:
    @pytest.mark.parametrize("reading", REFUSING_READINGS)
    @pytest.mark.parametrize("method,path,body", CREDENTIAL_ROUTES)
    def test_refused_and_nothing_stored(self, api_client, method, path, body, reading):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(_STATE, **reading), \
             patch(f"{_USER}.step_up_satisfied", return_value=True) as step_up, \
             patch(f"{_USER}.update_user_linkedin_password") as store_password, \
             patch(f"{_USER}.store_linkedin_li_at") as store_cookie, \
             patch(f"{_USER}.clear_user_linkedin_password") as clear_password, \
             patch(f"{_USER}.create_session") as create_session:
            resp = _post_credentials(api_client, method, path, body)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == _REFUSAL
        store_password.assert_not_called()
        store_cookie.assert_not_called()
        clear_password.assert_not_called()
        create_session.assert_not_called()
        # Refused BEFORE step-up: a suggest account is never asked for a ceremony it cannot use.
        step_up.assert_not_called()

    def test_automate_account_password_unchanged(self, api_client, signed_in):
        with patch(f"{_USER}.update_user_linkedin_password", return_value=True) as store:
            resp = _post_credentials(api_client, "put", "/api/user/linkedin-password",
                                     {"linkedin_password": "hunter2-not-real"})
        assert resp.status_code == 200
        store.assert_called_once_with(signed_in, "hunter2-not-real")

    def test_automate_account_cookie_unchanged(self, api_client, signed_in):
        with patch(f"{_USER}.store_linkedin_li_at", return_value=True) as store:
            resp = _post_credentials(api_client, "post", "/api/user/linkedin-cookie",
                                     {"li_at": _LI_AT})
        assert resp.status_code == 200
        assert store.call_args.args[:2] == (signed_in, _LI_AT)

    def test_automate_account_extension_token_unchanged(self, api_client, signed_in):
        with patch(f"{_USER}.create_session", return_value="ext-token") as create_session:
            resp = _post_credentials(api_client, "post", "/api/user/extension-token", {})
        assert resp.status_code == 200
        create_session.assert_called_once()

    def test_refusal_logs_info_never_warning(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(_STATE, return_value=_state("suggest")), \
             patch(f"{_USER}.store_linkedin_li_at"), \
             patch(f"{_USER}.log_info") as info, \
             patch(f"{_USER}.log_warning") as warning:
            _post_credentials(api_client, "post", "/api/user/linkedin-cookie", {"li_at": _LI_AT})
        assert any("suggest-only" in c.args[0] for c in info.call_args_list)
        warning.assert_not_called()


class _FakeRedis:
    """Just enough of Redis for the PIN exchange: get/set over a dict."""

    def __init__(self, data: Optional[dict] = None) -> None:
        self.data = dict(data or {})

    def get(self, key: str) -> Optional[bytes]:
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        self.data[key] = value
        return True


@pytest.fixture
def pin_redis():
    client = _FakeRedis({"linkedin:pin_token:abc123XYZ": str(_UID)})
    with patch(f"{_PIN}._redis_client", return_value=client):
        yield client


class TestVerificationPinRefused:
    @pytest.mark.parametrize("reading", REFUSING_READINGS)
    def test_submit_by_token_stores_nothing(self, pin_redis, reading):
        from cqc_lem.utilities.linkedin.verification_pin import submit_pin_by_token
        with patch(_STATE, **reading):
            assert submit_pin_by_token("abc123XYZ", "483920") is None
        assert f"linkedin:pin:{_UID}" not in pin_redis.data

    @pytest.mark.parametrize("reading", REFUSING_READINGS)
    def test_submit_for_known_user_stores_nothing(self, pin_redis, reading):
        from cqc_lem.utilities.linkedin.verification_pin import submit_pin
        with patch(_STATE, **reading):
            assert submit_pin(_UID, "483920") is False
        assert f"linkedin:pin:{_UID}" not in pin_redis.data

    def test_inbound_webhook_ignores_a_suggest_accounts_pin(self, api_client, pin_redis):
        """The SendGrid webhook stays 200 (a non-2xx makes SendGrid retry) and stores nothing."""
        with patch(_STATE, return_value=_state("suggest")), \
             patch(f"{_M}._log_inbound_verdict"):
            resp = api_client.post("/api/linkedin/verification-pin/inbound", data={
                "to": "pin+abc123XYZ@parse.example.com", "text": "483920"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "ignored"
        assert f"linkedin:pin:{_UID}" not in pin_redis.data

    def test_automate_account_pin_unchanged(self, api_client, pin_redis):
        with patch(f"{_M}._log_inbound_verdict"):
            resp = api_client.post("/api/linkedin/verification-pin/inbound", data={
                "to": "pin+abc123XYZ@parse.example.com", "text": "483920"})
        assert resp.json()["detail"] == "accepted"
        assert pin_redis.data[f"linkedin:pin:{_UID}"] == "483920"


class TestAccountReadiness:
    def _get(self, api_client: Any, reading: dict) -> dict:
        with patch(f"{_M}.get_session_user_id", return_value=_UID), \
             patch(_STATE, **reading), \
             patch(f"{_USER}.get_user_linkedin_display_name", return_value="Jordan Alvarez"), \
             patch(f"{_USER}.get_user_token_info", return_value={"access_token": "tok"}), \
             patch(f"{_USER}.has_linkedin_session", return_value=False), \
             patch(f"{_USER}.has_linkedin_password", return_value=True), \
             patch(f"{_USER}.get_user_subscription_info",
                   return_value={"subscription_status": "trial"}), \
             patch(f"{_USER}.get_user_geo", return_value=None):
            return api_client.get(f"/api/user/account-readiness?session_token={_TOKEN}").json()[
                "detail"]

    def test_suggest_account_is_not_asked_for_a_session(self, api_client):
        detail = self._get(api_client, {"return_value": _state("suggest")})
        assert "linkedin_session" not in {i["key"] for i in detail["items"]}
        assert detail["cookie_migration_needed"] is False
        assert detail["ready"] is True

    def test_automate_account_still_needs_a_session(self, api_client):
        detail = self._get(api_client, {"return_value": _state("automate")})
        assert "linkedin_session" in {i["key"] for i in detail["items"]}
        assert detail["cookie_migration_needed"] is True


class TestConnectSessionEmail:
    _MOD = "cqc_lem.utilities.notifications"

    def test_suggest_account_never_emailed_to_connect_a_cookie(self):
        from cqc_lem.utilities.notifications import notify_linkedin_session
        with patch(_STATE, return_value=_state("suggest")), \
             patch(f"{self._MOD}.get_linkedin_session_email_sent_at", return_value=None), \
             patch(f"{self._MOD}.get_user_email", return_value="u@example.com"), \
             patch(f"{self._MOD}.send_connect_linkedin_email") as send:
            assert notify_linkedin_session(_UID) is False
        send.assert_not_called()


class TestOAuthConnectUnaffected:
    def test_oauth_callback_never_consults_the_credential_gate(self):
        """The `w_member_social` connect flow is how a suggest account connects, so it is not gated."""
        from cqc_lem.api.routers import auth
        source = Path(auth.__file__).read_text()
        assert "credential_collection_allowed" not in source
        assert "_refuse_credential_write_for_suggest_mode" not in source


#: Every name that stores a LinkedIn password, session cookie or verification PIN.
_CREDENTIAL_SETTERS = frozenset({
    "update_user_linkedin_password", "store_linkedin_li_at", "store_cookies", "_store_cookie_rows",
    "submit_pin", "submit_pin_by_token",
})
#: Route functions allowed to reach a setter, and how each is gated.
_GATED_BY_ROUTE_REFUSAL = {"update_linkedin_password", "store_linkedin_cookie_endpoint"}
#: Gated inside `verification_pin._store_pin` instead (the webhook must always answer 200).
_GATED_IN_PIN_STORE = {"_handle_inbound_parse"}


def _functions_calling(names: frozenset, root: Path) -> dict:
    found: dict = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
                      for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if called & names:
                found[fn.name] = called
    return found


class TestEveryCredentialWriteIsGated:
    def test_every_api_caller_of_a_credential_setter_is_known_and_gated(self):
        import cqc_lem.api as api_pkg
        callers = _functions_calling(_CREDENTIAL_SETTERS, Path(api_pkg.__file__).parent)
        # Non-vacuous: the known routes must be found, and nothing else may be.
        assert set(callers) == _GATED_BY_ROUTE_REFUSAL | _GATED_IN_PIN_STORE
        for name in _GATED_BY_ROUTE_REFUSAL:
            assert "_refuse_credential_write_for_suggest_mode" in callers[name], name

    def test_the_pin_store_is_gated(self):
        from cqc_lem.utilities.linkedin import verification_pin
        tree = ast.parse(Path(verification_pin.__file__).read_text())
        store = next(f for f in ast.walk(tree)
                     if isinstance(f, ast.FunctionDef) and f.name == "_store_pin")
        called = {n.func.id for n in ast.walk(store)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "credential_collection_allowed" in called


def test_admin_router_writes_no_linkedin_credential():
    """Decision (#2368): no admin route stores a LinkedIn credential on a user's behalf. One that
    is added must refuse a suggest-only TARGET account the same way; this test names it.
    """
    from cqc_lem.api.routers import admin
    admin_file = Path(admin.__file__)
    assert _functions_calling(_CREDENTIAL_SETTERS, admin_file.parent).keys() <= (
        _GATED_BY_ROUTE_REFUSAL | _GATED_IN_PIN_STORE)
    tree = ast.parse(admin_file.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & _CREDENTIAL_SETTERS
