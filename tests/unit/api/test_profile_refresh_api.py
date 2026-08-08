"""`POST /api/user/linkedin-profile/refresh` — the on-demand profile re-scrape (issue #1076).

What is worth pinning: an unauthenticated caller gets nothing, the claim is taken BEFORE the task
is dispatched (so a double-click costs one Chrome session and not two), the task is queued with
`force_refresh=True` (without it the worker reads the cache back and the button does nothing), and
an `agent`-scoped token can never reach the path at all.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_PATH = "/api/user/linkedin-profile/refresh"
_GET = "/api/user/linkedin-profile"
_SESSION = "tok_test"
_USER_ID = 42


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


@pytest.fixture
def queued():
    """A signed-in caller whose claim is granted, with the Celery dispatch captured."""
    from cqc_lem.utilities.profile_refresh import REASON_QUEUED, RefreshClaim
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
         patch("cqc_lem.api.main.claim_profile_refresh",
               return_value=RefreshClaim(queued=True, reason=REASON_QUEUED)) as claim, \
         patch("cqc_lem.api.main.update_stale_profile") as task:
        yield claim, task


class TestAuth:
    def test_no_session_is_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.update_stale_profile") as task:
            resp = client.post(_PATH, json={"session_token": "bad"})
        assert resp.status_code == 401
        task.apply_async.assert_not_called()

    def test_the_caller_is_never_named_by_the_body(self, client, queued):
        """There is no `user_id` parameter to authorise — the task always runs as the session."""
        claim, task = queued
        resp = client.post(_PATH, json={"session_token": _SESSION, "user_id": 999})
        assert resp.status_code == 202
        claim.assert_called_once_with(_USER_ID)
        assert task.apply_async.call_args.kwargs["kwargs"]["user_id"] == _USER_ID


class TestQueueing:
    def test_a_granted_claim_queues_a_forced_rescrape(self, client, queued):
        _claim, task = queued
        resp = client.post(_PATH, json={"session_token": _SESSION})
        assert resp.status_code == 202
        assert resp.json()["detail"] == {
            "queued": True, "reason": "queued", "retry_after_seconds": 0,
        }
        # Without force_refresh the worker reads a profile cached within the day straight back and
        # the button silently does nothing — which is the whole defect this issue exists to fix.
        assert task.apply_async.call_args.kwargs["kwargs"] == {
            "user_id": _USER_ID, "force_refresh": True,
        }

    def test_a_spent_window_still_answers_202_and_queues_nothing(self, client):
        """A second press the same day is an expected no-op, not an error.

        429 would render in the SPA as a failure, for something the user did on purpose.
        """
        from cqc_lem.utilities.profile_refresh import REASON_ALREADY_REFRESHED_TODAY, RefreshClaim
        spent = RefreshClaim(queued=False, reason=REASON_ALREADY_REFRESHED_TODAY,
                             retry_after_seconds=3600)
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.claim_profile_refresh", return_value=spent), \
             patch("cqc_lem.api.main.update_stale_profile") as task:
            resp = client.post(_PATH, json={"session_token": _SESSION})
        assert resp.status_code == 202
        assert resp.json()["detail"] == {
            "queued": False,
            "reason": "already_refreshed_today",
            "retry_after_seconds": 3600,
        }
        task.apply_async.assert_not_called()

    def test_the_claim_is_taken_before_the_task_is_dispatched(self, client):
        """Ordering IS the dedupe.

        Claiming after dispatch would let a double-click spend two Selenium slots before either
        claim was recorded.
        """
        from cqc_lem.utilities.profile_refresh import REASON_QUEUED, RefreshClaim
        order = []
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.claim_profile_refresh",
                   side_effect=lambda uid: (order.append("claim"),
                                            RefreshClaim(queued=True, reason=REASON_QUEUED))[1]), \
             patch("cqc_lem.api.main.update_stale_profile") as task:
            task.apply_async.side_effect = lambda *a, **k: order.append("dispatch")
            client.post(_PATH, json={"session_token": _SESSION})
        assert order == ["claim", "dispatch"]


class TestProfileGetReportsTheWindow:
    def test_the_get_peeks_the_window_so_a_reload_keeps_the_button_disabled(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.get_linkedin_profile_url_by_user_id",
                   return_value="https://www.linkedin.com/in/someone/"), \
             patch("cqc_lem.api.main.refresh_claimed_seconds", return_value=7200) as peek:
            resp = client.get(f"{_GET}?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["refresh_available_in_seconds"] == 7200
        peek.assert_called_once_with(_USER_ID)


class TestAgentScope:
    def test_an_agent_session_cannot_spend_a_selenium_slot(self):
        """The path is absent from `_AGENT_SESSION_SURFACE`, so `_scope_allows` refuses it.

        Same posture as `agent_may_not_configure`: a headless token may QUEUE approval-gated work,
        never draw on the fixed Chrome session pool the engagement lanes are sized against.
        """
        from cqc_lem.api import main
        key = main._scope_path(_PATH)
        assert main._scope_allows(main.SESSION_SCOPE_AGENT, key) is False
        assert key not in main._AGENT_SESSION_SURFACE
