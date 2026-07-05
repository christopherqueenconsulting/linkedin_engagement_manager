"""Unit tests for throttled LinkedIn-session notifications."""

from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

from cqc_lem.utilities.notifications import notify_linkedin_session

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.notifications"


def test_skips_when_recently_sent(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at",
               return_value=datetime.now() - timedelta(days=1)), \
         patch(f"{_MOD}.get_user_email") as ge, \
         patch(f"{_MOD}.send_connect_linkedin_email") as sc:
        assert notify_linkedin_session(7) is False
        ge.assert_not_called()
        sc.assert_not_called()


def test_sends_connect_when_due(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at", return_value=None), \
         patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_connect_linkedin_email", return_value=True) as sc, \
         patch(f"{_MOD}.send_session_revalidation_email") as sr, \
         patch(f"{_MOD}.set_linkedin_session_email_sent_at") as stamp:
        assert notify_linkedin_session(7, revalidation=False) is True
        sc.assert_called_once_with("u@e.com")
        sr.assert_not_called()
        stamp.assert_called_once_with(7)


def test_sends_revalidation_when_flagged(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at",
               return_value=datetime.now() - timedelta(days=30)), \
         patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_session_revalidation_email", return_value=True) as sr, \
         patch(f"{_MOD}.send_connect_linkedin_email") as sc, \
         patch(f"{_MOD}.set_linkedin_session_email_sent_at"):
        assert notify_linkedin_session(7, revalidation=True) is True
        sr.assert_called_once_with("u@e.com")
        sc.assert_not_called()


def test_no_email_returns_false_and_does_not_stamp(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at", return_value=None), \
         patch(f"{_MOD}.get_user_email", return_value=None), \
         patch(f"{_MOD}.send_connect_linkedin_email") as sc, \
         patch(f"{_MOD}.set_linkedin_session_email_sent_at") as stamp:
        assert notify_linkedin_session(7) is False
        sc.assert_not_called()
        stamp.assert_not_called()


def test_throttle_zero_always_sends(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "0")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at",
               return_value=datetime.now()), \
         patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_connect_linkedin_email", return_value=True) as sc, \
         patch(f"{_MOD}.set_linkedin_session_email_sent_at"):
        assert notify_linkedin_session(7) is True
        sc.assert_called_once()


def test_newsletter_draft_ready_sends():
    from datetime import datetime
    from cqc_lem.utilities.notifications import notify_newsletter_draft_ready
    with patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_newsletter_draft_ready_email", return_value=True) as snd:
        assert notify_newsletter_draft_ready(3, "My Edition", datetime(2026, 7, 7, 13, 0)) is True
        snd.assert_called_once()
        assert snd.call_args[0][1] == "My Edition"


def test_newsletter_draft_ready_no_email():
    from cqc_lem.utilities.notifications import notify_newsletter_draft_ready
    with patch(f"{_MOD}.get_user_email", return_value=None), \
         patch(f"{_MOD}.send_newsletter_draft_ready_email") as snd:
        assert notify_newsletter_draft_ready(3, "My Edition", "2026-07-07") is False
        snd.assert_not_called()


def test_not_stamped_when_send_fails(monkeypatch):
    monkeypatch.setenv("LINKEDIN_SESSION_EMAIL_THROTTLE_DAYS", "7")
    with patch(f"{_MOD}.get_linkedin_session_email_sent_at", return_value=None), \
         patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_connect_linkedin_email", return_value=False), \
         patch(f"{_MOD}.set_linkedin_session_email_sent_at") as stamp:
        assert notify_linkedin_session(7) is False
        stamp.assert_not_called()
