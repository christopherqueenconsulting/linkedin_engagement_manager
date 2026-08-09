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
* **every** state-changing method is covered, not just POST — a route added as a `PUT`/`PATCH`/
  `DELETE` inherits the layer;
* the refusal comes **before** the scope check, so a forged request reaches no audit row;
* a **bearer-authenticated** caller (scripts, Postman, an SPA bundle cached from before #950) is
  exempt, so the rollout breaks nobody;
* **reads** are untouched — CSRF is a forged write, and requiring a header on a GET would break the
  browser's own credentialed navigations;
* the header is **not a secret**: presence is the whole check, so no value is privileged, case does
  not matter, and an empty one is no header at all;
* the two structural assumptions the layer rests on hold — **no CORS middleware** is installed, and
  the session cookie is read in exactly **one** place.
"""

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
from starlette.datastructures import Headers

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
# The avatar handlers moved to their own router (#1154), so the db functions they call are
# read from THAT module's globals now. `get_session_user_id` still patches on `_M`: the
# handlers reach it as an attribute of the host module at request time.
_AV = "cqc_lem.api.routers.avatar"
_UID = 77
_COOKIE = "browser-cookie"
_HEADER = {"X-LEM-Client": "spa"}
_POST_ID = 4242


class _FakeRequest:
    """Enough of a Starlette `Request` for the header helpers, with Starlette's own case-insensitive
    `Headers` — a plain dict would let a case bug pass unnoticed.
    """

    def __init__(self, authorization: Optional[str] = None, method: str = "POST",
                 client_header: Optional[str] = None) -> None:
        raw: Dict[str, str] = {}
        if authorization:
            raw["Authorization"] = authorization
        if client_header is not None:
            raw["X-LEM-Client"] = client_header
        self.headers = Headers(raw)
        self.method = method


@pytest.fixture(scope="module")
def client() -> Iterator[Any]:
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.engagement.invites.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.engagement.posting.automate_reply_commenting"),
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
def cookie_session() -> Iterator[None]:
    """A live session on the httpOnly cookie and nothing else — the browser's credential.

    `get_session_user_id` is deliberately NOT patched: it is the thing under test.
    """
    with patch(f"{_M}._db_resolve_session",
               side_effect=lambda t: {"user_id": _UID, "scope": "full"} if t == _COOKIE else None), \
         patch(f"{_M}.user_owns_posts", return_value=True), \
         patch(f"{_M}.record_auth_event", return_value=True), \
         patch(f"{_USER}.record_auth_event", return_value=True):
        yield


@pytest.fixture
def work() -> Iterator[Dict[str, MagicMock]]:
    """Everything the four routes would set in motion. Each mock doubles as the assertion that a
    refused request never reached the handler at all.
    """
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
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        resp = client.post(path, params=params, cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        # Refused BEFORE the handler queued anything — not merely refused on the way out. A forged
        # request that still spends the account's LLM budget is not a request that was refused.
        work[path].assert_not_called()
        work["chain"].assert_not_called()

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_the_same_write_from_the_spa_still_works(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                           headers=_HEADER)

        assert resp.status_code == 200

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_a_bearer_authenticated_caller_needs_no_header(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        """Scripts, Postman and the admin tooling are not browsers, and an SPA bundle cached from
        before #950 still sends a bearer and no header — so the rollout breaks nobody.
        """
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"non-browser-token"}):
            resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                               headers={"Authorization": "Bearer non-browser-token"})

        assert resp.status_code == 200


class TestAGuessedBearerBuysNothing:
    """Split by which layer is doing the refusing, so neither test can pass through a regression in
    the other. Which one answers depends on what else the request carried: since #950 the credential
    gate takes a bearer OR a session credential, so a forged request that brings the victim's cookie
    is past the edge by definition — which is exactly why this layer has to stand behind it.
    """

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_the_credential_gate_401s_it_at_the_edge_with_no_session_credential(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"non-browser-token"}):
            resp = client.post(path, params=params,
                               headers={"Authorization": "Bearer guessed"})

        assert resp.status_code == 401
        work[path].assert_not_called()

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_this_layer_403s_it_when_the_cookie_carried_it_past_the_edge(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        """The CSRF-relevant shape, and the one #950 changed: the gate accepts the session
        credential the browser attached by itself, so a guessed bearer is never what the edge is
        judging. The exemption is keyed on a bearer that MATCHES, so it stays shut.
        """
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"non-browser-token"}):
            resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                               headers={"Authorization": "Bearer guessed"})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        work[path].assert_not_called()

    @pytest.mark.parametrize("path,params", _QUERY_PARAM_WRITES)
    def test_this_layer_403s_it_when_no_credential_gate_is_configured(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock],
            path: str, params: Dict[str, Any]) -> None:
        """The fail-closed half, end to end: an unconfigured gate lets any `Authorization` header
        past the edge, and the exemption must NOT open for it — a deployment running the credential
        gate open must not also opt out of the CSRF layer.
        """
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", set()):
            resp = client.post(path, params=params, cookies={"lem_session": _COOKIE},
                               headers={"Authorization": "Bearer guessed"})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        work[path].assert_not_called()


class TestEveryStateChangingMethod:
    """POST is what the four routes use, but the gate is keyed on the METHOD SET. Asserting the set
    directly is what stops a refactor down to `{"POST"}` from passing green — a `PUT`/`PATCH`/
    `DELETE` route added later inherits the layer or it does not, and this is where that is decided.
    """

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_an_unsafe_method_without_the_header_is_refused(self, method: str) -> None:
        from cqc_lem.api.main import HTTPException, _request_object, _require_client_header

        token = _request_object.set(_FakeRequest(method=method))
        try:
            with patch(f"{_M}._API_ACCESS_TOKEN_SET", set()), pytest.raises(HTTPException) as exc:
                _require_client_header()
        finally:
            _request_object.reset(token)

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "client_header_required"

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_a_safe_method_passes_straight_through(self, method: str) -> None:
        from cqc_lem.api.main import _request_object, _require_client_header

        token = _request_object.set(_FakeRequest(method=method))
        try:
            _require_client_header()  # must not raise
        finally:
            _request_object.reset(token)

    def test_a_real_put_route_is_covered_too(self, client: Any, cookie_session: None) -> None:
        """The unit assertion above proves the set; this proves the set is actually reached on a
        method other than POST, through the real routing + resolver path.
        """
        with patch(f"{_M}.get_scheduled_dm_user_id") as owner, \
             patch(f"{_M}.update_scheduled_dm") as update:
            resp = client.put("/api/dm", json={"dm_id": 1, "action": "cancel",
                                               "session_token": "cookie"},
                              cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        owner.assert_not_called()
        update.assert_not_called()

    def test_a_real_delete_route_is_covered_too(self, client: Any, cookie_session: None) -> None:
        with patch(f"{_M}.get_scheduled_dm_user_id") as owner, \
             patch(f"{_M}.update_scheduled_dm_status") as cancel:
            resp = client.request("DELETE", "/api/dm",
                                  json={"dm_id": 1, "session_token": "cookie"},
                                  cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        owner.assert_not_called()
        cancel.assert_not_called()


class TestTheMultipartWrites:
    """Query parameters are not the only shape the JSON-body layer misses. A cross-origin caller can
    produce `multipart/form-data` with no preflight — a plain `<form enctype=…>`, or a `no-cors`
    `fetch` with a `FormData` body — and two mutating routes take exactly that. They are covered
    only because the layer was scoped to EVERY state-changing cookie-authenticated request rather
    than to the four routes that made the gap visible, so this is where that scoping is proven.
    """

    def test_an_avatar_training_upload_without_the_header_is_refused(
            self, client: Any, cookie_session: None) -> None:
        """The most expensive of the two: it spends an avatar credit and starts a training run."""
        with patch(f"{_AV}.get_avatar_credit_balance") as balance:
            resp = client.post("/api/avatar/training",
                               data={"session_token": "cookie", "trigger_word": "TOK"},
                               files={"photos": ("p.zip", b"not-a-zip", "application/zip")},
                               cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        balance.assert_not_called()

    def test_a_newsletter_cover_upload_without_the_header_is_refused(
            self, client: Any, cookie_session: None) -> None:
        with patch(f"{_USER}.get_newsletter_edition") as edition:
            resp = client.post("/api/user/newsletter-draft/cover",
                               data={"session_token": "cookie", "edition_id": 1},
                               files={"file": ("c.png", b"not-a-png", "image/png")},
                               cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        edition.assert_not_called()


class TestItRunsBeforeAnythingElse:
    def test_it_refuses_ahead_of_the_scope_check(self, client: Any, work: Dict[str, MagicMock]) -> None:
        """A scoped-away session that ALSO has no client header must be refused as a forgery, not as
        a scope violation. The scope refusal writes an audited `session_scope_denied` row, so if the
        order flipped a cross-site forgery would leave a trail attributed to the victim — and would
        have reached a code path that writes. This assertion is the ordering.
        """
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: ({"user_id": _UID, "scope": "extension"}
                                          if t == _COOKIE else None)), \
             patch(f"{_M}.record_auth_event") as audit:
            resp = client.post("/api/create_weekly_content/", cookies={"lem_session": _COOKIE})

        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "client_header_required"
        audit.assert_not_called()


class TestWhatTheLayerDoesNotTouch:
    def test_a_read_is_never_refused(self, client: Any, cookie_session: None) -> None:
        """CSRF is a forged WRITE. With no CORS middleware the attacker cannot read the response, so
        a forged GET buys nothing — and requiring a header on reads would break the browser's own
        credentialed navigations (a plain `<a href>` download, an `<img>` src).
        """
        with patch(f"{_M}.get_user_email", return_value="user@example.com"), \
             patch(f"{_M}.get_generation_status", return_value=None):
            resp = client.get("/api/content_generation_status/",
                              params={"session_token": "cookie"},
                              cookies={"lem_session": _COOKIE})

        assert resp.status_code == 200

    def test_an_explicit_token_write_needs_no_header(self, client: Any,
                                                     work: Dict[str, MagicMock]) -> None:
        """The cookie is the only credential a browser attaches by itself. A caller who put a real
        token in the request knew it — it is httpOnly and a cross-site form cannot read it — so
        there is nothing to forge.
        """
        with patch(f"{_M}._db_resolve_session",
                   side_effect=lambda t: {"user_id": _UID, "scope": "full"} if t == "real" else None):
            resp = client.post("/api/aws_test_get_my_profile/", params={"session_token": "real"})

        assert resp.status_code == 200

    def test_an_anonymous_write_is_still_a_401(self, client: Any, work: Dict[str, MagicMock]) -> None:
        """The header is a CSRF layer, not authorisation. No session is still no session — and it
        must not become a 403, which would tell an unauthenticated caller they were merely missing
        a header.
        """
        with patch(f"{_M}._db_resolve_session", return_value=None):
            resp = client.post("/api/create_weekly_content/")

        assert resp.status_code == 401


class TestTheHeaderIsNotASecret:
    @pytest.mark.parametrize("value", ["spa", "extension", "anything-at-all"])
    def test_any_value_passes_because_presence_is_the_mechanism(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock], value: str) -> None:
        """Comparing the value would buy nothing — the bundle is public, so an attacker knows it —
        and would invite the next reader to rotate it like a token. What a cross-origin form cannot
        do is set the header at all.
        """
        resp = client.post("/api/aws_test_get_my_profile/", cookies={"lem_session": _COOKIE},
                           headers={"X-LEM-Client": value})

        assert resp.status_code == 200

    @pytest.mark.parametrize("name", ["x-lem-client", "X-Lem-Client", "X-LEM-CLIENT"])
    def test_the_name_is_case_insensitive_like_every_other_header(
            self, client: Any, cookie_session: None, work: Dict[str, MagicMock], name: str) -> None:
        """HTTP header names are case-insensitive and an intermediary may re-case them. Reading it
        off Starlette's `Headers` gets this for free — this is the assertion that keeps it.
        """
        resp = client.post("/api/aws_test_get_my_profile/", cookies={"lem_session": _COOKIE},
                           headers={name: "spa"})

        assert resp.status_code == 200

    def test_an_empty_value_is_no_header(self, client: Any, cookie_session: None,
                                         work: Dict[str, MagicMock]) -> None:
        resp = client.post("/api/aws_test_get_my_profile/", cookies={"lem_session": _COOKIE},
                           headers={"X-LEM-Client": ""})

        assert resp.status_code == 403
        work["/api/aws_test_get_my_profile/"].assert_not_called()


class TestOutsideARequest:
    def test_no_request_in_scope_is_a_no_op(self) -> None:
        """A Celery beat or a direct call resolves sessions with no HTTP request behind it. There is
        no cross-site forgery without a cross-site request, so this must not raise.
        """
        from cqc_lem.api.main import _require_client_header

        _require_client_header()

    def test_no_configured_bearer_means_no_exemption(self) -> None:
        """A deployment running the credential gate open must not also opt out of this layer."""
        from cqc_lem.api.main import _bearer_authenticated

        with patch(f"{_M}._API_ACCESS_TOKEN_SET", set()):
            assert _bearer_authenticated(_FakeRequest("Bearer anything")) is False
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"tok"}):
            assert _bearer_authenticated(_FakeRequest("Bearer tok")) is True
            assert _bearer_authenticated(_FakeRequest(None)) is False
            assert _bearer_authenticated(None) is False


class TestTheAssumptionsTheLayerRestsOn:
    """Two claims in the design comment that a later change could silently break. Both are cheap to
    make executable, and neither is a claim a reader can re-verify by eye at review time.
    """

    def test_no_cors_middleware_is_installed(self) -> None:
        """`X-LEM-Client` works because a cross-origin request cannot set it and the preflight it
        would need has nothing to answer it. CORS with credentials would let a real cross-origin
        caller ASK for permission to send this header, reinstating the hole the layer closes — and
        it would arrive as a one-line `app.add_middleware(...)` in some unrelated PR.
        """
        from starlette.middleware.cors import CORSMiddleware

        from cqc_lem.api.main import app

        installed = [m.cls for m in app.user_middleware]
        assert CORSMiddleware not in installed, (
            "CORS with credentials would undo the X-LEM-Client CSRF layer — see "
            "docs/identity-and-sessions.md § CSRF before adding it."
        )

    def test_the_request_and_cookie_contextvars_are_set_together(self) -> None:
        """`_require_client_header` no-ops when no request is in scope, which is only safe because a
        live HTTP request can never carry the session cookie WITHOUT the request: one middleware
        sets both, in the same block. Splitting them would turn the no-op into a silent bypass.
        """
        from cqc_lem.api import main

        body = inspect.getsource(main.session_cookie_middleware)

        assert "_request_session_cookie.set(" in body
        assert "_request_object.set(request)" in body

    def test_no_handler_reads_the_session_cookie_for_itself(self) -> None:
        """The ONE-resolver claim, made executable. A handler that read the cookie on its own path
        would authenticate a caller without ever passing this check — and `/auth/logout` and
        `/user/sessions/revoke` are exactly the shortcuts where that is tempting.

        Named functions, not a count: since #950 the edge gate reads it too
        (`_has_session_credential`), and that read authenticates nobody — it asks only whether the
        caller brought SOMETHING for the resolver to judge, before routing, with no database. Both
        readers run ahead of every handler, which is the property that matters.
        """
        from cqc_lem.api import main

        tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))

        readers = set()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Attribute) and node.attr == "get"
                        and isinstance(node.value, ast.Attribute) and node.value.attr == "cookies"):
                    readers.add(func.name)

        assert readers == {"session_cookie_middleware", "_has_session_credential"}, (
            "The session cookie must be read only by session_cookie_middleware (which stamps the "
            "ContextVar) and the edge gate's presence check; every consumer reads the ContextVar so "
            "the CSRF and scope checks cannot be bypassed."
        )
