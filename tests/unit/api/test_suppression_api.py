"""Unit tests for the suppression-tripwire endpoints — issue #629.

GET /api/user/automation-status is what the Account banner reads; POST /api/user/automation-resume
is the ONLY way back from a trip, so the tests pin both the auth gate and the narrow resume: it may
lift the tripwire's own pause and nothing else.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_RL = "cqc_lem.utilities.linkedin.rate_limit"
_START = datetime(2026, 7, 1, 9, 0)


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


def _rows(baseline: int, recent: int) -> list:
    def post(offset, impressions):
        return {"post_id": 100 + offset, "scheduled_time": _START + timedelta(days=offset),
                "reactions": 10, "comments": 1, "reposts": 0, "saves": 0,
                "impressions": impressions}
    return [post(i, baseline) for i in range(10)] + [post(10 + i, recent) for i in range(3)]


def _stack(es, *, user_id=42, rows=None, trip=None, pause_reason=None, pause_remaining=0):
    es.enter_context(patch(f"{_M}.get_session_user_id", return_value=user_id))
    es.enter_context(patch(f"{_M}.get_post_performance_rows",
                           return_value=list(rows if rows is not None else _rows(8500, 8400))))
    es.enter_context(patch(f"{_M}.get_comment_outcomes", return_value=[]))
    es.enter_context(patch(f"{_RL}.suppression_trip_state", return_value=trip))
    es.enter_context(patch(f"{_RL}.automation_pause_remaining", return_value=pause_remaining))
    es.enter_context(patch(f"{_RL}.automation_pause_reason", return_value=pause_reason))
    es.enter_context(patch(f"{_RL}.rate_limit_cooldown_remaining", return_value=0))


_TRIP = {"user_id": 42, "reason": "reach collapse", "tripped_at": "2026-07-14T09:15:00+00:00"}


class TestStatusEndpoint:
    def test_requires_a_valid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            assert client.get("/api/user/automation-status?session_token=bad").status_code == 401

    def test_healthy_account_reports_nothing_tripped(self, client):
        with ExitStack() as es:
            _stack(es)
            detail = client.get("/api/user/automation-status?session_token=t").json()["detail"]
        assert detail["tripped"] is False
        assert detail["current"]["status"] == "ok"
        assert detail["engagement_paused"] is False
        assert detail["enabled"] is True

    def test_a_standing_trip_is_surfaced_with_its_reason(self, client):
        with ExitStack() as es:
            _stack(es, rows=_rows(8500, 340), trip=_TRIP,
                   pause_reason="suppression:42", pause_remaining=1000)
            detail = client.get("/api/user/automation-status?session_token=t").json()["detail"]
        assert detail["tripped"] is True
        assert detail["trip"]["reason"] == "reach collapse"
        assert detail["current"]["tripped"] is True
        assert detail["recovered"] is False
        assert detail["pause_by_tripwire"] is True
        assert detail["pause_remaining_s"] == 1000

    def test_recovery_is_reported_while_the_trip_still_stands(self, client):
        with ExitStack() as es:
            _stack(es, rows=_rows(8500, 8400), trip=_TRIP,
                   pause_reason="suppression:42", pause_remaining=1000)
            detail = client.get("/api/user/automation-status?session_token=t").json()["detail"]
        assert detail["tripped"] is True and detail["recovered"] is True

    def test_a_foreign_pause_is_not_attributed_to_the_tripwire(self, client):
        with ExitStack() as es:
            _stack(es, pause_reason="maintenance", pause_remaining=600)
            detail = client.get("/api/user/automation-status?session_token=t").json()["detail"]
        assert detail["engagement_paused"] is True and detail["pause_by_tripwire"] is False


class TestResumeEndpoint:
    def test_requires_a_valid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            response = client.post("/api/user/automation-resume", json={"session_token": "bad"})
        assert response.status_code == 401

    def test_clears_the_trip_and_lifts_the_tripwire_pause(self, client):
        with ExitStack() as es:
            _stack(es, rows=_rows(8500, 8400), trip=_TRIP,
                   pause_reason="suppression:42", pause_remaining=1000)
            clear = es.enter_context(patch(f"{_RL}.clear_suppression_trip", return_value=True))
            resume = es.enter_context(patch(f"{_RL}.resume_automation", return_value=True))
            detail = client.post("/api/user/automation-resume",
                                 json={"session_token": "t"}).json()["detail"]
        assert clear.called and resume.called
        assert detail["cleared"] is True and detail["resumed"] is True

    def test_never_lifts_a_pause_the_tripwire_did_not_set(self, client):
        # A 429 cooldown, a maintenance window or an admin kill-switch must survive this button.
        with ExitStack() as es:
            _stack(es, trip=_TRIP, pause_reason="maintenance", pause_remaining=600)
            clear = es.enter_context(patch(f"{_RL}.clear_suppression_trip", return_value=True))
            resume = es.enter_context(patch(f"{_RL}.resume_automation", return_value=True))
            detail = client.post("/api/user/automation-resume",
                                 json={"session_token": "t"}).json()["detail"]
        assert clear.called and not resume.called
        assert detail["resumed"] is False

    def test_never_lifts_another_users_suppression_pause(self, client):
        # `pause_automation` is ONE global breaker. User 42 pressing their own re-enable button must
        # not restart engagement while user 7's trip is the thing holding it down.
        with ExitStack() as es:
            _stack(es, user_id=42, trip=_TRIP, pause_reason="suppression:7", pause_remaining=600)
            es.enter_context(patch(f"{_RL}.clear_suppression_trip", return_value=True))
            resume = es.enter_context(patch(f"{_RL}.resume_automation", return_value=True))
            detail = client.post("/api/user/automation-resume",
                                 json={"session_token": "t"}).json()["detail"]
        assert not resume.called
        assert detail["resumed"] is False

    def test_is_idempotent_when_nothing_is_paused(self, client):
        with ExitStack() as es:
            _stack(es)
            es.enter_context(patch(f"{_RL}.clear_suppression_trip", return_value=True))
            resume = es.enter_context(patch(f"{_RL}.resume_automation", return_value=True))
            detail = client.post("/api/user/automation-resume",
                                 json={"session_token": "t"}).json()["detail"]
        assert not resume.called and detail["tripped"] is False
