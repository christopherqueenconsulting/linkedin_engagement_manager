"""Unit tests for the /api credential gate in cqc_lem.api.main.

Since issue #950 the gate takes EITHER credential: the non-browser bearer token (scripts, Postman,
admin tooling) or a session credential the route itself judges. The SPA no longer ships a bearer —
it was a build-time constant in a public bundle — so a cookie-authenticated browser has to clear
this middleware on its cookie alone, and a caller with neither credential still gets 401.
"""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME

pytestmark = pytest.mark.unit


# Import cqc_lem.api.main lazily (inside fixtures), not at module scope: importing
# it builds the OpenAI client, which needs OPENAI_API_KEY — set by the session
# autouse fixture in tests/conftest.py, which runs *after* collection.
@pytest.fixture(scope="module")
def main_mod():
    from cqc_lem.api import main
    return main


@pytest.fixture(scope="module")
def client():
    with patch("cqc_lem.utilities.observability.track_api_call"):
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc


# ---------------------------------------------------------------------------
# _bearer_token — Authorization header parsing
# ---------------------------------------------------------------------------

class TestBearerTokenParsing:
    @pytest.mark.parametrize("header,expected", [
        (None, None),
        ("", None),
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),  # scheme is case-insensitive
        ("Bearer   ", None),           # empty token
        ("Basic abc123", None),        # wrong scheme
        ("abc123", None),              # no scheme
    ])
    def test_parses_header(self, main_mod, header, expected):
        assert main_mod._bearer_token(header) == expected


# ---------------------------------------------------------------------------
# _api_token_required — which paths are gated
# ---------------------------------------------------------------------------

class TestApiTokenRequired:
    def test_disabled_when_no_tokens_configured(self, main_mod):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", set()):
            assert main_mod._api_token_required("/api/posts") is False

    @pytest.mark.parametrize("path", [
        "/api/posts",
        "/api/avatar/training",
    ])
    def test_business_routes_gated(self, main_mod, path):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"tok"}):
            assert main_mod._api_token_required(path) is True

    @pytest.mark.parametrize("path", [
        "/api/auth/email/init",   # login flow
        "/api/auth/email/verify",
        "/api/auth/session",
        "/api/billing/webhook",   # Stripe (signature-verified)
        "/api/assets",            # public: LinkedIn fetches media over unauth URL
        "/api/app-info",          # public: SPA footer version/toggle
        "/health",                # non-/api
        "/auth/linkedin/callback",
        "/assets/index.js",       # SPA static
    ])
    def test_public_routes_not_gated(self, main_mod, path):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"tok"}):
            assert main_mod._api_token_required(path) is False

    @pytest.mark.parametrize("path", [
        "/api/faq-admin",              # sibling of the public /api/faq
        "/api/app-info-internal",
        "/api/assets-private",
        "/api/billing/webhook-replay",
        "/api/user/linkedin-cookie-export",
    ])
    def test_a_public_entry_does_not_unlock_its_siblings(self, main_mod, path):
        """Public entries match on a path-segment boundary, not a bare string prefix."""
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"tok"}):
            assert main_mod._api_token_required(path) is True

    @pytest.mark.parametrize("path", [
        "/api/auth/email/init",                          # trailing-slash entry: whole subtree
        "/api/extension/linkedin-connect.zip",
        "/api/linkedin/verification-pin/inbound",        # leaf entry: segments below it stay public
        "/api/linkedin/comment-notification/inbound",
    ])
    def test_public_subpaths_stay_public(self, main_mod, path):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"tok"}):
            assert main_mod._api_token_required(path) is False


# ---------------------------------------------------------------------------
# Middleware behavior via TestClient
# ---------------------------------------------------------------------------

