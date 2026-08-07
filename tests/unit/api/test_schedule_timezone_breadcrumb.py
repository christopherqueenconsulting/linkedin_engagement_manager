"""Issue #774 — a scheduling write that arrives WITHOUT a UTC offset must leave a breadcrumb.

`db.to_naive_utc` treats a naive datetime as already-UTC, which is the contract
(docs/timezone-contract.md) but also the silent failure mode: a wall clock the user picked in their
own zone is stored verbatim and the post fires offset-hours early — post 33 was scheduled for 9am
ET and published at 5am ET, and the only evidence was the wrong publish time. The value is still
interpreted as UTC (legacy callers depend on it); we just refuse to do it silently.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_MAIN = "cqc_lem.api.main"

from tests.unit.api.conftest import SESSION_TOKEN  # noqa: E402

_NAIVE = "2026-07-28T09:00:00"
_AWARE = "2026-07-28T13:00:00Z"

_POST_BODY = {
    "session_token": SESSION_TOKEN,
    "content": "Test content",
    "video_url": None,
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


def _warned(mock_warn) -> bool:
    return any("Naive scheduled_datetime" in str(call.args[0]) for call in mock_warn.call_args_list)


class TestWarnIfNaiveSchedule:
    def test_helper_is_quiet_for_an_aware_datetime(self):
        from datetime import datetime, timezone

        from cqc_lem.api.main import _warn_if_naive_schedule
        with patch(f"{_MAIN}.log_warning") as warn:
            _warn_if_naive_schedule(datetime(2026, 7, 28, 13, tzinfo=timezone.utc), "/x/")
        assert not _warned(warn)

    def test_helper_is_quiet_for_none(self):
        from cqc_lem.api.main import _warn_if_naive_schedule
        with patch(f"{_MAIN}.log_warning") as warn:
            _warn_if_naive_schedule(None, "/x/")
        assert not _warned(warn)

    def test_helper_warns_for_a_naive_datetime(self):
        from datetime import datetime

        from cqc_lem.api.main import _warn_if_naive_schedule
        with patch(f"{_MAIN}.log_warning") as warn:
            _warn_if_naive_schedule(datetime(2026, 7, 28, 9), "/x/", user_id=7)
        assert _warned(warn)
        assert warn.call_args.kwargs["user_id"] == 7


class TestSchedulePostEndpoint:
    def test_warns_on_a_naive_scheduled_datetime(self, client, signed_in):
        with patch(f"{_MAIN}.insert_post", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/schedule_post/", json={**_POST_BODY, "scheduled_datetime": _NAIVE})
        assert resp.status_code == 200
        assert _warned(warn)

    def test_quiet_on_an_explicit_utc_scheduled_datetime(self, client, signed_in):
        with patch(f"{_MAIN}.insert_post", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/schedule_post/", json={**_POST_BODY, "scheduled_datetime": _AWARE})
        assert resp.status_code == 200
        assert not _warned(warn)


class TestUpdatePostEndpoint:
    def test_warns_on_a_naive_scheduled_datetime(self, client, signed_in):
        with patch(f"{_MAIN}.update_db_post", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/update_post/?post_id=33",
                               json={**_POST_BODY, "scheduled_datetime": _NAIVE})
        assert resp.status_code == 200
        assert _warned(warn)

    def test_quiet_on_an_explicit_utc_scheduled_datetime(self, client, signed_in):
        with patch(f"{_MAIN}.update_db_post", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/update_post/?post_id=33",
                               json={**_POST_BODY, "scheduled_datetime": _AWARE})
        assert resp.status_code == 200
        assert not _warned(warn)


class TestBulkUpdateEndpoint:
    def test_warns_on_a_naive_scheduled_datetime(self, client, signed_in):
        with patch(f"{_MAIN}.bulk_update_posts", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/posts/bulk_update/",
                               json={"session_token": SESSION_TOKEN, "post_ids": [33], "scheduled_datetime": _NAIVE})
        assert resp.status_code == 200
        assert _warned(warn)

    def test_quiet_when_no_scheduled_datetime_is_sent(self, client, signed_in):
        with patch(f"{_MAIN}.bulk_update_posts", return_value=True), \
             patch(f"{_MAIN}.log_warning") as warn:
            resp = client.post("/api/posts/bulk_update/",
                               json={"session_token": SESSION_TOKEN, "post_ids": [33], "status": "approved"})
        assert resp.status_code == 200
        assert not _warned(warn)
