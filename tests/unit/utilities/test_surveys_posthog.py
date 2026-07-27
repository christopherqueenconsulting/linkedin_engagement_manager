"""Unit tests for the PostHog Surveys half of `utilities/surveys.py` (issue #653).

Two things are load-bearing and both are asserted here: a PostHog answer becomes a `feedback` row
(so it reaches the auto-work loop) WITHOUT emitting the homegrown `survey_response` event (so one
answer is counted once), and the homegrown NPS scheduler stands down when PostHog owns NPS (so
nobody is prompted twice)."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from cqc_lem.utilities import surveys
from cqc_lem.utilities.db import FeedbackSource, FeedbackStatus

pytestmark = pytest.mark.unit

_S = "cqc_lem.utilities.surveys"


class TestKindRegistry:
    def test_both_surveys_are_registered_with_their_scales(self):
        kinds = surveys.posthog_survey_kinds()
        assert set(kinds) == {surveys.POSTHOG_KIND_NPS, surveys.POSTHOG_KIND_CSAT}
        assert (kinds["nps"]["min"], kinds["nps"]["max"]) == (0, 10)
        assert (kinds["csat"]["min"], kinds["csat"]["max"]) == (1, 5)
        assert kinds["nps"]["source"] == FeedbackSource.NPS
        assert kinds["csat"]["source"] == FeedbackSource.CSAT

    def test_the_registry_is_a_copy(self):
        # The SPA contract test and the API handler both read it; a caller must not be able to
        # mutate the module's own registry through the returned dict.
        surveys.posthog_survey_kinds()["nps"]["max"] = 99
        assert surveys.posthog_survey_kinds()["nps"]["max"] == 10

    def test_survey_names_match_the_provisioning_script(self):
        import importlib.util
        import pathlib
        path = (pathlib.Path(__file__).resolve().parents[3] / "scripts" / "posthog_surveys.py")
        spec = importlib.util.spec_from_file_location("posthog_surveys", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.NPS_SURVEY_NAME == surveys.POSTHOG_NPS_SURVEY_NAME
        assert module.CSAT_SURVEY_NAME == surveys.POSTHOG_CSAT_SURVEY_NAME
        assert module.NPS_AFTER_ACTIVATION_DAYS == surveys.POSTHOG_NPS_AFTER_ACTIVATION_DAYS
        assert module.CSAT_MIN_APPROVALS == surveys.POSTHOG_CSAT_MIN_APPROVALS
        assert module.SURVEY_WAIT_DAYS == surveys.POSTHOG_SURVEY_WAIT_DAYS


class TestLowScore:
    @pytest.mark.parametrize("score,low", [(0, True), (6, True), (7, False), (10, False)])
    def test_nps_detractors_are_low(self, score, low):
        assert surveys.is_low_score("nps", score) is low

    @pytest.mark.parametrize("score,low", [(1, True), (2, True), (3, False), (5, False)])
    def test_csat_one_and_two_are_low(self, score, low):
        assert surveys.is_low_score("csat", score) is low


class TestRecordPostHogResponse:
    def test_nps_lands_as_a_feedback_row_with_the_posthog_origin(self):
        with patch(f"{_S}.insert_feedback", return_value=77) as insert, \
             patch(f"{_S}.update_feedback_triage") as triage:
            result = surveys.record_posthog_survey_response(
                4, "nps", 3, comment="The comments read like a bot",
                survey_id="01900", survey_name="LEM NPS", context={"route": "/content"})

        assert result == {"feedback_id": 77, "sentiment": "detractor",
                          "low_score": True, "actionable": True}
        assert insert.call_args.args[0] == "The comments read like a bot"
        assert insert.call_args.kwargs["source"] == FeedbackSource.NPS
        context = insert.call_args.kwargs["context"]
        assert context["origin"] == "posthog_survey"
        assert context["posthog_survey_id"] == "01900"
        assert context["score"] == 3
        assert context["route"] == "/content"
        # A detractor stays `new` — that IS the feedback report.
        triage.assert_not_called()

    def test_a_happy_answer_with_no_comment_is_resolved_on_the_spot(self):
        with patch(f"{_S}.insert_feedback", return_value=78), \
             patch(f"{_S}.update_feedback_triage") as triage:
            result = surveys.record_posthog_survey_response(4, "csat", 5)

        assert result["actionable"] is False
        triage.assert_called_once_with(78, status=FeedbackStatus.RESOLVED)

    def test_a_happy_answer_WITH_a_comment_stays_in_the_loop(self):
        with patch(f"{_S}.insert_feedback", return_value=79) as insert, \
             patch(f"{_S}.update_feedback_triage") as triage:
            result = surveys.record_posthog_survey_response(4, "csat", 5,
                                                            comment="Add a carousel template")

        assert result["actionable"] is True
        triage.assert_not_called()
        assert insert.call_args.args[0] == "Add a carousel template"

    def test_no_comment_still_writes_a_readable_body(self):
        with patch(f"{_S}.insert_feedback", return_value=80) as insert, \
             patch(f"{_S}.update_feedback_triage"):
            surveys.record_posthog_survey_response(4, "nps", 9)
        assert insert.call_args.args[0] == "NPS 9/10 — no comment"

    def test_the_homegrown_survey_response_event_is_never_emitted(self):
        # PostHog's own `survey sent` already counted this answer in the browser. A second event
        # here would double every response-rate number on the dashboards.
        with patch(f"{_S}.insert_feedback", return_value=81), \
             patch(f"{_S}.update_feedback_triage"), \
             patch(f"{_S}._track_response") as track, \
             patch(f"{_S}.record_survey_prompt") as ledger:
            surveys.record_posthog_survey_response(4, "nps", 10)
        track.assert_not_called()
        ledger.assert_not_called()

    @pytest.mark.parametrize("kind,score", [("nps", 11), ("nps", -1), ("csat", 0), ("csat", 6),
                                            ("bogus", 5)])
    def test_an_unusable_kind_or_score_writes_nothing(self, kind, score):
        with patch(f"{_S}.insert_feedback") as insert:
            assert surveys.record_posthog_survey_response(4, kind, score) is None
        insert.assert_not_called()

    def test_a_failed_insert_reports_nothing_rather_than_a_half_result(self):
        with patch(f"{_S}.insert_feedback", return_value=None), \
             patch(f"{_S}.update_feedback_triage") as triage:
            assert surveys.record_posthog_survey_response(4, "nps", 2) is None
        triage.assert_not_called()

    def test_a_non_numeric_score_is_rejected(self):
        with patch(f"{_S}.insert_feedback") as insert:
            assert surveys.record_posthog_survey_response(4, "nps", None) is None
        insert.assert_not_called()


class TestHomegrownStandDown:
    def _activated(self, days=10):
        return datetime.now() - timedelta(days=days)

    def test_nps_is_selected_while_posthog_is_off(self):
        chosen = surveys.select_survey(activated_at=self._activated())
        assert chosen["key"] == surveys.SURVEY_NPS_DAY3

    def test_nps_is_retired_when_posthog_owns_it(self):
        assert surveys.select_survey(activated_at=self._activated(), nps_enabled=False) is None

    def test_the_review_offer_survives_the_stand_down(self):
        # The review is the extended-trial gate (#499) — PostHog has no way to unlock that, so it
        # must keep being asked even when PostHog owns NPS.
        chosen = surveys.select_survey(activated_at=self._activated(),
                                       trial_ends_at=datetime.now() + timedelta(days=2),
                                       nps_enabled=False)
        assert chosen["key"] == surveys.SURVEY_REVIEW_TRIAL_END

    def test_trial_end_nps_is_retired_too(self):
        chosen = surveys.select_survey(trial_ends_at=datetime.now() + timedelta(days=2),
                                       review_answered_at=datetime.now(), nps_enabled=False)
        assert chosen is None

    def test_next_survey_for_user_reads_the_flag_at_the_call_site(self):
        with patch(f"{_S}.get_onboarding_state",
                   return_value={"activated_at": self._activated()}), \
             patch(f"{_S}.get_user_subscription_info", return_value={}), \
             patch(f"{_S}.get_survey_prompts_sent", return_value={}), \
             patch(f"{_S}.get_latest_feedback_at", return_value=None), \
             patch(f"{_S}.posthog_surveys_enabled", return_value=True) as flag:
            assert surveys.next_survey_for_user(4) is None
        flag.assert_called_once_with(4)


class TestFlagResolution:
    def test_a_flag_lookup_failure_keeps_the_homegrown_asks(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", side_effect=RuntimeError("posthog down")):
            assert surveys.posthog_surveys_enabled(4) is False

    def test_the_flag_is_read_per_user(self):
        with patch("cqc_lem.utilities.flags.flag_enabled", return_value=True) as flag:
            assert surveys.posthog_surveys_enabled(9) is True
        assert flag.call_args.args == ("posthog-surveys-enabled", 9)