class TestGateMiddleware:
    TOKEN = "secret-token-xyz"

    # A gated, non-existent /api route exercises the gate without invoking a real
    # handler (/api/assets is public by design, so it can't test the gate).
    GATED_PROBE = "/api/__gated_probe__"

    def test_guarded_route_without_any_credential_is_401(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE)
        assert resp.status_code == 401

    def test_guarded_route_wrong_token_and_no_session_is_401(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_guarded_route_valid_token_passes_gate(self, main_mod, client):
        # Valid token clears the gate; routing then 404s on the unknown path.
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert resp.status_code != 401

    def test_session_cookie_passes_gate_without_a_bearer(self, main_mod, client):
        """The whole point of #950: the SPA holds no bearer, so its cookie has to clear the gate."""
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE,
                              headers={"Cookie": f"{SESSION_COOKIE_NAME}=whatever"})
        assert resp.status_code != 401

    def test_session_header_passes_gate_without_a_bearer(self, main_mod, client):
        # The cookie-less fallback (plain-http origin) and the tutorial capture harness.
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers={"X-Session-Token": "real-token"})
        assert resp.status_code != 401

    @pytest.mark.parametrize("headers", [
        {"Cookie": f"{SESSION_COOKIE_NAME}="},        # present but empty
        # Whitespace only — the quoted form is the one that survives cookie parsing as "  ",
        # so the cookie has to be stripped like the header is, not just tested for presence.
        {"Cookie": f'{SESSION_COOKIE_NAME}="  "'},
        {"X-Session-Token": "   "},                   # whitespace only
        {"Cookie": "some_other_cookie=1"},            # a different cookie is not a credential
    ])
    def test_empty_or_unrelated_session_credential_is_401(self, main_mod, client, headers):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers=headers)
        assert resp.status_code == 401

    def test_gate_disabled_allows_unauthenticated(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", set()):
            resp = client.get("/api/assets", params={"file_name": "x.png"})
        assert resp.status_code != 401


class TestQueryParamMutatingRoutesStillRefuseAnonymous:
    """Issue #950 regression: the gate got LOOSER (a cookie now clears it), so the four
    query-parameter mutating routes #914 converted are re-checked here end to end.

    Anonymous — no bearer, no session — is refused at the middleware. A caller who merely SETS a
    cookie clears the middleware and is then refused by `require_session_user_id()` in the handler,
    which is where authorisation actually lives. Either way: 401, and the task behind it never runs.
    """

    TOKEN = "secret-token-xyz"
    ROUTES = [
        ("/api/create_weekly_content/", {"user_id": 1}),
        ("/api/invite_to_li_company_page/", {"user_id": 1}),
        ("/api/aws_test_get_my_profile/", {"user_id": 1}),
        ("/api/automate_reply_commenting", {"post_id": 1}),
    ]

    @pytest.mark.parametrize("path,params", ROUTES)
    def test_no_credential_is_401(self, main_mod, client, path, params):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.post(path, params=params)
        assert resp.status_code == 401

    @pytest.mark.parametrize("path,params", ROUTES)
    def test_unresolvable_session_cookie_is_401(self, main_mod, client, path, params):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}), \
             patch.object(main_mod, "get_session_user_id", return_value=None):
            resp = client.post(path, params=params,
                               headers={"Cookie": f"{SESSION_COOKIE_NAME}=forged"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("path,params", ROUTES)
    def test_bearer_alone_is_401(self, main_mod, client, path, params):
        """A bearer holder is not a user. It clears the edge filter and nothing else."""
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}), \
             patch.object(main_mod, "get_session_user_id", return_value=None):
            resp = client.post(path, params=params,
                               headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert resp.status_code == 401


class TestSessionCookieAttributes:
    """The cookie is now the browser's ONLY credential (issue #950), so its attributes are the
    whole of the browser-side defence and belong under test rather than under review.

    `SameSite` is the one that replaced a layer: a cross-site form POST could never set an
    `Authorization` header, so the retired bearer was incidentally blocking form-based CSRF on the
    four query-parameter mutating routes #914 converted. `Lax` covers that on its own — the cookie
    is simply not attached to a cross-site POST — and #957 restores the explicit layer.
    """

    class _Recorder:
        """Captures what `_set_session_cookie` asks for, without a live response."""

        def __init__(self) -> None:
            self.kwargs = {}

        def set_cookie(self, **kwargs) -> None:
            self.kwargs = kwargs

    def _issued(self, main_mod, samesite: str = "lax", secure: bool = True):
        response = self._Recorder()
        with patch.object(main_mod, "SESSION_COOKIE_SAMESITE", samesite), \
             patch.object(main_mod, "SESSION_COOKIE_SECURE", secure):
            main_mod._set_session_cookie(response, "tok")
        return response.kwargs

    def test_cookie_is_httponly_secure_and_samesite_lax(self, main_mod):
        kwargs = self._issued(main_mod)
        assert kwargs["key"] == SESSION_COOKIE_NAME
        assert kwargs["httponly"] is True   # no XSS-readable session token
        assert kwargs["secure"] is True     # prod default
        assert kwargs["samesite"] == "lax"  # the CSRF layer the bearer's retirement leans on
        assert kwargs["path"] == "/"

    @pytest.mark.parametrize("configured,expected", [
        ("strict", "strict"),
        ("none", "none"),
        ("", "lax"),          # unset falls back to the documented default
        ("garbage", "lax"),   # and so does an unusable value — never a 500 on login
    ])
    def test_samesite_never_resolves_to_something_a_browser_would_reject(
            self, main_mod, configured, expected):
        assert self._issued(main_mod, samesite=configured)["samesite"] == expected

    def test_the_four_query_param_mutating_routes_are_post_only(self, main_mod):
        """`Lax` still sends the cookie on a top-level GET navigation, so a state-changing route
        reachable by GET would be CSRF-able by a bare link. These four are not.
        """
        wanted = {p.rstrip("/") for p, _ in TestQueryParamMutatingRoutesStillRefuseAnonymous.ROUTES}
        seen = set()
        for route in main_mod.router.routes:
            path = getattr(route, "path", "").rstrip("/")
            if path in wanted:
                seen.add(path)
                assert set(route.methods) == {"POST"}, f"{path} accepts {sorted(route.methods)}"
        assert seen == wanted, f"routes not found in the table: {sorted(wanted - seen)}"


class TestAppInfo:
    def test_returns_version_and_toggle(self, main_mod, client):
        with patch("cqc_lem.utilities.env_constants.get_app_version", return_value="1.2.3"), \
             patch("cqc_lem.utilities.env_constants.SHOW_VERSION_FOOTER", True):
            resp = client.get("/api/app-info")
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"version": "1.2.3", "show_version": True}

    def test_reachable_when_gate_enabled(self, main_mod, client):
        # The footer loads pre-login, so /api/app-info must clear the bearer gate.
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"tok"}):
            resp = client.get("/api/app-info")
        assert resp.status_code == 200
