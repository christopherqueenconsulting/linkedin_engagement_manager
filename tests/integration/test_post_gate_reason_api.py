"""Integration tests for the pending-reason API surface (issue #421): the posts list carries the
gate verdict the review UI renders, and /user/post/rescore re-runs the gates on the saved content
(promoting a fixed draft PENDING -> APPROVED) while refusing posts that aren't the caller's.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_API = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"

_FINDING = {"gate": "authenticity", "label": "Authenticity", "score": 41, "threshold": 60,
            "demoted": True, "explanation": "scored 41 of 100", "remediation": "add specifics",
            "details": ["no concrete detail"]}


_SESSION_TOKEN = "gate-reason-session"


def _client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    return TestClient(app)


@contextmanager
def _signed_in(user_id: int = 1):
    """The posts list resolves its caller from the session since issue #914."""
    with patch(f"{_API}.get_session_user_id", return_value=user_id):
        yield


class TestPostsListCarriesTheVerdict:
    def _post_row(self, gate_reason):
        return {
            "id": 42, "content": "draft", "video_url": None,
            "scheduled_time": "2026-08-01T12:00:00", "post_type": "text", "status": "pending",
            "carousel_slides": None, "authenticity_score": 41, "gate_reason": gate_reason,
        }

    def test_gate_reason_and_score_are_returned(self):
        with _signed_in(), patch(f"{_API}.get_posts",
                                 return_value=([self._post_row(json.dumps([_FINDING]))], 1)):
            r = _client().get(f"/api/posts/?session_token={_SESSION_TOKEN}")
        assert r.status_code == 200
        post = r.json()["detail"]["posts"][0]
        assert post["authenticity_score"] == 41
        assert post["gate_reason"] == [_FINDING]

    def test_a_post_with_no_verdict_returns_an_empty_list(self):
        with _signed_in(), patch(f"{_API}.get_posts", return_value=([self._post_row(None)], 1)):
            r = _client().get(f"/api/posts/?session_token={_SESSION_TOKEN}")
        assert r.json()["detail"]["posts"][0]["gate_reason"] == []

    def test_a_malformed_verdict_does_not_break_the_queue(self):
        with _signed_in(), patch(f"{_API}.get_posts", return_value=([self._post_row("{oops")], 1)):
            r = _client().get(f"/api/posts/?session_token={_SESSION_TOKEN}")
        assert r.status_code == 200
        assert r.json()["detail"]["posts"][0]["gate_reason"] == []


class TestRescoreEndpoint:
    def test_promotes_a_post_that_now_passes(self):
        result = {"passed": True, "status": "approved", "authenticity_score": 88,
                  "findings": [], "detail": "Passed every quality gate — approved and queued"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_post_user_id", return_value=1), \
             patch(f"{_USER}.get_post_status", return_value="pending"), \
             patch("cqc_lem.app.run_content_plan.rescore_post", return_value=result) as rescore:
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "t", "post_id": 42})
        assert r.status_code == 200
        assert r.json()["detail"] == result
        rescore.assert_called_once_with(42)

    def test_returns_the_findings_when_it_still_fails(self):
        result = {"passed": False, "status": "pending", "authenticity_score": 41,
                  "findings": [_FINDING], "detail": "Still held for review"}
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_post_user_id", return_value=1), \
             patch(f"{_USER}.get_post_status", return_value="pending"), \
             patch("cqc_lem.app.run_content_plan.rescore_post", return_value=result):
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "t", "post_id": 42})
        detail = r.json()["detail"]
        assert detail["passed"] is False
        assert detail["findings"][0]["remediation"] == "add specifics"

    def test_401_without_a_session(self):
        with patch(f"{_API}.get_session_user_id", return_value=None):
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "bad", "post_id": 42})
        assert r.status_code == 401

    def test_404_for_someone_elses_post(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_post_user_id", return_value=2):
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "t", "post_id": 42})
        assert r.status_code == 404

    @pytest.mark.parametrize("status", ["posted", "rejected", "scheduled"])
    def test_409_outside_the_review_states(self, status):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_post_user_id", return_value=1), \
             patch(f"{_USER}.get_post_status", return_value=status):
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "t", "post_id": 42})
        assert r.status_code == 409

    def test_500_when_scoring_blows_up(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_post_user_id", return_value=1), \
             patch(f"{_USER}.get_post_status", return_value="pending"), \
             patch("cqc_lem.app.run_content_plan.rescore_post", side_effect=RuntimeError("boom")):
            r = _client().post("/api/user/post/rescore",
                               json={"session_token": "t", "post_id": 42})
        assert r.status_code == 500


class TestThresholdPreferences:
    def test_thresholds_round_trip_through_the_prefs_endpoints(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as save:
            r = _client().put("/api/user/engagement-preferences",
                              json={"session_token": "t", "authenticity_score_min": 85,
                                    "post_similarity_max_pct": 40})
        assert r.status_code == 200
        saved = save.call_args[0][1]
        assert saved["authenticity_score_min"] == 85
        assert saved["post_similarity_max_pct"] == 40

    def test_out_of_range_thresholds_are_clamped_at_the_boundary(self):
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.update_engagement_preferences", return_value=True) as save:
            _client().put("/api/user/engagement-preferences",
                          json={"session_token": "t", "authenticity_score_min": 500,
                                "post_similarity_max_pct": 1})
        saved = save.call_args[0][1]
        assert saved["authenticity_score_min"] == 100
        assert saved["post_similarity_max_pct"] == 10

    def test_get_exposes_the_defaults_the_ui_shows_as_placeholders(self, monkeypatch):
        monkeypatch.setenv("AUTHENTICITY_SCORE_MIN", "60")
        monkeypatch.setenv("POST_SIMILARITY_MAX", "0.55")
        with patch(f"{_API}.get_session_user_id", return_value=1), \
             patch(f"{_USER}.get_engagement_preferences",
                   return_value={"authenticity_score_min": None,
                                 "post_similarity_max_pct": None}), \
             patch(f"{_USER}.get_or_create_reply_inbound_token", return_value="tok"), \
             patch(f"{_API}.get_gmail_forward_confirmation", return_value=None), \
             patch(f"{_USER}.max_catchup_touches_allowed", return_value=5):
            r = _client().get("/api/user/engagement-preferences?session_token=t")
        assert r.json()["detail"]["gate_defaults"] == {"authenticity_score_min": 60,
                                                       "post_similarity_max_pct": 55}
