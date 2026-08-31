"""Unit tests for throttled LinkedIn-session notifications."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

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
        # Issue #1135: existing rows were backfilled to auto-publishing, so True is the default.
        assert snd.call_args[1]["auto_publish"] is True


def test_newsletter_draft_ready_carries_the_publish_gate():
    """Issue #1135 — an opted-out author must be told the draft is waiting on THEM."""
    from cqc_lem.utilities.notifications import notify_newsletter_draft_ready
    with patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
         patch(f"{_MOD}.send_newsletter_draft_ready_email", return_value=True) as snd:
        notify_newsletter_draft_ready(3, "My Edition", "2026-07-07", auto_publish=False)
    assert snd.call_args[1]["auto_publish"] is False


def test_newsletter_draft_ready_email_copy_reports_the_real_outcome():
    """The one email an opted-out author gets — it cannot say "it will auto-publish as-is"."""
    from cqc_lem.utilities.email import send_newsletter_draft_ready_email
    with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
        send_newsletter_draft_ready_email("u@e.com", "My Edition", "Tuesday", auto_publish=False)
    html = " ".join(dispatch.call_args[0][2].split())
    assert "auto-publish as-is" not in html
    assert "it publishes only once you <strong>approve</strong> it" in html
    assert "Tuesday" in html

    with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
        send_newsletter_draft_ready_email("u@e.com", "My Edition", "Tuesday")
    assert "auto-publish as-is on Tuesday" in " ".join(dispatch.call_args[0][2].split())


def test_newsletter_draft_ready_email_links_to_the_screen_that_can_approve(monkeypatch):
    """Issue #1135 — the email now ASKS for an approval, so its button has to reach one.

    Reviewing, approving and skipping an edition all live on the newsletter queue and nowhere
    else; `/account` only holds the settings card. That was cosmetic while every draft shipped on
    silence, and load-bearing the moment an opted-out edition publishes only on an approval.
    """
    monkeypatch.setenv("LEM_APP_URL", "https://app.example.com/")
    from cqc_lem.utilities.email import send_newsletter_draft_ready_email
    with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
        send_newsletter_draft_ready_email("u@e.com", "My Edition", "Tuesday", auto_publish=False)
    html = " ".join(dispatch.call_args[0][2].split())
    assert 'href="https://app.example.com/content?tab=newsletters"' in html
    assert "https://app.example.com/account" not in html
    assert "from your account page" not in html


def test_newsletter_draft_ready_title_with_markup_cannot_break_the_body():
    """Titles are LLM-authored, so '&' and angle brackets reach this template unfiltered.

    Raw, everything from '<' to the next '>' renders as a bogus tag and the sentence loses its
    subject; the subject line is plain text and keeps the title as written.
    """
    from cqc_lem.utilities.email import send_newsletter_draft_ready_email
    title = 'Q3 <b>Recap</b> & "More"'
    with patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as dispatch:
        send_newsletter_draft_ready_email("u@e.com", title, "Tuesday <script>")
    subject, html = dispatch.call_args[0][1], dispatch.call_args[0][2]
    assert title in subject
    assert "<b>Recap</b>" not in html
    assert "Q3 &lt;b&gt;Recap&lt;/b&gt; &amp; &quot;More&quot;" in html
    assert "<script>" not in html
    assert "Tuesday &lt;script&gt;" in html


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
