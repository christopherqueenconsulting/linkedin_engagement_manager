"""Unit tests for the /api/user/engagement-targets roster CRUD (issue #616)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_SESSION = "tok"
_USER = 5
_URL = "https://www.linkedin.com/in/jane"


class TestGetEngagementTargets:
    def test_returns_targets_and_suggestions(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_engagement_targets",
                   return_value=[{"profile_url": _URL, "category": "peer"}]), \
             patch("cqc_lem.api.routers.user.suggest_engagement_targets",
                   return_value=[{"profile_url": "https://x/in/bob", "category": "icp"}]):
            resp = api_client.get(f"/api/user/engagement-targets?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["targets"][0]["profile_url"] == _URL
        assert detail["suggestions"][0]["category"] == "icp"

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/engagement-targets?session_token=bad")
        assert resp.status_code == 401


class TestUpdateEngagementTargets:
    def test_upserts(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_engagement_targets", return_value=True) as upd:
            resp = api_client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "name": "Jane", "category": "creator",
                             "max_comments_per_week": 3, "active": True, "source": "user"}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["category"] == "creator"

    def test_unknown_category_falls_back_to_peer(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_engagement_targets", return_value=True) as upd:
            resp = api_client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "category": "influencer"}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["category"] == "peer"

    def test_out_of_range_weekly_cap_is_clamped_not_rejected(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_engagement_targets", return_value=True) as upd:
            resp = api_client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "max_comments_per_week": 999}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["max_comments_per_week"] == 14

    def test_over_long_url_is_422(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER):
            resp = api_client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION, "targets": [{"profile_url": "h" * 513}]})
        assert resp.status_code == 422

    def test_500_on_failure(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_engagement_targets", return_value=False):
            resp = api_client.put("/api/user/engagement-targets",
                              json={"session_token": _SESSION, "targets": []})
        assert resp.status_code == 500

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.put("/api/user/engagement-targets",
                              json={"session_token": "bad", "targets": []})
        assert resp.status_code == 401


class TestDeleteEngagementTarget:
    def test_deletes(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.delete_engagement_target", return_value=True) as dele:
            resp = api_client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": _SESSION, "profile_url": _URL})
        assert resp.status_code == 200
        dele.assert_called_once_with(_USER, _URL)

    def test_500_on_failure(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.delete_engagement_target", return_value=False):
            resp = api_client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": _SESSION, "profile_url": _URL})
        assert resp.status_code == 500

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": "bad", "profile_url": _URL})
        assert resp.status_code == 401
