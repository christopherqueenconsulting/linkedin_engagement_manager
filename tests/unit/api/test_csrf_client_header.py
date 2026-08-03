"""CSRF — the custom-header layer (issue #957).

#950 retired the shared `/api` bearer token from the SPA bundle. It was worthless as ACCESS control
(every visitor held it) but real as a **CSRF** layer: a cross-site HTML form cannot set an
`Authorization` header even when the attacker knows the value, and setting one from `fetch()` needs
a preflight that has nothing to succeed against. Four mutating routes take query parameters and no
body, so the "a JSON body needs a preflight" layer never covered them either — which left
`SameSite=Lax` holding them alone.

`X-LEM-Client` puts the second layer back, and this module is the standing proof of its contract:

* a **cookie-authenticated write** without the header is refused 403, before the handler does
  anything — asserted per each of the four query-parameter routes;
* the same write **with** the header still works;
* a **bearer-authenticated** caller (scripts, Postman, an SPA bundle cached from before #950) is
  exempt, so the rollout breaks nobody;
* **reads** are untouched — CSRF is a forged write, and requiring a header on a GET would break the
  browser's own credentialed navigations;
* the header is **not a secret**: presence is the whole check, so no value is privileged and an
  empty one is no header at all.
"""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_UID = 77
_COOKIE = "browser-cookie"
_HEADER = {"X-LEM-Client": "spa"}
_POST_ID = 4242


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


@pytest.fixture
def cookie_session():
    """A live session on the httpOnly cookie and nothing else — the browser's credential.

    `get_session_user_id` is deliberately NOT patched: it is the thing under test."""
    with patch(f"{_M}._db_resolve_session",
               side_effect=lambda t: {"user_id": _UID, "scope": "full"} if t == _COOKIE else None), \
         patch(f"{_M}.user_owns_posts", return_value=True), \
         patch(f"{_M}.record_auth_event", return_value=True):
        yield


@pytest.fixture
def work():
    """Everything the four routes would set in motion. Each mock doubles as the assertion that a
    refused request never reached the handler at all."""
    with patch(f"{_M}.mark_queued") as mark_queued, \
         patch(f"{_M}.clear_generation_status"), \
         patch(f"{_M}.celery_chain") as chain, \
         patch(f"{_M}.plan_content_for_user"), \
         patch(f"{_M}.auto_create_weekly_content"), \
         patch(f"{_M}.automate_invites_to_company_page_for_user") as invites, \
         patch(f"{_M}.test_get_my_profile") as profile, \
         patch(f"{_M}.automate_reply_commenting") as replies:
        yield {
            "/api/create_weekly_content/": mark_queued,
            "/api/invite_to_li_company_page/": invites,
            "/api/aws_test_get_my_profile/": profile,
            "/api/automate_reply_commenting": replies,
            "chain": chain,
        }


# The four routes #914 converted to session auth that take query parameters and no body, so no
# preflight stands between a cross-site form and the handler.
_QUERY_PARAM_WRITES = [
    ("/api/create_weekly_content/", {}),
    ("/api/invite_to_li_company_page/", {}),
    ("/api/aws_test_get_my_profile/", {}),
    ("/api/automate_reply_commenting", {"post_id": _POST_ID}),
]


class TestTheFourQueryParameterWrites:
    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_a_cookie_authenticated_write_without_the_header_is_refused(
            self, client, cookie_session, work, path, params):
        resp = client.post(path, params=params, cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        # Refused BEFORE the handler queued anything — not merely refused on the way out. A forged
        # request that still spends the account's LLM budget is not a request that was refused.
        work[path].assert_not_called()
        work["chain"].assert_not_called()

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_the_same_write_from_the_spa_still_works(
            self, client, cookie_session, work, path, params):
        resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                           headers=_HEADER)

        assert resp.status_code == 200

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_a_bearer_authenticated_caller_needs_no_header(
            self, client, cookie_session, work, path, params):
        """Scripts, Postman and the admin tooling are not browsers, and an SPA bundle cached from
        before #950 still sends a bearer and no header — so the rollout breaks nobody."""
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"non-browser-token"}):
            resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                               headers={"Authorization": "Bearer non-browser-token"})

        assert resp.status_code == 200

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_a_guessed_bearer_does_not_buy_the_exemption(
            self, client, cookie_session, work, path, params):
        """Refused, and the queue never moves. Which layer says no is deployment-dependent and not
        the claim: today the credential gate 401s an unknown bearer at the edge, and once #950 lets
        a session credential past that gate it is this layer answering 403 one step further in."""
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"non-browser-token"}):
            resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                               headers={"Authorization": "Bearer guessed"})

        assert resp.status_code in (401, 403)
        work[path].assert_not_called()


