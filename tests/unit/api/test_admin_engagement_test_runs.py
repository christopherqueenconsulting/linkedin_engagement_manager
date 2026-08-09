"""Unit tests for the /api/admin/test/* engagement test-run endpoints.

Inputs are typed query params; auth requires BOTH a bearer token and the
X-Admin-Secret header (the bearer check is a no-op in tests because
API_ACCESS_TOKENS is unset, so only the admin secret is exercised here).
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.engagement.invites.automate_invites_to_company_page_for_user"),
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
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/comment", params={"user_id": 1})
        assert r.status_code == 403

    def test_queues_task(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.automate_commenting.apply_async", return_value=_async_result()) as t:
            r = client.post("/api/admin/test/comment", params={"user_id": 7}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["task_id"] == "task-123"
        assert t.call_args.kwargs["kwargs"]["user_id"] == 7

    def test_missing_required_user_id_is_422(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/comment", headers=_ADMIN_HEADER)
        assert r.status_code == 422


class TestReply:
    def test_404_when_post_user_missing(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.get_post_user_id", return_value=None):
            r = client.post("/api/admin/test/reply", params={"post_id": 99}, headers=_ADMIN_HEADER)
        assert r.status_code == 404

    def test_queues_task(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.get_post_user_id", return_value=5), \
             patch("cqc_lem.api.routers.admin.automate_reply_commenting.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/test/reply", params={"post_id": 12}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["post_id"] == 12
        assert r.json()["detail"]["user_id"] == 5


class TestDM:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/dm", params={"user_id": 1})
        assert r.status_code == 403

    def test_queues_appreciation_dm(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.automate_appreciation_dms_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/test/dm", params={"user_id": 3}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["user_id"] == 3


class TestDirectDM:
    def test_queues_direct_dm(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.send_private_dm.apply_async", return_value=_async_result()) as t:
            r = client.post("/api/admin/test/dm-direct",
                            params={"user_id": 1, "profile_url": "https://linkedin.com/in/x",
                                    "message": "Hi there"},
                            headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"]["profile_url"] == "https://linkedin.com/in/x"
        assert t.call_args.kwargs["kwargs"]["message"] == "Hi there"

    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/test/dm-direct",
                            params={"user_id": 1, "profile_url": "u", "message": "m"})
        assert r.status_code == 403


class TestTaskStatus:
    def test_returns_state(self, client):
        res = MagicMock()
        res.state = "SUCCESS"
        res.ready.return_value = True
        res.result = "done"
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.app.my_celery.app.AsyncResult", return_value=res):
            r = client.get("/api/admin/task-status/task-123", headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["state"] == "SUCCESS"
        assert r.json()["detail"]["result"] == "done"

    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.get("/api/admin/task-status/task-123")
        assert r.status_code == 403


class TestConsolidateDuplicateComments:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            r = client.post("/api/admin/consolidate-duplicate-comments", params={"user_id": 1})
        assert r.status_code == 403

    def test_defaults_to_dry_run(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.consolidate_duplicate_comments_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/consolidate-duplicate-comments",
                            params={"user_id": 3}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert r.json()["detail"]["dry_run"] is True
        assert t.call_args.kwargs["kwargs"] == {"user_id": 3, "dry_run": True, "hours": 168}
        assert t.call_args.kwargs["queue"] == "se_engage"

    def test_can_request_real_delete(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.consolidate_duplicate_comments_for_user.apply_async",
                   return_value=_async_result()) as t:
            r = client.post("/api/admin/consolidate-duplicate-comments",
                            params={"user_id": 3, "dry_run": False, "hours": 48}, headers=_ADMIN_HEADER)
        assert r.status_code == 200
        assert t.call_args.kwargs["kwargs"] == {"user_id": 3, "dry_run": False, "hours": 48}


class TestAdminCredentialsAreNotAdvertised:
    """The admin credentials must NOT reach the public schema (issue #1020).

    They used to: the security schemes were declared so Swagger's Authorize dialog showed both,
    and Swagger was served unauthenticated. Since every `/api/admin/*` operation is now hidden,
    nothing visible references those schemes and FastAPI stops emitting them — which is the
    outcome we want, so pin it rather than leave it to a code path nobody is checking. The
    schemes still exist in code and `_require_api_and_admin` still enforces both at runtime; the
    tests above prove that by getting 403 without the header and 200 with it.
    """

    def test_admin_schemes_absent_from_the_public_schema(self, client):
        schemes = client.get("/api/openapi.json").json().get(
            "components", {}).get("securitySchemes", {})
        assert not any(s.get("name") == "X-Admin-Secret" for s in schemes.values()), \
            "the admin header is named in a document served without a credential"

    def test_admin_secret_is_still_enforced_at_runtime(self, client):
        """Hiding is not gating: the same route that vanished from the schema must still 403."""
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET):
            assert client.post("/api/admin/automation-pause",
                               params={"user_id": 1}).status_code == 403


def _rejected_result():
    """What celery-once actually hands back on a graceful rejection.

    `once={'graceful': True}` does not raise — `QueueOnce.apply_async` returns
    `EagerResult(None, None, states.REJECTED)` (celery_once/tasks.py:104) because a run for the
    same key is already in flight. Nothing was queued.
    """
    from celery import states
    from celery.result import EagerResult
    return EagerResult(None, None, states.REJECTED)


# (route, task symbol patched on main, query params)
_QUEUE_ONCE_ROUTES = [
    ("/api/admin/test/comment", "automate_commenting", {"user_id": 7}),
    ("/api/admin/test/reply", "automate_reply_commenting", {"post_id": 12}),
    ("/api/admin/consolidate-duplicate-comments", "consolidate_duplicate_comments_for_user",
     {"user_id": 7}),
    ("/api/admin/test/dm", "automate_appreciation_dms_for_user", {"user_id": 7}),
    ("/api/admin/test/dm-direct", "send_private_dm",
     {"user_id": 7, "profile_url": "https://www.linkedin.com/in/x/", "message": "hi"}),
]


class TestAGracefulRejectionIsNotASuccess:
    """Every one of these tasks is `base=QueueOnce, once={'graceful': True}`.

    A rejection used to be reported as 200 with `"task_id": null` — telling the operator a run had
    started and handing them an id `/admin/task-status/{task_id}` can never resolve, which is the
    one thing these endpoints exist to let you watch.
    """

    @pytest.mark.parametrize("route,task,params", _QUEUE_ONCE_ROUTES)
    def test_already_running_answers_409(self, client, route, task, params):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.get_post_user_id", return_value=5), \
             patch(f"cqc_lem.api.routers.admin.{task}.apply_async", return_value=_rejected_result()):
            r = client.post(route, params=params, headers=_ADMIN_HEADER)
        assert r.status_code == 409, route
        assert "already queued or in progress" in r.json()["detail"]

    @pytest.mark.parametrize("route,task,params", _QUEUE_ONCE_ROUTES)
    def test_a_real_dispatch_still_answers_200_with_its_id(self, client, route, task, params):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.get_post_user_id", return_value=5), \
             patch(f"cqc_lem.api.routers.admin.{task}.apply_async", return_value=_async_result()):
            r = client.post(route, params=params, headers=_ADMIN_HEADER)
        assert r.status_code == 200, route
        assert r.json()["detail"]["task_id"] == "task-123"
        assert r.json()["detail"]["task"] == task

    @pytest.mark.parametrize("route,task,params", _QUEUE_ONCE_ROUTES)
    def test_no_route_can_ever_publish_a_null_task_id(self, client, route, task, params):
        """The property that matters, independent of how the rejection is spelled."""
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", _SECRET), \
             patch("cqc_lem.api.routers.admin.get_post_user_id", return_value=5), \
             patch(f"cqc_lem.api.routers.admin.{task}.apply_async", return_value=_rejected_result()):
            r = client.post(route, params=params, headers=_ADMIN_HEADER)
        body = r.json()
        assert not (isinstance(body.get("detail"), dict)
                    and body["detail"].get("task_id") is None), route
