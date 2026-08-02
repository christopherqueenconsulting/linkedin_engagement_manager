"""Issue #914 — no `/api` route derives the acting user from a request parameter.

Every endpoint below used to authenticate on an `email` or `user_id` query/body parameter, behind
nothing but the shared bearer token the SPA ships in its build (`VITE_API_TOKEN`) — which is to say
behind nothing at all, because anyone who loads the page holds it. This module is the standing proof
that the parameter is now a TARGET:

* **401** — a request with a valid bearer token and no session reaches nothing.
* **403** — a valid session plus ANOTHER account's `email` / `user_id` / `post_id` reaches nothing,
  and the assertion is not just the status: the db call behind it must never have happened.

The second half is the one worth keeping honest. A handler that 403s AFTER reading the row has
already leaked it into a log line and a latency signal, so each case asserts the mock was not
called.
"""

import pytest
from unittest.mock import patch

from tests.unit.api.conftest import SESSION_EMAIL, SESSION_TOKEN, SESSION_USER_ID

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"

_OTHER_EMAIL = "victim@example.com"
_OTHER_USER_ID = SESSION_USER_ID + 1


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


@pytest.fixture
def no_session():
    with patch(f"{_M}.get_session_user_id", return_value=None):
        yield


# One row per converted endpoint: (method, path, query, json body, the db call it must not make).
# `email` / `user_id` in the query or body is the FOREIGN target — a bearer holder naming somebody
# else's account.
_CASES = [
    ("GET", "/api/dashboard/stats/", {"email": _OTHER_EMAIL}, None, "get_dashboard_counts"),
    ("GET", "/api/dashboard/planned-tasks/", {"email": _OTHER_EMAIL}, None, "get_planned_tasks"),
    ("GET", "/api/activity/", {"email": _OTHER_EMAIL}, None, "get_recent_logs"),
    ("GET", "/api/posts/", {"email": _OTHER_EMAIL}, None, "get_posts"),
    ("GET", "/api/post_url/", {"post_id": 9, "email": _OTHER_EMAIL}, None,
     "get_post_url_from_log_for_user"),
    ("GET", "/api/user_id/", {"email": _OTHER_EMAIL}, None, "get_user_id"),
    ("PUT", "/api/user/", None, {"email": _OTHER_EMAIL, "blog_url": "https://evil.example"},
     "update_user"),
    ("POST", "/api/schedule_post/", None,
     {"email": _OTHER_EMAIL, "content": "hi", "scheduled_datetime": "2026-07-10T15:00:00Z"},
     "insert_post"),
    ("POST", "/api/create_weekly_content/", {"user_id": _OTHER_USER_ID}, None, "mark_queued"),
    ("POST", "/api/invite_to_li_company_page/", {"user_id": _OTHER_USER_ID}, None,
     "automate_invites_to_company_page_for_user"),
    ("POST", "/api/aws_test_get_my_profile/", {"user_id": _OTHER_USER_ID}, None,
     "test_get_my_profile"),
]

# The post-mutating routes name their target by id rather than by account, so they get their own
# table: ownership is the check, and `user_owns_posts` is what answers it.
_POST_ID_CASES = [
    ("POST", "/api/update_post/", {"post_id": 4242},
     {"content": "hi", "scheduled_datetime": "2026-07-10T15:00:00Z"}, "update_db_post"),
    ("POST", "/api/posts/bulk_update/", None,
     {"post_ids": [4242], "status": "approved"}, "bulk_update_posts"),
    ("DELETE", "/api/posts/", None, {"post_ids": [4242]}, "soft_delete_posts"),
    ("POST", "/api/automate_reply_commenting", {"post_id": 4242}, None,
     "automate_reply_commenting"),
]

_ALL_CASES = _CASES + _POST_ID_CASES


def _call(client, method, path, params, body, token=None):
    params = dict(params or {})
    body = dict(body) if body is not None else None
    if token:
        if body is not None:
            body["session_token"] = token
        else:
            params["session_token"] = token
    return client.request(method, path, params=params or None, json=body)


def _ids(cases):
    return [f"{m} {p}" for m, p, _, _, _ in cases]


class TestNoSessionIs401:
    """A valid bearer token is not an identity — every converted route needs a session."""

    @pytest.mark.parametrize("method,path,params,body,db_call", _ALL_CASES, ids=_ids(_ALL_CASES))
    def test_returns_401_and_touches_nothing(self, client, no_session, method, path, params, body,
                                             db_call):
        with patch(f"{_M}.{db_call}") as touched:
            resp = _call(client, method, path, params, body)
        assert resp.status_code == 401, f"{method} {path} answered {resp.status_code}"
        assert not _touched(touched), f"{method} {path} reached {db_call} without a session"


