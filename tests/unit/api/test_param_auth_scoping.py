"""Issue #914 — no `/api` route derives the acting user from a request parameter.

Every endpoint below used to authenticate on an `email` or `user_id` query/body parameter, behind
nothing but the shared bearer token the SPA shipped in its build (`VITE_API_TOKEN`) — which is to say
behind nothing at all, because anyone who loaded the page held it. (#950 retired that token from the
bundle; `test_api_token_gate.py` covers the gate's contract.) This module is the standing proof
that the parameter is now a TARGET:

* **401** — a request with a valid bearer token and no session reaches nothing.
* **403** — a valid session plus ANOTHER account's `email` / `user_id` / `post_id` reaches nothing,
  and the assertion is not just the status: the db call behind it must never have happened.

The second half is the one worth keeping honest. A handler that 403s AFTER reading the row has
already leaked it into a log line and a latency signal, so each case asserts the mock was not
called.
"""

from unittest.mock import patch

import pytest

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
    task itself.
    """
    return bool(mock.called or mock.apply_async.called)


class TestOwnTargetStillWorks:
    """The rule is "not another account", not "no parameter" — a legacy client that names its OWN
    address keeps working, or this lands as an outage rather than a fix.
    """

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

    def test_new_email_is_refused_out_loud_not_ignored(self, client, signed_in):
        """400 and a pointer, never a silent 200 — a client that believes it moved the address
        while the account still answers to the old one is its own failure mode.
        """
        with patch(f"{_M}.update_user", return_value=True) as upd:
            resp = client.put("/api/user/", json={"session_token": SESSION_TOKEN,
                                                  "new_email": "attacker@evil.example",
                                                  "blog_url": "https://blog.example.com"})
        assert resp.status_code == 400
        assert "/user/email/change/init" in resp.json()["detail"]
        upd.assert_not_called()

    def test_settings_without_new_email_still_save(self, client, signed_in):
        with patch(f"{_M}.update_user", return_value=True) as upd:
            resp = client.put("/api/user/", json={"session_token": SESSION_TOKEN,
                                                  "blog_url": "https://blog.example.com"})
        assert resp.status_code == 200
        assert "email" not in upd.call_args.kwargs
        assert upd.call_args[0][0] == SESSION_USER_ID


class TestDenialsAreVisible:
    """A 403 here is a broken client or somebody working the hole #914 closed. Neither is an
    expected no-op, so both warn — and the recurrence escalation turning a repeat into a filed
    defect is the point. A 401 is the opposite: sessions expire in the ordinary course of things
    and the SPA polls, so warning on one would file a defect for working behaviour.
    """

    def test_a_foreign_target_warns(self):
        from fastapi import HTTPException

        from cqc_lem.api.main import _reject_foreign_user_id

        with patch(f"{_M}.log_warning") as warn, \
             patch(f"{_M}.record_auth_event", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _reject_foreign_user_id(SESSION_USER_ID, _OTHER_USER_ID)
        assert exc.value.status_code == 403
        warn.assert_called_once()
        assert warn.call_args.kwargs["user_id"] == SESSION_USER_ID

    def test_an_expired_session_does_not_warn(self):
        from fastapi import HTTPException

        from cqc_lem.api.main import require_session_user_id

        with patch(f"{_M}.get_session_user_id", return_value=None), \
             patch(f"{_M}.log_warning") as warn, patch(f"{_M}.log_debug") as dbg:
            with pytest.raises(HTTPException) as exc:
                require_session_user_id("stale")
        assert exc.value.status_code == 401
        warn.assert_not_called()
        dbg.assert_called_once()

    def test_an_unparseable_target_fails_closed_instead_of_500ing(self):
        """FastAPI coerces today's query parameters; the helper has to hold on its own the day it
        is reused from a body model.
        """
        from fastapi import HTTPException

        from cqc_lem.api.main import _reject_foreign_user_id

        with patch(f"{_M}.log_warning"), patch(f"{_M}.record_auth_event", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _reject_foreign_user_id(SESSION_USER_ID, "not-a-number")
        assert exc.value.status_code == 403

    def test_the_caller_naming_their_own_id_is_silent(self):
        from cqc_lem.api.main import _reject_foreign_user_id

        with patch(f"{_M}.log_warning") as warn:
            _reject_foreign_user_id(SESSION_USER_ID, SESSION_USER_ID)
        warn.assert_not_called()


class TestADeniedTargetIsAudited:
    """A log line is greppable; an audit row is queryable per account. "Has this user been naming
    other people's accounts?" is a question asked about ONE user after the fact, which is the same
    reason `_scope_checked` writes one for a misused extension token.
    """

    def test_the_row_names_the_kind_of_identifier_and_the_path_only(self, client, no_session):
        from cqc_lem.api.main import AuthAuditEvent

        with patch(f"{_M}.get_session_user_id", return_value=SESSION_USER_ID), \
             patch(f"{_M}.get_user_email", return_value=SESSION_EMAIL), \
             patch(f"{_M}.get_recent_logs"), \
             patch(f"{_M}.record_auth_event", return_value=True) as recorded:
            resp = client.get("/api/activity/", params={"session_token": SESSION_TOKEN,
                                                        "email": _OTHER_EMAIL})

        assert resp.status_code == 403
        recorded.assert_called_once()
        assert recorded.call_args[0][0] == AuthAuditEvent.FOREIGN_TARGET_DENIED
        kwargs = recorded.call_args.kwargs
        assert kwargs["user_id"] == SESSION_USER_ID
        assert kwargs["success"] is False
        assert kwargs["details"] == {"target": "email", "path": "/activity"}
        # The caller-supplied address is somebody else's and never lands in the audit trail.
        assert _OTHER_EMAIL not in str(recorded.call_args)

    def test_a_failed_audit_write_does_not_turn_the_refusal_into_a_500(self, client, no_session):
        """The refusal IS the control; the row is the record of it."""
        with patch(f"{_M}.get_session_user_id", return_value=SESSION_USER_ID), \
             patch(f"{_M}.get_user_email", return_value=SESSION_EMAIL), \
             patch(f"{_M}.get_recent_logs"), \
             patch(f"{_M}.record_auth_event", side_effect=RuntimeError("db down")):
            resp = client.get("/api/activity/", params={"session_token": SESSION_TOKEN,
                                                        "email": _OTHER_EMAIL})
        assert resp.status_code == 403


class TestAnOutageIsNotAPermissionError:
    """`user_owns_posts` fails closed either way — but "you may not touch these posts" and "we
    could not find out" are different facts. Answering 403 for a database fault tells a user they
    lack permission to their own drafts, sends on-call hunting an authorisation bug, and files a
    security-shaped defect through `_deny`'s recurrence escalation.
    """

    @pytest.mark.parametrize("method,path,params,body,db_call", _POST_ID_CASES,
                             ids=_ids(_POST_ID_CASES))
    def test_an_unprovable_check_is_503_and_touches_nothing(self, client, signed_in, method, path,
                                                            params, body, db_call):
        from cqc_lem.utilities.db import OwnershipUnprovable

        with patch(f"{_M}.user_owns_posts", side_effect=OwnershipUnprovable("db down")), \
             patch(f"{_M}.{db_call}") as touched:
            resp = _call(client, method, path, params, body, token=SESSION_TOKEN)
        assert resp.status_code == 503, f"{method} {path} answered {resp.status_code}"
        assert not _touched(touched)

    def test_an_outage_does_not_warn_as_a_denial(self):
        """`_deny` escalates a repeat into a filed GitHub issue — a database blip must not be the
        thing that files it.
        """
        from fastapi import HTTPException

        from cqc_lem.api.main import _require_own_posts
        from cqc_lem.utilities.db import OwnershipUnprovable

        with patch(f"{_M}.user_owns_posts", side_effect=OwnershipUnprovable("db down")), \
             patch(f"{_M}.log_warning") as warn, \
             patch(f"{_M}.record_auth_event") as recorded:
            with pytest.raises(HTTPException) as exc:
                _require_own_posts(SESSION_USER_ID, [1])

        assert exc.value.status_code == 503
        warn.assert_not_called()
        recorded.assert_not_called()


class TestACredentialNeverRendersInAModelRepr:
    """`/update_post/` carried `myprint(f"Received Post Request: {post}")` and gained a
    `session_token` field in the same change. Deleting that line fixed the instance; `repr=False`
    is what makes the next such line harmless.
    """

    def test_session_token_is_absent_from_the_model_repr(self):
        from datetime import datetime, timezone

        from cqc_lem.api.main import BulkDeleteRequest, BulkUpdateRequest, PostRequest, UserSettingsRequest

        models = [
            PostRequest(session_token="live-secret", content="hi",
                        scheduled_datetime=datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)),
            BulkUpdateRequest(session_token="live-secret", post_ids=[1]),
            BulkDeleteRequest(session_token="live-secret", post_ids=[1]),
            UserSettingsRequest(session_token="live-secret"),
        ]
        for model in models:
            assert "live-secret" not in repr(model), type(model).__name__
            assert "live-secret" not in f"{model}", type(model).__name__
            # Still readable by the handler — this is a repr rule, not a parsing one.
            assert model.session_token == "live-secret"


class TestTheAuthorisedIdIsTheOnlyOneThatReachesATask:
    """Two names for one account is how these routes got here. The parameter is authorised and then
    never used again — `caller_id` is the only thing that reaches Celery.
    """

    def test_weekly_content_runs_as_the_caller_not_the_parameter(self, client, signed_in):
        with patch(f"{_M}.mark_queued") as queued, \
             patch(f"{_M}.celery_chain") as chain, \
             patch(f"{_M}.plan_content_for_user") as plan, \
             patch(f"{_M}.auto_create_weekly_content") as fill:
            resp = client.post("/api/create_weekly_content/",
                               params={"session_token": SESSION_TOKEN,
                                       "user_id": SESSION_USER_ID})

        assert resp.status_code == 200
        queued.assert_called_once_with(SESSION_USER_ID)
        plan.si.assert_called_once_with(user_id=SESSION_USER_ID)
        fill.si.assert_called_once_with(user_id=SESSION_USER_ID)
        chain.assert_called_once()

    def test_aws_profile_test_runs_as_the_caller(self, client, signed_in):
        with patch(f"{_M}.test_get_my_profile") as task:
            resp = client.post("/api/aws_test_get_my_profile/",
                               params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert task.apply_async.call_args.kwargs["kwargs"] == {"user_id": SESSION_USER_ID}


class TestAResolvedSessionWithNoAddressIsOurFault:
    """`schedule_post` answered 403 "User not found" for a session that RESOLVED — which tells the
    caller they lack permission to their own account while hiding a data fault.
    """

    def test_it_is_a_500_not_a_403(self, client, no_session):
        with patch(f"{_M}.get_session_user_id", return_value=SESSION_USER_ID), \
             patch(f"{_M}.get_user_email", return_value=None), \
             patch(f"{_M}.log_error") as err, \
             patch(f"{_M}.insert_post") as inserted:
            resp = client.post("/api/schedule_post/",
                               json={"session_token": SESSION_TOKEN, "content": "hi",
                                     "scheduled_datetime": "2026-07-10T15:00:00Z"})
        assert resp.status_code == 500
        inserted.assert_not_called()
        err.assert_called_once()


class TestNoHandlerNamesAnAccountItDoesNotUse:
    """`/auth/linkedin/` took an `email` it never read — the user comes from the `session_token`
    carried in the OAuth `state`. Dead, but it was the last signature in the file naming an account
    as a parameter, and it put the address in a URL (history, `Referer`) for nothing.
    """

    def test_linkedin_auth_init_takes_no_email(self):
        import inspect

        from cqc_lem.api.main import linkedin_auth_init

        assert "email" not in inspect.signature(linkedin_auth_init).parameters

    def test_a_stray_email_parameter_changes_nothing(self, client):
        """A cached page still linking with `?email=` must redirect exactly as before, not 422."""
        with patch(f"{_M}.AuthClient") as auth:
            auth.return_value.generate_member_auth_url.return_value = "https://linkedin.example/oauth"
            resp = client.get("/api/auth/linkedin/",
                              params={"session_token": SESSION_TOKEN, "email": _OTHER_EMAIL},
                              follow_redirects=False)
        assert resp.status_code == 307
        state = auth.return_value.generate_member_auth_url.call_args.kwargs["state"]
        assert state.endswith(SESSION_TOKEN)
        assert _OTHER_EMAIL not in state
