"""Integration tests for FastAPI endpoints."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest


@pytest.mark.integration
class TestHealthEndpoint:
    def test_health_returns_200(self, api_client):
        response = api_client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


# Issue #914: these endpoints resolve the acting user from the SESSION, so an integration run has
# to present one. `_signed_in` stands in for the session row the SPA would be carrying in its
# httpOnly cookie; the parameter each request still sends is a TARGET that has to match it.
_SESSION_TOKEN = "integration-session-token"
_USER_ID = 1
_EMAIL = "test@example.com"


@contextmanager
def _signed_in(**extra):
    patches = {
        "get_session_user_id": _USER_ID,
        "get_user_email": _EMAIL,
        "user_owns_posts": True,
        **extra,
    }
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(f"cqc_lem.api.main.{name}", return_value=value))
            for name, value in patches.items()
        }
        yield mocks


@pytest.mark.integration
class TestSchedulePostEndpoint:
    def test_schedule_post_returns_200_for_the_session_user(self, api_client):
        with _signed_in(insert_post=True):
            response = api_client.post("/api/schedule_post/", json={
                "session_token": _SESSION_TOKEN,
                "email": _EMAIL,
                "content": "Test post content",
                "scheduled_datetime": "2025-06-01T12:00:00",
                "post_type": "text",
                "status": "pending",
            })

        assert response.status_code == 200
        assert response.json()["status_code"] == 200

    def test_schedule_post_returns_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.insert_post") as mock_insert:
            response = api_client.post("/api/schedule_post/", json={
                "email": "nobody@example.com",
                "content": "Test post",
                "scheduled_datetime": "2025-06-01T12:00:00",
                "post_type": "text",
                "status": "pending",
            })

        assert response.status_code == 401
        mock_insert.assert_not_called()

    def test_schedule_post_returns_403_for_another_account(self, api_client):
        with _signed_in() as mocks:
            mocks["get_user_email"].return_value = _EMAIL
            with patch("cqc_lem.api.main.insert_post") as mock_insert:
                response = api_client.post("/api/schedule_post/", json={
                    "session_token": _SESSION_TOKEN,
                    "email": "victim@example.com",
                    "content": "Test post",
                    "scheduled_datetime": "2025-06-01T12:00:00",
                    "post_type": "text",
                    "status": "pending",
                })

        assert response.status_code == 403
        mock_insert.assert_not_called()


@pytest.mark.integration
class TestGetPostsEndpoint:
    def test_get_posts_returns_200_with_posts(self, api_client):
        rows = (
            [
                {
                    "id": 1, "content": "Hello world", "video_url": None,
                    "scheduled_time": "2025-01-01T12:00:00", "post_type": "text",
                    "status": "pending", "carousel_slides": None,
                }
            ],
            1,
        )
        with _signed_in(get_posts=rows):
            response = api_client.get(f"/api/posts/?session_token={_SESSION_TOKEN}")

        assert response.status_code == 200
        body = response.json()
        assert body["status_code"] == 200
        assert len(body["detail"]["posts"]) == 1

    def test_get_posts_returns_200_with_empty_list_when_no_posts(self, api_client):
        with _signed_in(get_posts=([], 0)):
            response = api_client.get(f"/api/posts/?session_token={_SESSION_TOKEN}")

        assert response.status_code == 200
        body = response.json()
        assert body["detail"]["posts"] == []
        assert body["detail"]["total"] == 0

    def test_get_posts_returns_401_without_a_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.get_posts") as mock_posts:
            response = api_client.get("/api/posts/")

        assert response.status_code == 401
        mock_posts.assert_not_called()

    def test_get_posts_returns_403_for_another_accounts_email(self, api_client):
        with _signed_in(), patch("cqc_lem.api.main.get_posts") as mock_posts:
            response = api_client.get(
                f"/api/posts/?session_token={_SESSION_TOKEN}&email=victim@example.com")

        assert response.status_code == 403
        mock_posts.assert_not_called()


@pytest.mark.integration
class TestUpdatePostEndpoint:
    def test_update_post_returns_200(self, api_client):
        with _signed_in(update_db_post=True):
            response = api_client.post("/api/update_post/?post_id=1", json={
                "session_token": _SESSION_TOKEN,
                "email": _EMAIL,
                "content": "Updated content",
                "scheduled_datetime": "2025-06-01T12:00:00",
                "post_type": "text",
                "status": "approved",
            })

        assert response.status_code == 200

    def test_update_post_returns_403_for_a_post_the_caller_does_not_own(self, api_client):
        with _signed_in(user_owns_posts=False), \
             patch("cqc_lem.api.main.update_db_post") as mock_update:
            response = api_client.post("/api/update_post/?post_id=4242", json={
                "session_token": _SESSION_TOKEN,
                "content": "Updated content",
                "scheduled_datetime": "2025-06-01T12:00:00",
                "post_type": "text",
                "status": "approved",
            })

        assert response.status_code == 403
        mock_update.assert_not_called()
