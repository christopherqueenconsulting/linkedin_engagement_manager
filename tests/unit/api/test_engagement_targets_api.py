"""Unit tests for the /api/user/engagement-targets roster CRUD (issue #616)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient
        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


_SESSION = "tok"
_USER = 5
_URL = "https://www.linkedin.com/in/jane"


class TestGetEngagementTargets:
    def test_returns_targets_and_suggestions(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_engagement_targets",
                   return_value=[{"profile_url": _URL, "category": "peer"}]), \
             patch("cqc_lem.api.main.suggest_engagement_targets",
                   return_value=[{"profile_url": "https://x/in/bob", "category": "icp"}]):
            resp = client.get(f"/api/user/engagement-targets?session_token={_SESSION}")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["targets"][0]["profile_url"] == _URL
        assert detail["suggestions"][0]["category"] == "icp"

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/engagement-targets?session_token=bad")
        assert resp.status_code == 401


class TestUpdateEngagementTargets:
    def test_upserts(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_engagement_targets", return_value=True) as upd:
            resp = client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "name": "Jane", "category": "creator",
                             "max_comments_per_week": 3, "active": True, "source": "user"}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["category"] == "creator"

    def test_unknown_category_falls_back_to_peer(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_engagement_targets", return_value=True) as upd:
            resp = client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "category": "influencer"}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["category"] == "peer"

    def test_out_of_range_weekly_cap_is_clamped_not_rejected(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_engagement_targets", return_value=True) as upd:
            resp = client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION,
                "targets": [{"profile_url": _URL, "max_comments_per_week": 999}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["max_comments_per_week"] == 14

    def test_over_long_url_is_422(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER):
            resp = client.put("/api/user/engagement-targets", json={
                "session_token": _SESSION, "targets": [{"profile_url": "h" * 513}]})
        assert resp.status_code == 422

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_engagement_targets", return_value=False):
            resp = client.put("/api/user/engagement-targets",
                              json={"session_token": _SESSION, "targets": []})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/engagement-targets",
                              json={"session_token": "bad", "targets": []})
        assert resp.status_code == 401


class TestDeleteEngagementTarget:
    def test_deletes(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.delete_engagement_target", return_value=True) as dele:
            resp = client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": _SESSION, "profile_url": _URL})
        assert resp.status_code == 200
        dele.assert_called_once_with(_USER, _URL)

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.delete_engagement_target", return_value=False):
            resp = client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": _SESSION, "profile_url": _URL})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.request("DELETE", "/api/user/engagement-targets",
                                  json={"session_token": "bad", "profile_url": _URL})
        assert resp.status_code == 401
