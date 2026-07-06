"""Unit tests for the /api/admin/test/* engagement test-run endpoints.

Inputs are typed query params; auth requires BOTH a bearer token and the
X-Admin-Secret header (the bearer check is a no-op in tests because
API_ACCESS_TOKENS is unset, so only the admin secret is exercised here).
"""

import pytest
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
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


_SECRET = "s3cret"
_ADMIN_HEADER = {"x-admin-secret": _SECRET}


def _async_result(task_id="task-123"):
    m = MagicMock()
    m.id = task_id
    return m


class TestComment:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/comment", params={"user_id": 1})
        assert r.status_code == 403

    def test_queues_task(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.automate_commenting.apply_async", return_value=_async_result()) as t:
            r = client.post("/api/admin/test/comment", params={"user_id": 7}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["task_id"] == "task-123"
        assert t.call_args.kwargs["kwargs"]["user_id"] == 7

    def test_missing_required_user_id_is_422(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/comment", headers=_ADMIN_HEADER)
        assert r.status_code == 422


class TestReply:
    def test_404_when_post_user_missing(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=None):
            r = client.post("/api/admin/test/reply", params={"post_id": 99}, headers=_ADMIN_HEADER)
        assert r.status_code == 404

    def test_queues_task(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=5), \
             patch("cqc_lem.api.main.automate_reply_commenting.apply_async", return_value=_async_result()) as t:
            r = client.post("/api/admin/test/reply", params={"post_id": 12}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["post_id"] == 12
        assert r.json()["detail"]["user_id"] == 5


class TestDM:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/dm", params={"user_id": 1})
        assert r.status_code == 403

    def test_queues_appreciation_dm(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.automate_appreciation_dms_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/test/dm", params={"user_id": 3}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["user_id"] == 3


class TestDirectDM:
    def test_queues_direct_dm(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.send_private_dm.apply_async", return_value=_async_result()) as t:
            r = client.post("/api/admin/test/dm-direct",
                            params={"user_id": 1, "profile_url": "https://linkedin.com/in/x",
                                    "message": "Hi there"},
                            headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["profile_url"] == "https://linkedin.com/in/x"
        assert t.call_args.kwargs["kwargs"]["message"] == "Hi there"

    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/dm-direct",
                            params={"user_id": 1, "profile_url": "u", "message": "m"})
        assert r.status_code == 403


class TestTaskStatus:
    def test_returns_state(self, client):
        res = MagicMock()
        res.state = "SUCCESS"
        res.ready.return_value = True
        res.result = "done"
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=res):
            r = client.get("/api/admin/task-status/task-123", headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["state"] == "SUCCESS"
        assert r.json()["detail"]["result"] == "done"

    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.get("/api/admin/task-status/task-123")
        assert r.status_code == 403


class TestConsolidateDuplicateComments:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/consolidate-duplicate-comments", params={"user_id": 1})
        assert r.status_code == 403

    def test_defaults_to_dry_run(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.consolidate_duplicate_comments_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/consolidate-duplicate-comments",
                            params={"user_id": 3}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["dry_run"] is True
        assert t.call_args.kwargs["kwargs"] == {"user_id": 3, "dry_run": True, "hours": 168}
        assert t.call_args.kwargs["queue"] == "se_engage"

    def test_can_request_real_delete(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.main.consolidate_duplicate_comments_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/consolidate-duplicate-comments",
                            params={"user_id": 3, "dry_run": False, "hours": 48}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"] == {"user_id": 3, "dry_run": False, "hours": 48}


class TestAuthSchemeInOpenAPI:
    """Both security schemes must be advertised so /docs shows both credentials."""
    def test_both_schemes_present(self, client):
        schema = client.get("/openapi.json").json()
        schemes = schema.get("components", {}).get("securitySchemes", {})
        names = set(schemes.keys())
        # HTTPBearer + APIKeyHeader(name="X-Admin-Secret")
        assert any(s.get("type") == "http" and s.get("scheme") == "bearer" for s in schemes.values())
        assert any(s.get("type") == "apiKey" and s.get("name") == "X-Admin-Secret" for s in schemes.values())
        assert names  # non-empty
