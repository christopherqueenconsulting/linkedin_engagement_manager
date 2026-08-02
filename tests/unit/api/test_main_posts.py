"""Unit tests for post-related API endpoints:
  GET  /api/posts/
  POST /api/posts/bulk_update/
  DELETE /api/posts/
  POST /api/update_post/
"""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.api.main"

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID  # noqa: E402

_SAMPLE_POST = {
    "id": 1,
    "content": "Test LinkedIn post content",
    "video_url": None,
    "scheduled_time": "2024-06-01T10:00:00",
    "post_type": "text",
    "status": "pending",
    "carousel_slides": None,
}

_POST_BODY = {
    "session_token": SESSION_TOKEN,
    "content": "Test content",
    "video_url": None,
    "scheduled_datetime": "2024-06-01T10:00:00",
    "post_type": "text",
    "status": "pending",
}


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


# ---------------------------------------------------------------------------
# GET /api/posts/
# ---------------------------------------------------------------------------

class TestGetPostsForEmail:

    def test_returns_200_with_posts(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([_SAMPLE_POST], 1)):
            resp = client.get("/api/posts/", params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        body = resp.json()
        assert body["detail"]["total"] == 1
        assert body["detail"]["page"] == 1
        assert body["detail"]["page_size"] == 10
        assert len(body["detail"]["posts"]) == 1
        assert body["detail"]["posts"][0]["post_id"] == 1

    def test_no_session_returns_401(self, client):
        with patch(f"{_DB}.get_session_user_id", return_value=None):
            resp = client.get("/api/posts/")
        assert resp.status_code == 401

    def test_another_accounts_email_returns_403(self, client, signed_in):
        """A valid bearer token plus somebody else's address reads nothing (issue #914)."""
        with patch(f"{_DB}.get_posts") as mock_get:
            resp = client.get("/api/posts/", params={"session_token": SESSION_TOKEN,
                                                     "email": "victim@example.com"})
        assert resp.status_code == 403
        mock_get.assert_not_called()

    def test_pagination_params_forwarded(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([], 0)) as mock_get:
            resp = client.get(
                "/api/posts/",
                params={"session_token": SESSION_TOKEN, "page": 2, "page_size": 5},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["detail"]["page"] == 2
        assert body["detail"]["page_size"] == 5
        # offset for page 2 with page_size 5 is 5
        mock_get.assert_called_once_with(
            SESSION_USER_ID,
            limit=5,
            offset=5,
            sort_order="asc",
            status_filter=None,
            post_type_filter=None,
            search=None,
            sort_by="scheduled_time",
            start_date=None,
            end_date=None,
        )

    def test_sort_order_desc(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([], 0)) as mock_get:
            resp = client.get(
                "/api/posts/",
                params={"session_token": SESSION_TOKEN, "sort_order": "desc"},
            )
        assert resp.status_code == 200
        mock_get.assert_called_once_with(
            SESSION_USER_ID,
            limit=10,
            offset=0,
            sort_order="desc",
            status_filter=None,
            post_type_filter=None,
            search=None,
            sort_by="scheduled_time",
            start_date=None,
            end_date=None,
        )

    def test_status_filter_forwarded(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([_SAMPLE_POST], 1)) as mock_get:
            resp = client.get(
                "/api/posts/",
                params={"session_token": SESSION_TOKEN, "status_filter": "pending"},
            )
        assert resp.status_code == 200
        mock_get.assert_called_once_with(
            SESSION_USER_ID,
            limit=10,
            offset=0,
            sort_order="asc",
            status_filter="pending",
            post_type_filter=None,
            search=None,
            sort_by="scheduled_time",
            start_date=None,
            end_date=None,
        )

    def test_post_type_and_search_and_sort_by_forwarded(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([], 0)) as mock_get:
            resp = client.get(
                "/api/posts/",
                params={
                    "session_token": SESSION_TOKEN,
                    "post_type_filter": "carousel",
                    "search": "ai AND marketing",
                    "sort_by": "status",
                },
            )
        assert resp.status_code == 200
        mock_get.assert_called_once_with(
            SESSION_USER_ID,
            limit=10,
            offset=0,
            sort_order="asc",
            status_filter=None,
            post_type_filter="carousel",
            search="ai AND marketing",
            sort_by="status",
            start_date=None,
            end_date=None,
        )

    def test_invalid_post_type_filter_422(self, client, signed_in):
        resp = client.get(
            "/api/posts/",
            params={"session_token": SESSION_TOKEN, "post_type_filter": "gif"},
        )
        assert resp.status_code == 422

    def test_invalid_sort_by_422(self, client, signed_in):
        resp = client.get(
            "/api/posts/",
            params={"session_token": SESSION_TOKEN, "sort_by": "content"},
        )
        assert resp.status_code == 422

    def test_carousel_slides_json_string_parsed(self, client, signed_in):
        """carousel_slides stored as a JSON string should be decoded to a list."""
        post_with_slides = dict(_SAMPLE_POST, carousel_slides='["slide1.png", "slide2.png"]')
        with patch(f"{_DB}.get_posts", return_value=([post_with_slides], 1)):
            resp = client.get("/api/posts/", params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        slides = resp.json()["detail"]["posts"][0]["carousel_slides"]
        assert slides == ["slide1.png", "slide2.png"]

    def test_empty_posts_list_returns_200(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([], 0)):
            resp = client.get("/api/posts/", params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["total"] == 0
        assert resp.json()["detail"]["posts"] == []

    def test_date_range_params_forwarded(self, client, signed_in):
        with patch(f"{_DB}.get_posts", return_value=([], 0)) as mock_get:
            resp = client.get(
                "/api/posts/",
                params={
                    "session_token": SESSION_TOKEN,
                    "start_date": "2026-07-01T00:00:00Z",
                    "end_date": "2026-07-31T23:59:59Z",
                },
            )
        assert resp.status_code == 200
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        assert kwargs["start_date"] is not None
        assert kwargs["end_date"] is not None

    def test_malformed_start_date_returns_422(self, client, signed_in):
        resp = client.get(
            "/api/posts/",
            params={"session_token": SESSION_TOKEN, "start_date": "not-a-date"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/posts/bulk_update/
# ---------------------------------------------------------------------------

class TestBulkUpdatePostsEndpoint:

    def test_returns_200_on_success(self, client, signed_in):
        with patch(f"{_DB}.bulk_update_posts", return_value=True):
            resp = client.post(
                "/api/posts/bulk_update/",
                json={"session_token": SESSION_TOKEN, "post_ids": [1, 2], "status": "approved"},
            )
        assert resp.status_code == 200
        assert "updated" in resp.json()["detail"].lower()

    def test_empty_post_ids_returns_400(self, client, signed_in):
        resp = client.post(
            "/api/posts/bulk_update/",
            json={"session_token": SESSION_TOKEN, "post_ids": []},
        )
        assert resp.status_code == 400

    def test_bulk_update_failure_returns_405(self, client, signed_in):
        with patch(f"{_DB}.bulk_update_posts", return_value=False):
            resp = client.post(
                "/api/posts/bulk_update/",
                json={"session_token": SESSION_TOKEN, "post_ids": [1, 2], "status": "approved"},
            )
        assert resp.status_code == 405

    def test_status_only_update(self, client, signed_in):
        with patch(f"{_DB}.bulk_update_posts", return_value=True) as mock_update:
            resp = client.post(
                "/api/posts/bulk_update/",
                json={"session_token": SESSION_TOKEN, "post_ids": [3], "status": "pending"},
            )
        assert resp.status_code == 200
        mock_update.assert_called_once()

    def test_scheduled_datetime_update(self, client, signed_in):
        with patch(f"{_DB}.bulk_update_posts", return_value=True) as mock_update:
            resp = client.post(
                "/api/posts/bulk_update/",
                json={"session_token": SESSION_TOKEN, "post_ids": [4, 5], "scheduled_datetime": "2024-07-01T09:00:00"},
            )
        assert resp.status_code == 200
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# DELETE /api/posts/
# ---------------------------------------------------------------------------

class TestDeletePostsEndpoint:

    def test_returns_200_on_success(self, client, signed_in):
        with patch(f"{_DB}.soft_delete_posts", return_value=True):
            resp = client.request(
                "DELETE",
                "/api/posts/",
                json={"session_token": SESSION_TOKEN, "post_ids": [1, 2]},
            )
        assert resp.status_code == 200
        assert "deleted" in resp.json()["detail"].lower()

    def test_empty_post_ids_returns_400(self, client, signed_in):
        resp = client.request(
            "DELETE",
            "/api/posts/",
            json={"session_token": SESSION_TOKEN, "post_ids": []},
        )
        assert resp.status_code == 400

    def test_soft_delete_failure_returns_405(self, client, signed_in):
        with patch(f"{_DB}.soft_delete_posts", return_value=False):
            resp = client.request(
                "DELETE",
                "/api/posts/",
                json={"session_token": SESSION_TOKEN, "post_ids": [1, 2]},
            )
        assert resp.status_code == 405

    def test_calls_soft_delete_with_correct_ids(self, client, signed_in):
        with patch(f"{_DB}.soft_delete_posts", return_value=True) as mock_delete:
            client.request(
                "DELETE",
                "/api/posts/",
                json={"session_token": SESSION_TOKEN, "post_ids": [7, 8, 9]},
            )
        mock_delete.assert_called_once_with([7, 8, 9], rejection_reason=None,
                                            user_id=SESSION_USER_ID)

    def test_passes_rejection_reason_to_soft_delete(self, client, signed_in):
        with patch(f"{_DB}.soft_delete_posts", return_value=True) as mock_delete:
            resp = client.request(
                "DELETE",
                "/api/posts/",
                json={"session_token": SESSION_TOKEN, "post_ids": [7], "rejection_reason": "Too salesy"},
            )
        assert resp.status_code == 200
        mock_delete.assert_called_once_with([7], rejection_reason="Too salesy",
                                            user_id=SESSION_USER_ID)

    def test_blank_rejection_reason_becomes_none(self, client, signed_in):
        with patch(f"{_DB}.soft_delete_posts", return_value=True) as mock_delete:
            client.request(
                "DELETE",
                "/api/posts/",
                json={"session_token": SESSION_TOKEN, "post_ids": [7], "rejection_reason": "   "},
            )
        mock_delete.assert_called_once_with([7], rejection_reason=None, user_id=SESSION_USER_ID)

    def test_rejection_reason_too_long_returns_422(self, client, signed_in):
        resp = client.request(
            "DELETE",
            "/api/posts/",
            json={"session_token": SESSION_TOKEN, "post_ids": [7], "rejection_reason": "x" * 1001},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/update_post/
# ---------------------------------------------------------------------------

class TestUpdatePost:

    def test_returns_200_on_success(self, client, signed_in):
        with patch(f"{_DB}.update_db_post", return_value=True):
            resp = client.post(
                "/api/update_post/",
                params={"post_id": 42},
                json=_POST_BODY,
            )
        assert resp.status_code == 200
        assert "updated" in resp.json()["detail"].lower()

    def test_update_failure_returns_405(self, client, signed_in):
        with patch(f"{_DB}.update_db_post", return_value=False):
            resp = client.post(
                "/api/update_post/",
                params={"post_id": 42},
                json=_POST_BODY,
            )
        assert resp.status_code == 405

    def test_missing_post_id_returns_422(self, client, signed_in):
        """FastAPI rejects the request when post_id query param is missing."""
        with patch(f"{_DB}.update_db_post", return_value=True):
            resp = client.post("/api/update_post/", json=_POST_BODY)
        assert resp.status_code == 422

    def test_calls_update_db_post_with_correct_args(self, client, signed_in):
        with patch(f"{_DB}.update_db_post", return_value=True) as mock_update:
            client.post(
                "/api/update_post/",
                params={"post_id": 99},
                json=_POST_BODY,
            )
        mock_update.assert_called_once()
        call_args = mock_update.call_args
        # update_db_post(content, video_url, scheduled_datetime, post_type, post_id, status)
        assert call_args.args[0] == "Test content"   # content
        assert call_args.args[4] == 99               # post_id

    def test_with_video_url(self, client, signed_in):
        body = dict(_POST_BODY, video_url="https://example.com/video.mp4")
        with patch(f"{_DB}.update_db_post", return_value=True) as mock_update:
            resp = client.post(
                "/api/update_post/",
                params={"post_id": 10},
                json=body,
            )
        assert resp.status_code == 200
        call_args = mock_update.call_args
        assert call_args.args[1] == "https://example.com/video.mp4"

    def test_rejection_reason_persisted_on_update(self, client, signed_in):
        body = dict(_POST_BODY, status="rejected", rejection_reason="Not relevant")
        with patch(f"{_DB}.update_db_post", return_value=True), \
             patch(f"{_DB}.update_db_post_rejection_reason", return_value=True) as mock_reason:
            resp = client.post(
                "/api/update_post/",
                params={"post_id": 10},
                json=body,
            )
        assert resp.status_code == 200
        mock_reason.assert_called_once_with(10, "Not relevant")

    def test_blank_rejection_reason_not_persisted(self, client, signed_in):
        body = dict(_POST_BODY, status="rejected", rejection_reason="   ")
        with patch(f"{_DB}.update_db_post", return_value=True), \
             patch(f"{_DB}.update_db_post_rejection_reason") as mock_reason:
            resp = client.post(
                "/api/update_post/",
                params={"post_id": 10},
                json=body,
            )
        assert resp.status_code == 200
        mock_reason.assert_not_called()
