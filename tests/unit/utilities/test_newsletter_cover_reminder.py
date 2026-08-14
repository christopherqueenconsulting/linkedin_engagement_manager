"""Unit tests for the pre-slot newsletter cover reminder (issue #1432).

No generated cover had ever been approved in production, so every edition shipped cover-less and
nothing said so before the slot. These cover the notify half: the one-shot claim, the fail-open
behaviour, and the copy that names the consequence.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.notifications"
_EMAIL = "cqc_lem.utilities.email"
_RATE = "cqc_lem.utilities.linkedin.rate_limit"

_KEY = "lem:newsletter_cover_pending_email:12"


def _redis(set_result=True):
    client = MagicMock()
    client.set.return_value = set_result
    return client


class TestNotifyNewsletterCoverPending:
    def test_sends_and_claims_the_edition_slot(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=True) as send:
            assert notify_newsletter_cover_pending(
                3, 12, "My Edition", datetime(2026, 8, 20, 13, 0)) is True
        assert client.set.call_args[0][0] == _KEY
        assert client.set.call_args[1] == {"nx": True, "ex": 30 * 86400}
        to_email, title, when = send.call_args[0]
        assert (to_email, title) == ("u@e.com", "My Edition")
        assert "August 20" in when

    def test_second_pass_on_the_same_edition_is_silent(self):
        # One reminder per edition: the slot is a single event, so a daily beat must not nag.
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        with patch(f"{_RATE}.shared_redis_client", return_value=_redis(set_result=False)), \
             patch(f"{_MOD}.get_user_email") as get_email, \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email") as send:
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is False
        get_email.assert_not_called()
        send.assert_not_called()

    def test_non_datetime_slot_passes_through(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        with patch(f"{_RATE}.shared_redis_client", return_value=_redis()), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=True) as send:
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "2026-08-20") is True
        assert send.call_args[0][2] == "2026-08-20"

    def test_redis_outage_fails_open_and_still_emails(self):
        # Silence is the defect this lane exists to fix — losing Redis must not reintroduce it.
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        with patch(f"{_RATE}.shared_redis_client", side_effect=RuntimeError("no redis")), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=True) as send:
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is True
        send.assert_called_once()

    def test_no_redis_configured_still_emails(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        with patch(f"{_RATE}.shared_redis_client", return_value=None), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=True):
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is True

    def test_user_without_email_releases_the_claim(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value=None), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email") as send:
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is False
        send.assert_not_called()
        client.delete.assert_called_once_with(_KEY)

    def test_failed_send_releases_the_claim(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=False):
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is False
        client.delete.assert_called_once_with(_KEY)

    def test_successful_send_keeps_the_claim(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=True):
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is True
        client.delete.assert_not_called()

    def test_send_exception_is_swallowed_and_released(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", side_effect=RuntimeError("db down")):
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is False
        client.delete.assert_called_once_with(_KEY)

    def test_release_failure_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_newsletter_cover_pending
        client = _redis()
        client.delete.side_effect = RuntimeError("redis gone")
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_newsletter_cover_pending_email", return_value=False):
            assert notify_newsletter_cover_pending(3, 12, "My Edition", "soon") is False


class TestNewsletterCoverPendingEmail:
    def test_names_the_consequence_and_deep_links_to_the_queue(self, monkeypatch):
        monkeypatch.setenv("LEM_APP_URL", "https://app.example.com/")
        from cqc_lem.utilities.email import send_newsletter_cover_pending_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            assert send_newsletter_cover_pending_email(
                "u@e.com", "My Edition", "Thursday, August 20 at 01:00 PM UTC") is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "u@e.com"
        assert "My Edition" in subject
        # The whole point of the email: the edition still ships, the cover does not.
        assert "without a cover image" in html
        assert "Thursday, August 20 at 01:00 PM UTC" in html
        # The queue is the ONE screen with an Approve cover control — /account is not it.
        assert "https://app.example.com/content?tab=newsletters" in html
        assert "Approve cover" in html

    def test_untitled_edition_still_reads(self):
        from cqc_lem.utilities.email import send_newsletter_cover_pending_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_newsletter_cover_pending_email("u@e.com", "", "tomorrow")
        assert "Your next edition" in dispatch.call_args[0][1]
