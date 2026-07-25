"""Unit tests for survey invite delivery (issue #501): the email builder and the notify wrapper."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_SURVEY = {"key": "review_trial_end", "source": "review",
           "subject": "Leave a review, unlock the extended trial",
           "headline": "Write a review, keep LEM running longer",
           "body": "Your trial ends in a few days.",
           "cta_label": "Write a review", "cta_path": "/?survey=review_trial_end"}


class TestSendSurveyPromptEmail:
    def test_builds_an_absolute_cta_url_and_dispatches(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch, \
             patch.dict("os.environ", {"LEM_APP_URL": "https://app.example.com/"}):
            from cqc_lem.utilities.email import send_survey_prompt_email
            assert send_survey_prompt_email(
                "u@example.com", _SURVEY["subject"], _SURVEY["headline"], _SURVEY["body"],
                _SURVEY["cta_label"], _SURVEY["cta_path"]) is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "u@example.com"
        assert subject == _SURVEY["subject"]
        assert "https://app.example.com/?survey=review_trial_end" in html
        assert _SURVEY["body"] in html

    def test_relative_path_without_a_leading_slash_still_works(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch, \
             patch.dict("os.environ", {"LEM_APP_URL": "https://app.example.com"}):
            from cqc_lem.utilities.email import send_survey_prompt_email
            send_survey_prompt_email("u@example.com", "s", "h", "b", "Go", "account")
        assert "https://app.example.com/account" in dispatch.call_args[0][2]

    def test_escapes_caller_supplied_copy(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
            from cqc_lem.utilities.email import send_survey_prompt_email
            send_survey_prompt_email("u@example.com", "s", "<script>x</script>",
                                     "Line one\nAT&T & <b>bold</b>", "Go </a><script>", "/")
        html = dispatch.call_args[0][2]
        assert "<script>" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html
        assert "Line one<br/>AT&amp;T &amp; &lt;b&gt;bold&lt;/b&gt;" in html


class TestNotifySurveyPrompt:
    def test_sends_and_tracks(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_survey_prompt_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_survey_prompt") as track:
            from cqc_lem.utilities.notifications import notify_survey_prompt
            assert notify_survey_prompt(9, _SURVEY) is True
        assert send.call_args.kwargs["subject"] == _SURVEY["subject"]
        assert send.call_args.kwargs["cta_path"] == _SURVEY["cta_path"]
        track.assert_called_once_with(9, "review_trial_end")

    def test_no_email_address_means_no_send(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value=None), \
             patch("cqc_lem.utilities.email.send_survey_prompt_email") as send:
            from cqc_lem.utilities.notifications import notify_survey_prompt
            assert notify_survey_prompt(9, _SURVEY) is False
        send.assert_not_called()

    def test_unsent_email_is_not_tracked(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_survey_prompt_email", return_value=False), \
             patch("cqc_lem.utilities.observability.track_survey_prompt") as track:
            from cqc_lem.utilities.notifications import notify_survey_prompt
            assert notify_survey_prompt(9, _SURVEY) is False
        track.assert_not_called()

    def test_a_send_failure_is_never_fatal(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_survey_prompt_email",
                   side_effect=RuntimeError("smtp down")):
            from cqc_lem.utilities.notifications import notify_survey_prompt
            assert notify_survey_prompt(9, _SURVEY) is False

    def test_falls_back_to_default_copy(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_survey_prompt_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_survey_prompt"):
            from cqc_lem.utilities.notifications import notify_survey_prompt
            assert notify_survey_prompt(9, {"key": "nps_day3"}) is True
        assert send.call_args.kwargs["subject"] == "How are we doing?"
        assert send.call_args.kwargs["cta_path"] == "/"


class TestTrackers:
    def test_survey_events_carry_their_properties(self):
        with patch("cqc_lem.utilities.observability.posthog") as ph:
            from cqc_lem.utilities.observability import track_survey_prompt, track_survey_response
            track_survey_prompt(9, "nps_day3")
            track_survey_response(9, "nps", score=10)
        prompt_kwargs = ph.capture.call_args_list[0].kwargs
        assert prompt_kwargs["event"] == "survey_prompt"
        assert prompt_kwargs["properties"]["survey"] == "nps_day3"
        response_kwargs = ph.capture.call_args_list[1].kwargs
        assert response_kwargs["event"] == "survey_response"
        assert response_kwargs["properties"] == {"source": "nps", "score": 10}
