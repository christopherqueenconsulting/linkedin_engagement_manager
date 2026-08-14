"""Unit tests for GET /api/extension/linkedin-connect.zip."""

import io
import zipfile
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestDownloadExtension:
    def test_returns_zip_with_extension_files(self, api_client):
        resp = api_client.get("/api/extension/linkedin-connect.zip")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "lem-linkedin-connect.zip" in resp.headers["content-disposition"]

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert zf.testzip() is None  # no corrupt members
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "popup.html" in names
        assert "popup.js" in names

    def test_manifest_is_valid_mv3(self, api_client):
        import json
        resp = api_client.get("/api/extension/linkedin-connect.zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["manifest_version"] == 3
        assert "cookies" in manifest["permissions"]

    def test_route_is_public_when_bearer_gate_enabled(self):
        # The download is a plain <a href> browser GET with no Authorization header, so it
        # must bypass the /api/* bearer-token gate (which is off in tests unless a token is
        # configured). Regression guard for the prod 401.
        from cqc_lem.api import main
        with patch.object(main, "_API_ACCESS_TOKEN_SET", {"secret-token"}):
            assert main._api_token_required("/api/extension/linkedin-connect.zip") is False
            assert main._api_token_required("/api/user/account-readiness") is True

    def test_cookie_post_is_public_but_rest_of_user_is_gated(self):
        # The extension POSTs the LinkedIn session to /api/user/linkedin-cookie without the
        # SPA bearer token; it's self-authenticated by the body's session_token. Only that
        # exact leaf is public — sibling /api/user/* routes stay gated. Regression guard.
        from cqc_lem.api import main
        with patch.object(main, "_API_ACCESS_TOKEN_SET", {"secret-token"}):
            assert main._api_token_required("/api/user/linkedin-cookie") is False
            assert main._api_token_required("/api/user/linkedin-password") is True
            assert main._api_token_required("/api/user/profile") is True
