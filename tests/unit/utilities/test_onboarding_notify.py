"""Unit tests for onboarding nudge delivery (issue #500): the email builder, the notification
wrapper, and the brand-voice copy generator.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_NUDGE = {"key": "approve_post", "subject": "Your drafts are waiting",
          "headline": "Approve one post", "body": "Nothing publishes until you approve one.",
          "cta_label": "Review your drafts", "cta_path": "/content?tab=review"}


class TestSendOnboardingNudgeEmail:
    def test_builds_an_absolute_cta_url_and_dispatches(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch, \
             patch.dict("os.environ", {"LEM_APP_URL": "https://app.example.com/"}):
            from cqc_lem.utilities.email import send_onboarding_nudge_email
            assert send_onboarding_nudge_email(
                "u@example.com", _NUDGE["subject"], _NUDGE["headline"], _NUDGE["body"],
                _NUDGE["cta_label"], _NUDGE["cta_path"]) is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "u@example.com"
        assert subject == "Your drafts are waiting"
        assert "https://app.example.com/content?tab=review" in html
        assert _NUDGE["body"] in html

    def test_relative_path_without_a_leading_slash_still_works(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch, \
             patch.dict("os.environ", {"LEM_APP_URL": "https://app.example.com"}):
            from cqc_lem.utilities.email import send_onboarding_nudge_email
            send_onboarding_nudge_email("u@example.com", "s", "h", "b", "Go", "account")
        html = dispatch.call_args[0][2]
        assert "https://app.example.com/account" in html

    def test_escapes_caller_supplied_copy(self):
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
            from cqc_lem.utilities.email import send_onboarding_nudge_email
            send_onboarding_nudge_email(
                "u@example.com", "s", "<script>x</script>", "Line one\nAT&T & <b>bold</b>",
                "Go </a><script>", "/account")
        html = dispatch.call_args[0][2]
        assert "<script>" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html
        assert "Line one<br/>AT&amp;T &amp; &lt;b&gt;bold&lt;/b&gt;" in html


class TestNotifyOnboardingNudge:
    def test_sends_and_tracks(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_onboarding_nudge_email",
                   return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_onboarding_nudge") as track:
            from cqc_lem.utilities.notifications import notify_onboarding_nudge
            assert notify_onboarding_nudge(3, _NUDGE) is True
        assert send.call_args.kwargs["subject"] == _NUDGE["subject"]
        track.assert_called_once_with(3, "approve_post")

    def test_no_email_address_means_no_send(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value=None), \
             patch("cqc_lem.utilities.email.send_onboarding_nudge_email") as send:
            from cqc_lem.utilities.notifications import notify_onboarding_nudge
            assert notify_onboarding_nudge(3, _NUDGE) is False
        send.assert_not_called()

    def test_missing_copy_falls_back_to_defaults(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_onboarding_nudge_email",
                   return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_onboarding_nudge"):
            from cqc_lem.utilities.notifications import notify_onboarding_nudge
            assert notify_onboarding_nudge(3, {"key": "x"}) is True
        assert send.call_args.kwargs["cta_path"] == "/account"
        assert send.call_args.kwargs["cta_label"] == "Finish setup"

    def test_send_failure_is_reported_not_raised(self):
        with patch("cqc_lem.utilities.notifications.get_user_email", return_value="u@example.com"), \
             patch("cqc_lem.utilities.email.send_onboarding_nudge_email",
                   side_effect=RuntimeError("smtp down")):
            from cqc_lem.utilities.notifications import notify_onboarding_nudge
            assert notify_onboarding_nudge(3, _NUDGE) is False


class TestGenerateOnboardingNudgeCopy:
    def test_returns_the_model_copy(self):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="  Two tight sentences.  "))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response) as call:
            from cqc_lem.utilities.ai.ai_helper import generate_onboarding_nudge_copy
            assert generate_onboarding_nudge_copy(3, _NUDGE) == "Two tight sentences."
        assert call.call_args.kwargs["model"] == "lem-simple"
        assert call.call_args.kwargs["_track_user_id"] == 3

    def test_empty_completion_falls_back_to_the_default_body(self):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="  "))]
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=response):
            from cqc_lem.utilities.ai.ai_helper import generate_onboarding_nudge_copy
            assert generate_onboarding_nudge_copy(3, _NUDGE) == _NUDGE["body"]

    def test_llm_failure_falls_back_to_the_default_body(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", side_effect=RuntimeError("boom")):
            from cqc_lem.utilities.ai.ai_helper import generate_onboarding_nudge_copy
            assert generate_onboarding_nudge_copy(3, _NUDGE) == _NUDGE["body"]