class TestWhatTheLayerDoesNotTouch:
    def test_a_read_is_never_refused(self, client, cookie_session):
        """CSRF is a forged WRITE. With no CORS middleware the attacker cannot read the response, so
        a forged GET buys nothing — and requiring a header on reads would break the browser's own
        credentialed navigations (a plain `<a href>` download, an `<img>` src)."""
        with patch(f"{_M}.get_user_email", return_value="user@example.com"), \
             patch(f"{_M}.get_generation_status", return_value=None):
            resp = client.get("/api/content_generation_status/",
                              params={"session_token": "cookie"},
                              cookies={"lem_session": _COOKIE})

        assert resp.status_code == 200

    def test_an_explicit_token_write_needs_no_header(self, client, work):
        """The cookie is the only credential a browser attaches by itself. A caller who put a real
        token in the request knew it — it is httpOnly and a cross-site form cannot read it — so
        there is nothing to forge."""
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "full"} if t == "real" else None):
            resp = client.post("/api/aws_test_get_my_profile/", params={"session_token": "real"})

        assert resp.status_code == 200

    def test_an_anonymous_write_is_still_a_401(self, client, work):
        """The header is a CSRF layer, not authorisation. No session is still no session — and it
        must not become a 403, which would tell an unauthenticated caller they were merely missing
        a header."""
        with patch(f"{_M}._db_resolve_session", return_value=None):
            resp = client.post("/api/create_weekly_content/")

        assert resp.status_code == 401


class TestTheHeaderIsNotASecret:
    @pytest.mark.parametrize("value", ["spa", "extension", "anything-at-all"])
    def test_any_value_passes_because_presence_is_the_mechanism(
            self, client, cookie_session, work, value):
        """Comparing the value would buy nothing — the bundle is public, so an attacker knows it —
        and would invite the next reader to rotate it like a token. What a cross-origin form cannot
        do is set the header at all."""
        resp = client.post("/api/aws_test_get_my_profile/", cookies={"lem_session": _COOKIE},
                           headers={"X-LEM-Client": value})

        assert resp.status_code == 200

    def test_an_empty_value_is_no_header(self, client, cookie_session, work):
        resp = client.post("/api/aws_test_get_my_profile/", cookies={"lem_session": _COOKIE},
                           headers={"X-LEM-Client": ""})

        assert resp.status_code == 403
        work["/api/aws_test_get_my_profile/"].assert_not_called()


class TestOutsideARequest:
    def test_no_request_in_scope_is_a_no_op(self):
        """A Celery beat or a direct call resolves sessions with no HTTP request behind it. There is
        no cross-site forgery without a cross-site request, so this must not raise."""
        from cqc_lem.api.main import _require_client_header

        _require_client_header()

    def test_no_configured_bearer_means_no_exemption(self):
        """A deployment running the credential gate open must not also opt out of this layer."""
        from cqc_lem.api.main import _bearer_authenticated

        with patch(f"{_M}._API_ACCESS_TOKEN_SET", set()):
            assert _bearer_authenticated(_FakeRequest("Bearer anything")) is False
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"tok"}):
            assert _bearer_authenticated(_FakeRequest("Bearer tok")) is True
            assert _bearer_authenticated(_FakeRequest(None)) is False
            assert _bearer_authenticated(None) is False


class _FakeRequest:
    def __init__(self, authorization):
        self.headers = {"Authorization": authorization} if authorization else {}
