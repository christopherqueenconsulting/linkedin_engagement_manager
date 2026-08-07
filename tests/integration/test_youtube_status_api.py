"""Integration test for the YouTube publishing endpoints (issue #742).

Drives the real GET /api/admin/youtube-status and POST /api/admin/youtube-token handlers through
the real `youtube_auth` probe logic, with only the token endpoint, the credential table and Redis
faked — so the whole path that turns "Google says invalid_grant" into "needs re-auth (reason)" on
the settings card is exercised, including the two things this endpoint pair must never do: leak the
refresh token, or let a non-admin session read it.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, **kw):
        self.store[key] = value
        return True


def _response(status_code=200, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch):
    """A configured install whose token lives in .env, with an in-memory credential store."""
    from cqc_lem.utilities.marketing import youtube_auth as ya
    stored = {}
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "env-token")
    monkeypatch.setattr(ya, "get_app_credential", lambda name: stored.get(name))
    monkeypatch.setattr(ya, "get_app_credential_updated_at", lambda name: None)
    monkeypatch.setattr(ya, "set_app_credential",
                        lambda name, value, note=None: stored.update({name: value}) or True)
    monkeypatch.setattr(ya, "_redis", _FakeRedis)
    return stored


def _status(client, response, live=False):
    with patch(f"{_M}.get_session_user_id", return_value=42), \
         patch(f"{_M}.is_user_admin", return_value=True), \
         patch("requests.post", return_value=response):
        return client.get(f"/api/admin/youtube-status?session_token=t&live={str(live).lower()}")


def test_a_live_token_reads_as_connected(client, env):
    from cqc_lem.utilities.marketing.youtube_auth import UPLOAD_SCOPE
    response = _status(client, _response(payload={"access_token": "at", "scope": UPLOAD_SCOPE}))
    assert response.status_code == 200
    detail = response.json()["detail"]
    assert detail["connected"] is True and detail["status"] == "ok"
    assert detail["configured"] is True and detail["token_source"] == "env"


def test_a_dead_grant_reads_as_needs_reauth_with_the_reason(client, env):
    response = _status(client, _response(400, {"error": "invalid_grant",
                                               "error_description": "Token has been expired"}))
    detail = response.json()["detail"]
    assert detail["connected"] is False and detail["status"] == "needs_reauth"
    assert detail["error"] == "invalid_grant" and "expired" in detail["reason"]
    assert detail["runbook"] == "docs/youtube-publishing.md"


def test_an_unreachable_google_reads_as_unknown_not_broken(client, env):
    with patch(f"{_M}.get_session_user_id", return_value=42), \
         patch(f"{_M}.is_user_admin", return_value=True), \
         patch("requests.post", side_effect=OSError("network down")):
        detail = client.get("/api/admin/youtube-status?session_token=t").json()["detail"]
    assert detail["status"] == "unknown" and detail["connected"] is False


def test_the_refresh_token_is_never_returned(client, env):
    response = _status(client, _response(payload={"access_token": "at"}))
    assert "env-token" not in json.dumps(response.json())


def test_a_non_admin_session_is_refused(client, env):
    with patch(f"{_M}.get_session_user_id", return_value=42), \
         patch(f"{_M}.is_user_admin", return_value=False), \
         patch("requests.post") as post:
        response = client.get("/api/admin/youtube-status?session_token=t")
    assert response.status_code == 403
    post.assert_not_called()


def test_an_expired_session_is_refused(client, env):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert client.get("/api/admin/youtube-status?session_token=t").status_code == 401


def test_installing_a_token_stores_it_probes_it_and_takes_precedence_over_env(client, env,
                                                                             monkeypatch):
    from cqc_lem.utilities.marketing import youtube_auth as ya
    monkeypatch.setattr(f"{_M}.ADMIN_SECRET", "s3cret")
    with patch("requests.post", return_value=_response(payload={"access_token": "at"})) as post:
        response = client.post("/api/admin/youtube-token",
                               json={"refresh_token": "  db-token  "},
                               headers={"x-admin-secret": "s3cret"})
    assert response.status_code == 200
    assert response.json()["detail"] == {"stored": True, "status": "ok",
                                         "reason": "Refresh token exchanged for an access token"}
    assert env[ya.CREDENTIAL_NAME] == "db-token"          # trimmed on the way in
    assert post.call_args.kwargs["data"]["refresh_token"] == "db-token"  # DB value wins over .env


def test_installing_a_token_requires_the_admin_secret(client, env, monkeypatch):
    monkeypatch.setattr(f"{_M}.ADMIN_SECRET", "s3cret")
    with patch("requests.post") as post:
        response = client.post("/api/admin/youtube-token", json={"refresh_token": "x"})
    assert response.status_code == 403
    post.assert_not_called()


def test_an_empty_token_is_rejected_before_anything_is_stored(client, env, monkeypatch):
    monkeypatch.setattr(f"{_M}.ADMIN_SECRET", "s3cret")
    response = client.post("/api/admin/youtube-token", json={"refresh_token": "   "},
                           headers={"x-admin-secret": "s3cret"})
    assert response.status_code == 422 and env == {}
