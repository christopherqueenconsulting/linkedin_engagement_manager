"""Integration tests for the per-user content-language setting (issue #548).

The language is what stops an audio-capable video model inventing a foreign-language
voiceover, so it has to be readable AND settable through /api/user/settings.
"""

from unittest.mock import patch

import pytest


@pytest.mark.integration
class TestGetUserSettingsLanguage:
    def test_returns_explicit_and_effective_language(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.user.get_user_subscription_info", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_preferences",
                   return_value={"last_login_inactivate_delay": 90, "auto_schedule_posts": 1,
                                 "content_buffer_days": 7, "content_buffer_max_posts": 5,
                                 "content_language": "es-ES"}), \
             patch("cqc_lem.api.routers.user.get_user_content_language", return_value="es-ES"), \
             patch("cqc_lem.api.routers.user.get_user_blog_url", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_sitemap_url", return_value=None), \
             patch("cqc_lem.api.routers.user.get_company_linked_in_url_for_user", return_value=None):
            response = api_client.get("/api/user/settings?session_token=t")

        assert response.status_code == 200
        prefs = response.json()["detail"]["preferences"]
        assert prefs["content_language"] == "es-ES"
        assert prefs["effective_content_language"] == "es-ES"

    def test_unset_language_reports_the_inherited_default(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.user.get_user_subscription_info", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_preferences",
                   return_value={"last_login_inactivate_delay": 90, "auto_schedule_posts": 1,
                                 "content_buffer_days": 7, "content_buffer_max_posts": 5,
                                 "content_language": None}), \
             patch("cqc_lem.api.routers.user.get_user_content_language", return_value="en-GB"), \
             patch("cqc_lem.api.routers.user.get_user_blog_url", return_value=None), \
             patch("cqc_lem.api.routers.user.get_user_sitemap_url", return_value=None), \
             patch("cqc_lem.api.routers.user.get_company_linked_in_url_for_user", return_value=None):
            response = api_client.get("/api/user/settings?session_token=t")

        prefs = response.json()["detail"]["preferences"]
        assert prefs["content_language"] is None          # nothing explicitly chosen
        assert prefs["effective_content_language"] == "en-GB"  # Login Location locale


@pytest.mark.integration
class TestUpdateUserSettingsLanguage:
    def test_language_is_forwarded_to_the_db(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.user.update_user_preferences", return_value=True) as upd:
            response = api_client.put("/api/user/settings", json={
                "session_token": "t", "last_login_inactivate_delay": 90,
                "auto_schedule_posts": True, "content_language": "pt-BR",
            })

        assert response.status_code == 200
        assert upd.call_args[1]["content_language"] == "pt-BR"

    def test_omitted_language_is_left_unchanged(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.user.update_user_preferences", return_value=True) as upd:
            api_client.put("/api/user/settings", json={
                "session_token": "t", "last_login_inactivate_delay": 90,
                "auto_schedule_posts": True,
            })

        assert upd.call_args[1]["content_language"] is None

    def test_over_long_language_is_rejected_not_truncated_by_mysql(self, api_client):
        # Mirrors the V52 lesson: an over-long value must 422 here, never reach the column.
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.routers.user.update_user_preferences", return_value=True) as upd:
            response = api_client.put("/api/user/settings", json={
                "session_token": "t", "last_login_inactivate_delay": 90,
                "auto_schedule_posts": True, "content_language": "x" * 17,
            })

        assert response.status_code == 422
        upd.assert_not_called()
