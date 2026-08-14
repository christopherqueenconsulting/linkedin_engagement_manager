"""Integration tests for the LinkedIn display-name settings API (issue #731).

The name is what reply detection compares a DM thread's last sender against, so the endpoint is a
REQUIRED field: an empty save is a 400, not a silent clear that would leave the follow-up sequencer
skipping every person it looks at.
"""

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_API = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"


class TestGetDisplayName:
    def test_returns_saved_name_and_the_scraped_suggestion(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_user_linkedin_display_name", return_value="Christopher Queen"), \
             patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id",
                   return_value=(json.dumps({"full_name": "Christopher Queen"}),)):
            r = api_client.get("/api/user/linkedin-display-name?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"] == {"linkedin_display_name": "Christopher Queen",
                                      "profile_full_name": "Christopher Queen"}

    def test_unsaved_name_still_offers_the_scraped_one(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_user_linkedin_display_name", return_value=None), \
             patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id",
                   return_value=(json.dumps({"full_name": "Jordan Alvarez"}),)):
            r = api_client.get("/api/user/linkedin-display-name?session_token=tok")
        detail = r.json()["detail"]
        assert detail["linkedin_display_name"] is None
        assert detail["profile_full_name"] == "Jordan Alvarez"

    def test_an_unreadable_profile_is_no_suggestion_not_an_error(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_user_linkedin_display_name", return_value="Jordan Alvarez"), \
             patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id",
                   side_effect=RuntimeError("db down")):
            r = api_client.get("/api/user/linkedin-display-name?session_token=tok")
        assert r.status_code == 200
        assert r.json()["detail"]["profile_full_name"] is None

    def test_401_without_a_session(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=None):
            r = api_client.get("/api/user/linkedin-display-name?session_token=bad")
        assert r.status_code == 401


class TestPutDisplayName:
    def test_saves_a_whitespace_normalised_name(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_user_linkedin_display_name", return_value=True) as saved:
            r = api_client.put("/api/user/linkedin-display-name",
                              json={"session_token": "tok",
                                    "linkedin_display_name": "  Christopher   Queen "})
        assert r.status_code == 200
        saved.assert_called_once_with(1, "Christopher Queen")

    def test_empty_name_is_rejected(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_user_linkedin_display_name") as saved:
            r = api_client.put("/api/user/linkedin-display-name",
                              json={"session_token": "tok", "linkedin_display_name": "   "})
        assert r.status_code == 400
        saved.assert_not_called()

    def test_overlong_name_is_rejected(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_user_linkedin_display_name") as saved:
            r = api_client.put("/api/user/linkedin-display-name",
                              json={"session_token": "tok", "linkedin_display_name": "x" * 256})
        assert r.status_code == 400
        saved.assert_not_called()

    def test_a_failed_write_is_a_500(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_user_linkedin_display_name", return_value=False):
            r = api_client.put("/api/user/linkedin-display-name",
                              json={"session_token": "tok", "linkedin_display_name": "Jordan"})
        assert r.status_code == 500

    def test_401_without_a_session(self, api_client):
        with patch(f"{_API}.get_session_user_id", return_value=None), \
             patch(f"{_USER}.update_user_linkedin_display_name") as saved:
            r = api_client.put("/api/user/linkedin-display-name",
                              json={"session_token": "bad", "linkedin_display_name": "Jordan"})
        assert r.status_code == 401
        saved.assert_not_called()
