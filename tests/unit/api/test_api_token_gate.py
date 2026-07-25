"""Unit tests for the /api bearer-token gate in cqc_lem.api.main."""

from unittest.mock import patch

import pytest

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

    def test_guarded_route_without_token_is_401(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE)
        assert resp.status_code == 401

    def test_guarded_route_wrong_token_is_401(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_guarded_route_valid_token_passes_gate(self, main_mod, client):
        # Valid token clears the gate; routing then 404s on the unknown path.
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {self.TOKEN}):
            resp = client.get(self.GATED_PROBE, headers={"Authorization": f"Bearer {self.TOKEN}"})
        assert resp.status_code != 401

    def test_gate_disabled_allows_unauthenticated(self, main_mod, client):
        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", set()):
            resp = client.get("/api/assets", params={"file_name": "x.png"})
        assert resp.status_code != 401


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
