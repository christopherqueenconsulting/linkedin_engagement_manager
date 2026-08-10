"""Unit tests for the public brand-showcase endpoint (issue #1299).

The endpoint is public, read-only, and returns only the brand account's already-published posts
with their stored engagement counts. These tests exercise the handler directly through FastAPI's
TestClient while stubbing the DB layer and the feature flag.
"""
import json
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cqc_lem.api import main
from cqc_lem.platform.db.enums import PostStatus

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_BRAND = "cqc_lem.utilities.brand_account"
_FLAGS = "cqc_lem.utilities.flags"
_REDIS = "cqc_lem.utilities.linkedin.rate_limit"


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_request_state():
    """Reset the public-route cache/rate-limit state between tests."""
    main._request_session_cookie.set(None)
    yield


class _FakeCursor:
    """Minimal MySQL-ish cursor that only answers the queries this endpoint issues."""

    def __init__(self, posts=None, stats=None):
        self.posts = posts or []
        self.stats = stats or {}
        self._result: list[Any] = []

    def execute(self, sql: str, params: Optional[tuple] = None):
        s = " ".join(sql.split())
        params = params or ()
        if "SELECT id, content, scheduled_time, post_type, status FROM posts" in s:
            # get_posted_posts filters by user_id and status in SQL.
            self._result = [
                dict(p) for p in self.posts
                if p["status"] == PostStatus.POSTED.value and p.get("user_id") == params[0]
            ]
        elif "SELECT reactions, comments, reposts, impressions, saves, captured_at FROM post_stats" in s:
            self._result = [dict(self.stats.get((params[0], params[1]), {}))]
        elif "SELECT post_url FROM logs" in s:
            url = self.stats.get(f"url:{params[1]}")
            # get_post_url_from_log_for_user uses `db_cursor()` (tuple cursor) and reads row[0].
            self._result = [(url,)] if url is not None else []
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


class _FakeConn:
    def __init__(self, posts=None, stats=None):
        self._cursor = _FakeCursor(posts, stats)

    def cursor(self, *a, **k):
        return self._cursor

    def close(self):
        pass


def _make_post(post_id: int, user_id: int, content: str, status: str = "posted",
               post_type: str = "text", scheduled_time: Optional[datetime] = None):
    return {
        "id": post_id,
        "user_id": user_id,
        "content": content,
        "status": status,
        "post_type": post_type,
        "scheduled_time": scheduled_time or datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    }


def _get_showcase(client, *, enabled: bool = True, brand_user_id: int = 1,
                  posts=None, stats=None):
    posts = posts or []
    with patch(f"{_FLAGS}.flag_enabled", return_value=enabled), \
         patch(f"{_BRAND}.brand_user_id", return_value=brand_user_id), \
         patch("cqc_lem.platform.db.connection.get_db_connection",
               side_effect=lambda *a, **k: _FakeConn(posts, stats)), \
         patch("cqc_lem.utilities.observability.posthog"):
        return client.get("/api/brand-showcase")


class TestBrandShowcasePublicContract:
    def test_unauthenticated_request_returns_200_with_posts(self, client):
        posts = [_make_post(1, 1, "Hello from LEM")]
        stats = {(1, 1): {"reactions": 42, "comments": 7, "reposts": 3, "impressions": 1200, "saves": 1}}
        response = _get_showcase(client, posts=posts, stats=stats)

        assert response.status_code == 200
        detail = response.json()["detail"]
        assert len(detail["posts"]) == 1
        assert detail["posts"][0]["id"] == 1
        assert detail["posts"][0]["reactions"] == 42

    def test_returns_empty_list_when_flag_is_off(self, client):
        response = _get_showcase(client, enabled=False)

        assert response.status_code == 200
        assert response.json()["detail"]["posts"] == []

    def test_returns_empty_list_when_brand_has_no_posts(self, client):
        response = _get_showcase(client, posts=[])

        assert response.status_code == 200
        assert response.json()["detail"]["posts"] == []

    def test_returns_only_posted_posts(self, client):
        posts = [
            _make_post(1, 1, "Published"),
            _make_post(2, 1, "Draft", status=PostStatus.PENDING.value),
            _make_post(3, 1, "Errored", status=PostStatus.ERROR.value),
        ]
        stats = {(1, 1): {"reactions": 10}}
        response = _get_showcase(client, posts=posts, stats=stats)

        detail = response.json()["detail"]
        assert [p["id"] for p in detail["posts"]] == [1]

    def test_never_returns_another_users_rows(self, client):
        posts = [
            _make_post(1, 1, "Brand post"),
            _make_post(2, 99, "Someone else's post"),
        ]
        stats = {(1, 1): {"reactions": 5}}
        response = _get_showcase(client, posts=posts, stats=stats)

        detail = response.json()["detail"]
        # get_posted_posts filters by user_id in SQL, so the second post is already gone.
        assert [p["id"] for p in detail["posts"]] == [1]

    def test_passes_through_null_stats_without_fabrication(self, client):
        posts = [_make_post(1, 1, "No stats yet")]
        response = _get_showcase(client, posts=posts, stats={})

        post = response.json()["detail"]["posts"][0]
        assert post["reactions"] is None
        assert post["comments"] is None
        assert post["impressions"] is None


