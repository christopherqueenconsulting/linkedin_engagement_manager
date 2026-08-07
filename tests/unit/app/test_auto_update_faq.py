"""Unit tests for the hourly auto-FAQ beat task and its notification/email path (issue #507)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_FAQ = "cqc_lem.utilities.feedback.faq_service"
_NOTIFY = "cqc_lem.utilities.notifications"


class TestAutoUpdateFaq:
    def test_summarises_the_pass(self):
        with patch(f"{_FAQ}.process_faq_feedback",
                   return_value={"candidates": 9, "questions": 6, "clusters": 2,
                                 "counts": {"published": 1, "pending": 1}}) as process:
            from cqc_lem.app.run_scheduler import auto_update_faq
            result = auto_update_faq(limit=10)
        process.assert_called_once_with(limit=10)
        assert result == ("Auto-FAQ: 6 question(s) in 2 cluster(s) — "
                          "{'published': 1, 'pending': 1}")

    def test_an_empty_pass_reads_as_nothing_to_do(self):
        with patch(f"{_FAQ}.process_faq_feedback",
                   return_value={"candidates": 0, "questions": 0, "clusters": 0, "counts": {}}):
            from cqc_lem.app.run_scheduler import auto_update_faq
            assert auto_update_faq() == "Auto-FAQ: 0 question(s) in 0 cluster(s) — nothing to do"


class TestBeatSchedule:
    def test_the_faq_pass_runs_hourly_after_both_filing_ticks(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["update-faq"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_update_faq"
        assert entry["schedule"].minute == {50}


class TestNotifyFaqAnswer:
    def test_sends_the_answer_to_the_asker(self):
        from cqc_lem.utilities.notifications import notify_faq_answer
        with patch(f"{_NOTIFY}.get_user_email", return_value="a@b.co"), \
             patch("cqc_lem.utilities.email.send_faq_answer_email", return_value=True) as send:
            assert notify_faq_answer(4, "How do I pause it?", "From the account page.") is True
        assert send.call_args[0] == ("a@b.co", "How do I pause it?", "From the account page.")

    def test_no_email_address_means_no_send(self):
        from cqc_lem.utilities.notifications import notify_faq_answer
        with patch(f"{_NOTIFY}.get_user_email", return_value=None), \
             patch("cqc_lem.utilities.email.send_faq_answer_email") as send:
            assert notify_faq_answer(4, "Q?", "A.") is False
        send.assert_not_called()

    def test_a_raising_send_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_faq_answer
        with patch(f"{_NOTIFY}.get_user_email", return_value="a@b.co"), \
             patch("cqc_lem.utilities.email.send_faq_answer_email",
                   side_effect=RuntimeError("smtp down")):
            assert notify_faq_answer(4, "Q?", "A.") is False


class TestFaqAnswerEmail:
    def test_escapes_both_halves_and_links_the_faq(self):
        from cqc_lem.utilities.email import send_faq_answer_email
        with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
            assert send_faq_answer_email("a@b.co", "<script>x</script>?",
                                         "Line one\nLine two") is True
        to_email, _subject, html = dispatch.call_args[0]
        assert to_email == "a@b.co"
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert "Line one<br/>Line two" in html
        assert "#faq" in html
