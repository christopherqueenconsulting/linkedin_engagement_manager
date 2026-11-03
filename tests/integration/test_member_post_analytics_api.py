"""Integration tests for the separate Community Management OAuth connection."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.integration

_API = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_ANALYTICS = "cqc_lem.utilities.linkedin.member_post_analytics"


class TestAnalyticsConnectionSettings:
    def test_status_requires_a_session(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=None):
            response = api_client.get(
                "/api/user/member-post-analytics-connection", params={"session_token": "bad"})

        assert response.status_code == 401

    def test_status_never_returns_saved_token(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.member_post_analytics_user_allowed", return_value=True), \
             patch(f"{_USER}.member_post_analytics_connection_available_for_user", return_value=True), \
             patch(f"{_USER}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_USER}.member_post_analytics_separate_app_enabled", return_value=True), \
             patch(f"{_USER}.get_member_analytics_token_info",
                   return_value={"access_token": "private-token"}), \
             patch(f"{_USER}.get_member_analytics_access_token", return_value="private-token"), \
             patch(f"{_USER}.get_token_expiry", return_value=None):
            response = api_client.get(
                "/api/user/member-post-analytics-connection", params={"session_token": "tok"})

        assert response.status_code == 200
        detail = response.json()["detail"]
        assert detail["available"] is True
        assert detail["connected"] is True
        assert detail["saved_token"] is True
        assert "access_token" not in detail
        assert "private-token" not in response.text

    def test_disconnect_removes_only_the_analytics_token(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.member_post_analytics_user_allowed", return_value=True), \
             patch(f"{_USER}.delete_member_analytics_token", return_value=True) as delete_token:
            response = api_client.delete(
                "/api/user/member-post-analytics-connection", params={"session_token": "tok"})

        assert response.status_code == 200
        delete_token.assert_called_once_with(1)


class TestAnalyticsOAuth:
    def test_start_requests_only_cma_scopes_for_allowlisted_account(self, api_client):
        client = Mock()
        client.generate_member_auth_url.return_value = "https://linkedin.test/authorize"
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.guard_linkedin"), \
             patch(f"{_API}.LI_STATE_SALT", "state-salt"), \
             patch(f"{_API}.AuthClient", return_value=client) as auth_client, \
             patch(f"{_API}.member_post_analytics_connection_available_for_user", return_value=True):
            response = api_client.get(
                "/auth/linkedin/analytics/", params={"session_token": "session-token"})

        assert response.status_code == 307
        auth_client.assert_called_once()
        client.generate_member_auth_url.assert_called_once_with(
            state="state-salt:member_analytics:session-token",
            scopes=["r_basicprofile", "r_member_postAnalytics"],
        )

    def test_cma_callback_stores_app_scoped_identity_without_replacing_publish_token(self, api_client):
        client = Mock()
        client.exchange_auth_code_for_access_token.return_value = SimpleNamespace(
            access_token="cma-token", expires_in=3600, scope="r_basicprofile r_member_postAnalytics")
        restli = Mock()
        restli.get.return_value = SimpleNamespace(
            status_code=200, entity={"id": "cma-member-id", "vanityName": "lem-member"})
        with patch(f"{_API}.guard_linkedin"), \
             patch(f"{_API}.LI_STATE_SALT", "state-salt"), \
             patch(f"{_API}.LI_API_VERSION", "202606"), \
             patch(f"{_API}.LI_MEMBER_POST_ANALYTICS_REDIRECT_URL",
                   "https://lem.test/auth/linkedin/analytics/callback"), \
             patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.member_post_analytics_connection_available_for_user", return_value=True), \
             patch(f"{_API}.AuthClient", return_value=client), \
             patch(f"{_API}.RestliClient", return_value=restli), \
             patch(f"{_API}.get_linkedin_profile_url_by_user_id",
                   return_value="https://www.linkedin.com/in/lem-member/"), \
             patch(f"{_API}.upsert_member_analytics_token", return_value=True) as save_token, \
             patch(f"{_API}.update_user_linkedin_token") as publish_token:
            response = api_client.get("/auth/linkedin/analytics/callback", params={
                "code": "oauth-code", "state": "state-salt:member_analytics:session-token"})

        assert response.status_code == 307
        location = response.headers["location"]
        assert urlparse(location).path == "/account"
        assert parse_qs(urlparse(location).query) == {"li_analytics_connected": ["1"]}
        restli.get.assert_called_once_with(
            resource_path="/me", access_token="cma-token", version_string="202606")
        save_token.assert_called_once_with(1, "cma-member-id", "cma-token", 3600)
        publish_token.assert_not_called()

    def test_main_app_only_requests_cma_scope_when_separate_app_bridge_is_off(self, api_client):
        client = Mock()
        client.generate_member_auth_url.return_value = "https://linkedin.test/authorize"
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.guard_linkedin"), \
             patch(f"{_API}.AuthClient", return_value=client), \
             patch(f"{_API}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_API}.member_post_analytics_separate_app_enabled", return_value=False):
            response = api_client.get("/auth/linkedin/", params={"session_token": "tok"})

        assert response.status_code == 307
        scopes = client.generate_member_auth_url.call_args.kwargs["scopes"]
        assert "r_member_postAnalytics" in scopes

    def test_main_app_keeps_existing_scope_set_for_separate_app_bridge(self, api_client):
        client = Mock()
        client.generate_member_auth_url.return_value = "https://linkedin.test/authorize"
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_API}.guard_linkedin"), \
             patch(f"{_API}.AuthClient", return_value=client), \
             patch(f"{_API}.member_post_analytics_enabled_for_user", return_value=True), \
             patch(f"{_API}.member_post_analytics_separate_app_enabled", return_value=True):
            response = api_client.get("/auth/linkedin/", params={"session_token": "tok"})

        assert response.status_code == 307
        scopes = client.generate_member_auth_url.call_args.kwargs["scopes"]
        assert "r_member_postAnalytics" not in scopes