class TestBrandShowcaseCuration:
    def test_limits_to_six_most_recent_posts(self, client):
        posts = [_make_post(i, 1, f"Post {i}") for i in range(1, 9)]
        stats = {(1, i): {"reactions": i} for i in range(1, 9)}
        response = _get_showcase(client, posts=posts, stats=stats)

        detail = response.json()["detail"]
        assert len(detail["posts"]) == 6
        # get_posted_posts orders oldest first; the endpoint keeps the last six and reverses to newest first.
        assert [p["id"] for p in detail["posts"]] == [8, 7, 6, 5, 4, 3]

    def test_includes_post_url_from_successful_post_log(self, client):
        posts = [_make_post(1, 1, "Linked")]
        stats = {
            (1, 1): {"reactions": 1},
            "url:1": "https://www.linkedin.com/posts/lem_activity-123",
        }
        response = _get_showcase(client, posts=posts, stats=stats)

        post = response.json()["detail"]["posts"][0]
        assert post["post_url"] == "https://www.linkedin.com/posts/lem_activity-123"


class TestBrandShowcaseRateLimitAndCache:
    def test_redis_cache_is_used_when_available(self, client):
        cached = [{"id": 99, "content": "cached", "post_type": "text", "published_at": None,
                   "post_url": None, "reactions": 1, "comments": None, "reposts": None,
                   "impressions": None, "saves": None}]
        redis = MagicMock()
        redis.get.side_effect = lambda key: (
            json.dumps(cached).encode() if key == main._brand_showcase_cache_key() else None
        )

        with patch(f"{_FLAGS}.flag_enabled", return_value=True), \
             patch(f"{_REDIS}.shared_redis_client", return_value=redis), \
             patch("cqc_lem.platform.db.connection.get_db_connection") as db_conn, \
             patch("cqc_lem.utilities.observability.posthog"):
            response = client.get("/api/brand-showcase")

        assert response.status_code == 200
        assert response.json()["detail"]["posts"] == cached
        db_conn.assert_not_called()

    def test_rate_limit_allows_normal_load(self, client):
        redis = MagicMock()
        redis.get.return_value = None
        posts = [_make_post(1, 1, "Post")]
        stats = {(1, 1): {"reactions": 1}}

        with patch(f"{_FLAGS}.flag_enabled", return_value=True), \
             patch(f"{_REDIS}.shared_redis_client", return_value=redis), \
             patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=lambda *a, **k: _FakeConn(posts, stats)), \
             patch("cqc_lem.utilities.observability.posthog"):
            response = client.get("/api/brand-showcase")

        assert response.status_code == 200
        assert redis.pipeline.called


class TestBrandShowcaseFailureModes:
    def test_db_fault_returns_503_not_500(self, client):
        with patch(f"{_FLAGS}.flag_enabled", return_value=True), \
             patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=RuntimeError("db down")), \
             patch("cqc_lem.utilities.observability.posthog"):
            response = client.get("/api/brand-showcase")

        assert response.status_code == 503
