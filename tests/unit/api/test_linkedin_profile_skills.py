"""Unit tests for GET /api/user/linkedin-profile-skills (issue #1075)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_USER = "cqc_lem.api.routers.user"

from tests.unit.api.conftest import SESSION_TOKEN  # noqa: E402


class TestLinkedInProfileSkills:
    BASE = "/api/user/linkedin-profile-skills"

    def test_no_session_returns_401(self, api_client, no_session):
        resp = api_client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 401

    def test_returns_top_skills_and_adopted_overlap(self, api_client, signed_in):
        profile = object.__new__(object)
        with patch(f"{_USER}.load_profile_for_user", return_value=profile), \
             patch(f"{_USER}.profile_niche_anchors", return_value=["AI Strategy", "Product Growth", "B2B Sales"]), \
             patch(f"{_USER}.get_engagement_preferences", return_value={
                 "focus_topics": ["ai strategy", "leadership"]
             }):
            resp = api_client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["skills"] == ["AI Strategy", "Product Growth", "B2B Sales"]
        assert detail["adopted"] == ["AI Strategy"]
        assert detail["focus_topics"] == ["ai strategy", "leadership"]

    def test_missing_profile_returns_empty_skills(self, api_client, signed_in):
        with patch(f"{_USER}.load_profile_for_user", return_value=None), \
             patch(f"{_USER}.get_engagement_preferences", return_value={"focus_topics": []}):
            resp = api_client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["skills"] == []
        assert detail["adopted"] == []

    def test_load_profile_failure_is_caught(self, api_client, signed_in):
        with patch(f"{_USER}.load_profile_for_user", side_effect=Exception("db down")), \
             patch(f"{_USER}.get_engagement_preferences", return_value={"focus_topics": []}):
            resp = api_client.get(self.BASE, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["skills"] == []
