"""Unit tests for the NPS/review survey policy and capture (issue #501)."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_S = "cqc_lem.utilities.surveys"

NOW = datetime(2026, 7, 25, 12, 0, 0)


class TestBands:
    @pytest.mark.parametrize("score,band", [(0, "detractor"), (6, "detractor"), (7, "passive"),
                                            (8, "passive"), (9, "promoter"), (10, "promoter")])
    def test_nps_bucket(self, score, band):
        from cqc_lem.utilities.surveys import nps_bucket
        assert nps_bucket(score) == band

    @pytest.mark.parametrize("rating,sentiment", [(1, "negative"), (2, "negative"), (3, "neutral"),
                                                  (4, "positive"), (5, "positive")])
    def test_review_sentiment(self, rating, sentiment):
        from cqc_lem.utilities.surveys import review_sentiment
        assert review_sentiment(rating) == sentiment


class TestSelectSurvey:
    def test_day3_nps_after_activation(self):
        from cqc_lem.utilities.surveys import SURVEY_NPS_DAY3, select_survey
        survey = select_survey(activated_at=NOW - timedelta(days=3), now=NOW)
        assert survey["key"] == SURVEY_NPS_DAY3
        assert survey["source"] == "nps"
        assert survey["cta_path"] == f"/?survey={SURVEY_NPS_DAY3}"

    def test_nothing_before_day3(self):
        from cqc_lem.utilities.surveys import select_survey
        assert select_survey(activated_at=NOW - timedelta(days=2, hours=23), now=NOW) is None

    def test_never_activated_and_no_trial_ending_is_not_asked(self):
        from cqc_lem.utilities.surveys import select_survey
        assert select_survey(trial_ends_at=NOW + timedelta(days=9), now=NOW) is None

    def test_review_offer_outranks_nps_at_trial_end(self):
        from cqc_lem.utilities.surveys import SURVEY_REVIEW_TRIAL_END, select_survey
        survey = select_survey(activated_at=NOW - timedelta(days=10),
                               trial_ends_at=NOW + timedelta(days=2), now=NOW)
        assert survey["key"] == SURVEY_REVIEW_TRIAL_END
        assert survey["source"] == "review"

    def test_trial_end_nps_once_the_review_is_in(self):
        from cqc_lem.utilities.surveys import SURVEY_NPS_TRIAL_END, select_survey
        survey = select_survey(trial_ends_at=NOW + timedelta(days=1),
                               review_answered_at=NOW - timedelta(days=1), now=NOW)
        assert survey["key"] == SURVEY_NPS_TRIAL_END

    def test_expired_trial_is_not_a_trial_ending(self):
        from cqc_lem.utilities.surveys import select_survey
        assert select_survey(trial_ends_at=NOW - timedelta(hours=1), now=NOW) is None

    def test_answered_surveys_are_never_re_asked(self):
        from cqc_lem.utilities.surveys import select_survey
        assert select_survey(activated_at=NOW - timedelta(days=30),
                             trial_ends_at=NOW + timedelta(days=1),
                             nps_answered_at=NOW - timedelta(days=20),
                             review_answered_at=NOW - timedelta(days=20), now=NOW) is None

    def test_already_asked_surveys_are_skipped(self):
        from cqc_lem.utilities.surveys import (
            SURVEY_NPS_DAY3,
            SURVEY_NPS_TRIAL_END,
            SURVEY_REVIEW_TRIAL_END,
            select_survey,
        )
        survey = select_survey(activated_at=NOW - timedelta(days=5),
                               trial_ends_at=NOW + timedelta(days=1),
                               sent_keys={SURVEY_REVIEW_TRIAL_END}, now=NOW)
        assert survey["key"] == SURVEY_NPS_DAY3
        assert select_survey(activated_at=NOW - timedelta(days=5),
                             trial_ends_at=NOW + timedelta(days=1),
                             sent_keys={SURVEY_REVIEW_TRIAL_END, SURVEY_NPS_DAY3},
                             now=NOW)["key"] == SURVEY_NPS_TRIAL_END
        assert select_survey(activated_at=NOW - timedelta(days=5),
                             trial_ends_at=NOW + timedelta(days=1),
                             sent_keys={SURVEY_REVIEW_TRIAL_END, SURVEY_NPS_DAY3,
                                        SURVEY_NPS_TRIAL_END}, now=NOW) is None

    def test_cooldown_blocks_a_second_ask_the_same_day(self):
        from cqc_lem.utilities.surveys import select_survey
        assert select_survey(activated_at=NOW - timedelta(days=5),
                             last_sent_at=NOW - timedelta(hours=3), now=NOW) is None
        assert select_survey(activated_at=NOW - timedelta(days=5),
                             last_sent_at=NOW - timedelta(hours=25), now=NOW) is not None

    def test_defaults_to_wall_clock_now(self):
        from cqc_lem.utilities.surveys import SURVEY_NPS_DAY3, select_survey
        assert select_survey(activated_at=datetime.now() - timedelta(days=4))["key"] == \
            SURVEY_NPS_DAY3


class TestNextSurveyForUser:
    def test_binds_state_answers_and_ledger(self):
        from cqc_lem.utilities.surveys import SURVEY_NPS_DAY3
        with patch(f"{_S}.get_onboarding_state",
                   return_value={"activated_at": datetime.now() - timedelta(days=4)}), \
             patch(f"{_S}.get_user_subscription_info", return_value={"trial_ends_at": None}), \
             patch(f"{_S}.get_survey_prompts_sent", return_value={}), \
             patch(f"{_S}.get_latest_feedback_at", return_value=None):
            from cqc_lem.utilities.surveys import next_survey_for_user
            assert next_survey_for_user(7)["key"] == SURVEY_NPS_DAY3

    def test_missing_state_and_subscription_are_survivable(self):
        with patch(f"{_S}.get_onboarding_state", return_value={}), \
             patch(f"{_S}.get_user_subscription_info", return_value=None), \
             patch(f"{_S}.get_survey_prompts_sent", return_value={}), \
             patch(f"{_S}.get_latest_feedback_at", return_value=None):
            from cqc_lem.utilities.surveys import next_survey_for_user
            assert next_survey_for_user(7) is None

    def test_snapshot_exposes_the_survey_and_the_review_gate(self):
        from cqc_lem.utilities.surveys import SURVEY_REVIEW_TRIAL_END
        survey = {"key": SURVEY_REVIEW_TRIAL_END, "source": "review", "headline": "h", "body": "b",
                  "cta_label": "Write a review", "cta_path": "/?survey=review_trial_end"}
        with patch(f"{_S}.next_survey_for_user", return_value=survey), \
             patch(f"{_S}.has_review_feedback", return_value=False):
            from cqc_lem.utilities.surveys import survey_snapshot
            snap = survey_snapshot(7)
        assert snap["survey"]["key"] == SURVEY_REVIEW_TRIAL_END
        assert "cta_path" not in snap["survey"]  # the SPA opens the modal itself
        assert snap["review_submitted"] is False
        assert (snap["nps_min"], snap["nps_max"]) == (0, 10)

    def test_snapshot_without_a_due_survey(self):
        with patch(f"{_S}.next_survey_for_user", return_value=None), \
             patch(f"{_S}.has_review_feedback", return_value=True):
            from cqc_lem.utilities.surveys import survey_snapshot
            snap = survey_snapshot(7)
        assert snap["survey"] is None
        assert snap["review_submitted"] is True


class TestRecordNps:
    def test_stores_the_why_as_the_body_with_the_score_in_context(self):
        with patch(f"{_S}.insert_feedback", return_value=11) as insert, \
             patch(f"{_S}.record_survey_prompt") as record, \
             patch("cqc_lem.utilities.observability.track_survey_response") as track:
            from cqc_lem.utilities.db import FeedbackSource
            from cqc_lem.utilities.surveys import SURVEY_NPS_DAY3, record_nps_response
            assert record_nps_response(7, 9, why="  It writes like me  ",
                                       context={"route": "/"},
                                       survey_key=SURVEY_NPS_DAY3) == 11
        kwargs = insert.call_args.kwargs
        assert insert.call_args.args[0] == "It writes like me"
        assert kwargs["source"] == FeedbackSource.NPS
        assert kwargs["sentiment"] == "promoter"
        assert kwargs["context"] == {"route": "/", "score": 9, "survey_key": SURVEY_NPS_DAY3}
        record.assert_called_once_with(7, SURVEY_NPS_DAY3)
        assert track.call_args.kwargs["score"] == 9

    def test_score_only_answer_still_persists(self):
        with patch(f"{_S}.insert_feedback", return_value=12) as insert, \
             patch(f"{_S}.record_survey_prompt"), \
             patch("cqc_lem.utilities.observability.track_survey_response"):
            from cqc_lem.utilities.surveys import record_nps_response
            assert record_nps_response(7, 3) == 12
        assert insert.call_args.args[0] == "NPS 3/10 — no comment"
        assert insert.call_args.kwargs["sentiment"] == "detractor"

    def test_unknown_survey_key_is_not_recorded_as_asked(self):
        with patch(f"{_S}.insert_feedback", return_value=13), \
             patch(f"{_S}.record_survey_prompt") as record, \
             patch("cqc_lem.utilities.observability.track_survey_response"):
            from cqc_lem.utilities.surveys import record_nps_response
            record_nps_response(7, 10, survey_key="made-up")
        record.assert_not_called()

    def test_failed_insert_skips_the_ledger_and_analytics(self):
        with patch(f"{_S}.insert_feedback", return_value=None), \
             patch(f"{_S}.record_survey_prompt") as record, \
             patch("cqc_lem.utilities.observability.track_survey_response") as track:
            from cqc_lem.utilities.surveys import record_nps_response
            assert record_nps_response(7, 10) is None
        record.assert_not_called()
        track.assert_not_called()

    def test_tracking_failure_never_loses_the_response(self):
        with patch(f"{_S}.insert_feedback", return_value=14), \
             patch(f"{_S}.record_survey_prompt"), \
             patch("cqc_lem.utilities.observability.track_survey_response",
                   side_effect=RuntimeError("posthog down")):
            from cqc_lem.utilities.surveys import record_nps_response
            assert record_nps_response(7, 10) == 14


class TestRecordReview:
    def test_body_joins_the_testimonial_and_the_improvement(self):
        with patch(f"{_S}.insert_feedback", return_value=21) as insert, \
             patch(f"{_S}.record_survey_prompt") as record, \
             patch("cqc_lem.utilities.observability.track_survey_response") as track:
            from cqc_lem.utilities.db import FeedbackSource
            from cqc_lem.utilities.surveys import SURVEY_REVIEW_TRIAL_END, record_review_response
            assert record_review_response(7, 5, improvement="Faster carousels",
                                          testimonial="Saves me an hour a day",
                                          consent_testimonial=True,
                                          survey_key=SURVEY_REVIEW_TRIAL_END) == 21
        kwargs = insert.call_args.kwargs
        assert insert.call_args.args[0] == "Saves me an hour a day\n\nFaster carousels"
        assert kwargs["source"] == FeedbackSource.REVIEW
        assert kwargs["sentiment"] == "positive"
        assert kwargs["context"]["rating"] == 5
        assert kwargs["context"]["consent_testimonial"] is True
        assert kwargs["context"]["improvement"] == "Faster carousels"
        record.assert_called_once_with(7, SURVEY_REVIEW_TRIAL_END)
        assert track.call_args.kwargs["rating"] == 5

    def test_rating_only_review_still_persists_and_defaults_consent_off(self):
        with patch(f"{_S}.insert_feedback", return_value=22) as insert, \
             patch(f"{_S}.record_survey_prompt"), \
             patch("cqc_lem.utilities.observability.track_survey_response"):
            from cqc_lem.utilities.surveys import record_review_response
            assert record_review_response(7, 2) == 22
        assert insert.call_args.args[0] == "Review 2/5 — no comment"
        assert insert.call_args.kwargs["sentiment"] == "negative"
        assert insert.call_args.kwargs["context"]["consent_testimonial"] is False
        assert insert.call_args.kwargs["context"]["testimonial"] is None


class TestDismissAndSend:
    def test_dismiss_records_a_known_survey_only(self):
        with patch(f"{_S}.record_survey_prompt", return_value=True) as record:
            from cqc_lem.utilities.surveys import SURVEY_NPS_DAY3, dismiss_survey
            assert dismiss_survey(7, SURVEY_NPS_DAY3) is True
            assert dismiss_survey(7, "nope") is False
        record.assert_called_once_with(7, SURVEY_NPS_DAY3)

    def test_send_records_the_ask_only_when_the_email_went_out(self):
        survey = {"key": "nps_day3"}
        with patch("cqc_lem.utilities.notifications.notify_survey_prompt", return_value=True), \
             patch(f"{_S}.record_survey_prompt") as record:
            from cqc_lem.utilities.surveys import send_survey_prompt
            assert send_survey_prompt(7, survey) is True
        record.assert_called_once_with(7, "nps_day3")

        with patch("cqc_lem.utilities.notifications.notify_survey_prompt", return_value=False), \
             patch(f"{_S}.record_survey_prompt") as record:
            from cqc_lem.utilities.surveys import send_survey_prompt
            assert send_survey_prompt(7, survey) is False
        record.assert_not_called()
