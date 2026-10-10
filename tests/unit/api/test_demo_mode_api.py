"""DEMO_MODE at the API boundary (#2372): the LinkedIn OAuth routes refuse with a readable 503."""

from unittest.mock import patch

import pytest


@pytest.fixture
def main_mod():
    from cqc_lem.api import main
    return main


def test_oauth_init_refused_in_demo_mode(main_mod, api_client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    with patch.object(main_mod, "AuthClient") as auth_client:
        resp = api_client.get("/auth/linkedin/", follow_redirects=False)
    assert resp.status_code == 503
    assert "Demo mode" in resp.json()["detail"]
    auth_client.assert_not_called()


def test_oauth_callback_refused_before_the_token_exchange(main_mod, api_client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    with patch.object(main_mod, "AuthClient") as auth_client, \
         patch.object(main_mod, "RestliClient") as restli:
        resp = api_client.get("/auth/linkedin/callback?code=abc", follow_redirects=False)
    assert resp.status_code == 503
    auth_client.assert_not_called()
    restli.assert_not_called()


def test_oauth_init_redirects_when_off(main_mod, api_client, monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch.object(main_mod, "AuthClient") as auth_client:
        auth_client.return_value.generate_member_auth_url.return_value = "https://www.linkedin.com/x"
        resp = api_client.get("/auth/linkedin/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    auth_client.assert_called_once()
