"""Unit tests for the NPS / review survey endpoints (issue #501)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_S = "cqc_lem.utilities.surveys"


class TestNpsEndpoint:
    def test_persists_the_score_and_the_why(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_nps_response", return_value=101) as record, \
             patch("cqc_lem.utilities.db.has_review_feedback", return_value=False):
            resp = api_client.post("/api/survey/nps", json={
                "session_token": "tok", "score": 9, "why": "It sounds like me",
                "survey_key": "nps_day3", "context": {"route": "/"}})
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail == {"feedback_id": 101, "bucket": "promoter", "review_invite": True}
        assert record.call_args.args == (42, 9)
        assert record.call_args.kwargs["survey_key"] == "nps_day3"
        assert record.call_args.kwargs["context"] == {"route": "/"}

    def test_promoter_who_already_reviewed_is_not_re_invited(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_nps_response", return_value=102), \
             patch("cqc_lem.utilities.db.has_review_feedback", return_value=True):
            resp = api_client.post("/api/survey/nps",
                               json={"session_token": "tok", "score": 10})
        assert resp.json()["detail"]["review_invite"] is False

    def test_detractor_is_not_invited_to_review(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_nps_response", return_value=103), \
             patch("cqc_lem.utilities.db.has_review_feedback", return_value=False):
            resp = api_client.post("/api/survey/nps", json={"session_token": "tok", "score": 4})
        detail = resp.json()["detail"]
        assert detail["bucket"] == "detractor"
        assert detail["review_invite"] is False

    def test_oversized_context_is_dropped(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_nps_response", return_value=104) as record, \
             patch("cqc_lem.utilities.db.has_review_feedback", return_value=False):
            api_client.post("/api/survey/nps", json={"session_token": "tok", "score": 7,
                                                 "context": {"blob": "x" * 9000}})
        assert record.call_args.kwargs["context"] == {"truncated": True}

    @pytest.mark.parametrize("score", [-1, 11])
    def test_out_of_range_scores_are_rejected(self, api_client, score):
        with patch(f"{_M}.get_session_user_id", return_value=42):
            resp = api_client.post("/api/survey/nps", json={"session_token": "tok", "score": score})
        assert resp.status_code == 422

    def test_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.post("/api/survey/nps", json={"session_token": "bad", "score": 9})
        assert resp.status_code == 401

    def test_500_when_the_row_cannot_be_saved(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_nps_response", return_value=None):
            resp = api_client.post("/api/survey/nps", json={"session_token": "tok", "score": 9})
        assert resp.status_code == 500


class TestReviewEndpoint:
    def test_persists_the_review_and_reports_the_unlock(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_review_response", return_value=201) as record:
            resp = api_client.post("/api/survey/review", json={
                "session_token": "tok", "rating": 5, "improvement": "Faster carousels",
                "testimonial": "Saves me an hour a day", "consent_testimonial": True,
                "survey_key": "review_trial_end"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"feedback_id": 201, "trial_extension_unlocked": True}
        assert record.call_args.args == (42, 5)
        assert record.call_args.kwargs["consent_testimonial"] is True
        assert record.call_args.kwargs["testimonial"] == "Saves me an hour a day"

    @pytest.mark.parametrize("rating", [0, 6])
    def test_out_of_range_ratings_are_rejected(self, api_client, rating):
        with patch(f"{_M}.get_session_user_id", return_value=42):
            resp = api_client.post("/api/survey/review",
                               json={"session_token": "tok", "rating": rating})
        assert resp.status_code == 422

    def test_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.post("/api/survey/review", json={"session_token": "bad", "rating": 5})
        assert resp.status_code == 401

    def test_500_when_the_row_cannot_be_saved(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_review_response", return_value=None):
            resp = api_client.post("/api/survey/review", json={"session_token": "tok", "rating": 5})
        assert resp.status_code == 500


class TestDismissAndSnapshot:
    def test_dismiss_records_the_ask(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.dismiss_survey", return_value=True) as dismiss:
            resp = api_client.post("/api/survey/dismiss",
                               json={"session_token": "tok", "survey_key": "nps_day3"})
        assert resp.json()["detail"] == {"dismissed": True}
        dismiss.assert_called_once_with(42, "nps_day3")

    def test_dismiss_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.post("/api/survey/dismiss",
                               json={"session_token": "bad", "survey_key": "nps_day3"})
        assert resp.status_code == 401

    def test_snapshot_returns_the_due_survey(self, api_client):
        snapshot = {"survey": {"key": "nps_day3", "source": "nps", "headline": "h", "body": "b",
                               "cta_label": "Give a score"},
                    "review_submitted": False, "nps_min": 0, "nps_max": 10,
                    "review_min_rating": 1, "review_max_rating": 5}
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.survey_snapshot", return_value=snapshot) as snap:
            resp = api_client.get("/api/user/survey?session_token=tok")
        assert resp.status_code == 200
        assert resp.json()["detail"]["survey"]["key"] == "nps_day3"
        snap.assert_called_once_with(42)

    def test_snapshot_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/survey?session_token=bad")
        assert resp.status_code == 401


class TestPostHogSurveyEndpoint:
    """POST /survey/posthog — the bridge from a PostHog survey answer into the feedback loop
    (issue #653).
    """

    def test_an_nps_answer_is_persisted_with_its_posthog_identifiers(self, api_client):
        result = {"feedback_id": 501, "sentiment": "detractor", "low_score": True,
                  "actionable": True}
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_posthog_survey_response", return_value=result) as record:
            resp = api_client.post("/api/survey/posthog", json={
                "session_token": "tok", "kind": "nps", "score": 3,
                "comment": "The comments read like a bot", "survey_id": "0199",
                "survey_name": "LEM NPS", "context": {"route": "/content"}})
        assert resp.status_code == 200
        assert resp.json()["detail"] == result
        assert record.call_args.args == (42, "nps", 3)
        assert record.call_args.kwargs["survey_id"] == "0199"
        assert record.call_args.kwargs["comment"] == "The comments read like a bot"
        assert record.call_args.kwargs["context"] == {"route": "/content"}

    def test_a_csat_answer_uses_the_csat_scale(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_posthog_survey_response",
                   return_value={"feedback_id": 502}) as record:
            resp = api_client.post("/api/survey/posthog", json={
                "session_token": "tok", "kind": "csat", "score": 5})
        assert resp.status_code == 200
        assert record.call_args.args == (42, "csat", 5)

    def test_a_score_outside_the_kind_scale_is_rejected(self, api_client):
        # 6 is a legal NPS score and an impossible CSAT one — the bound is the KIND's, which a
        # single field constraint could never express.
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_posthog_survey_response") as record:
            resp = api_client.post("/api/survey/posthog", json={
                "session_token": "tok", "kind": "csat", "score": 6})
        assert resp.status_code == 422
        record.assert_not_called()

    def test_an_unknown_kind_is_rejected(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_posthog_survey_response") as record:
            resp = api_client.post("/api/survey/posthog", json={
                "session_token": "tok", "kind": "pmf", "score": 5})
        assert resp.status_code == 422
        record.assert_not_called()

    def test_401_without_a_session(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.post("/api/survey/posthog",
                               json={"session_token": "bad", "kind": "nps", "score": 5})
        assert resp.status_code == 401

    def test_500_when_the_row_cannot_be_saved(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_S}.record_posthog_survey_response", return_value=None):
            resp = api_client.post("/api/survey/posthog",
                               json={"session_token": "tok", "kind": "nps", "score": 5})
        assert resp.status_code == 500