class TestAnotherAccountsTargetIs403:
    """A session plus somebody else's identifier reaches nothing."""

    @pytest.mark.parametrize("method,path,params,body,db_call", _CASES, ids=_ids(_CASES))
    def test_returns_403_and_touches_nothing(self, client, signed_in, method, path, params, body,
                                             db_call):
        with patch(f"{_M}.{db_call}") as touched:
            resp = _call(client, method, path, params, body, token=SESSION_TOKEN)
        assert resp.status_code == 403, f"{method} {path} answered {resp.status_code}"
        assert not _touched(touched), f"{method} {path} reached {db_call} for another account"

    @pytest.mark.parametrize("method,path,params,body,db_call", _POST_ID_CASES,
                             ids=_ids(_POST_ID_CASES))
    def test_a_foreign_post_id_is_403(self, client, signed_in, method, path, params, body,
                                      db_call):
        with patch(f"{_M}.user_owns_posts", return_value=False), \
             patch(f"{_M}.{db_call}") as touched:
            resp = _call(client, method, path, params, body, token=SESSION_TOKEN)
        assert resp.status_code == 403, f"{method} {path} answered {resp.status_code}"
        assert not _touched(touched), f"{method} {path} reached {db_call} for a foreign post"


def _touched(mock) -> bool:
    """Celery tasks are patched as objects, so the call that matters is `.apply_async`, not the
    task itself."""
    return bool(mock.called or mock.apply_async.called)


class TestOwnTargetStillWorks:
    """The rule is "not another account", not "no parameter" — a legacy client that names its OWN
    address keeps working, or this lands as an outage rather than a fix."""

    def test_own_email_is_accepted(self, client, signed_in):
        with patch(f"{_M}.get_recent_logs", return_value=[]) as logs:
            resp = client.get("/api/activity/", params={"session_token": SESSION_TOKEN,
                                                        "email": SESSION_EMAIL})
        assert resp.status_code == 200
        assert logs.call_args[0][0] == SESSION_USER_ID

    def test_own_email_matches_case_insensitively(self, client, signed_in):
        with patch(f"{_M}.get_recent_logs", return_value=[]):
            resp = client.get("/api/activity/", params={"session_token": SESSION_TOKEN,
                                                        "email": SESSION_EMAIL.upper()})
        assert resp.status_code == 200

    def test_own_user_id_is_accepted(self, client, signed_in):
        with patch(f"{_M}.automate_invites_to_company_page_for_user") as task:
            resp = client.post("/api/invite_to_li_company_page/",
                               params={"session_token": SESSION_TOKEN,
                                       "user_id": SESSION_USER_ID})
        assert resp.status_code == 200
        assert task.apply_async.call_args[1]["kwargs"] == {"user_id": SESSION_USER_ID}


class TestBulkUpdateChecksEveryId:
    """A list is only as scoped as its worst entry: one foreign id must fail the whole call."""

    def test_ownership_is_asked_for_the_whole_list(self, client, signed_in):
        with patch(f"{_M}.user_owns_posts", return_value=True) as owns, \
             patch(f"{_M}.bulk_update_posts", return_value=True):
            resp = client.post("/api/posts/bulk_update/",
                               json={"session_token": SESSION_TOKEN,
                                     "post_ids": [1, 2, 3], "status": "approved"})
        assert resp.status_code == 200
        owns.assert_called_once_with(SESSION_USER_ID, [1, 2, 3])

    def test_one_foreign_id_rejects_the_batch(self, client, signed_in):
        with patch(f"{_M}.user_owns_posts", return_value=False), \
             patch(f"{_M}.bulk_update_posts") as upd:
            resp = client.request("DELETE", "/api/posts/",
                                  json={"session_token": SESSION_TOKEN, "post_ids": [1, 4242]})
        assert resp.status_code == 403
        upd.assert_not_called()


class TestEmailChangeIsNotHere:
    """`PUT /user/` used to move the account email on the strength of knowing the current one."""

    def test_new_email_is_ignored(self, client, signed_in):
        with patch(f"{_M}.update_user", return_value=True) as upd:
            resp = client.put("/api/user/", json={"session_token": SESSION_TOKEN,
                                                  "new_email": "attacker@evil.example",
                                                  "blog_url": "https://blog.example.com"})
        assert resp.status_code == 200
        assert "email" not in upd.call_args.kwargs
        assert upd.call_args[0][0] == SESSION_USER_ID
