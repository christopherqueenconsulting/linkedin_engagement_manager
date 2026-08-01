"""Unit tests for the LinkedIn token-expiring notification + email (issue #600)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.notifications"
_EMAIL = "cqc_lem.utilities.email"
_RATE = "cqc_lem.utilities.linkedin.rate_limit"


def _redis(set_result=True):
    client = MagicMock()
    client.set.return_value = set_result
    return client


class TestNotifyLinkedInTokenExpiring:
    def test_sends_and_claims_the_throttle_slot(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True) as send:
            assert notify_linkedin_token_expiring(7, 5, "2026-09-01T00:00:00+00:00") is True
        send.assert_called_once_with("u@e.com", 5, "2026-09-01T00:00:00+00:00")
        key, value = client.set.call_args[0]
        assert key == "lem:linkedin_token_expiry_email:7"
        assert client.set.call_args[1] == {"nx": True, "ex": 7 * 86400}

    def test_second_call_inside_window_is_throttled(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        with patch(f"{_RATE}.shared_redis_client", return_value=_redis(set_result=False)), \
             patch(f"{_MOD}.get_user_email") as get_email, \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email") as send:
            assert notify_linkedin_token_expiring(7, 5) is False
        get_email.assert_not_called()
        send.assert_not_called()

    def test_throttle_window_is_configurable(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS", "3")
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True):
            assert notify_linkedin_token_expiring(7, 5) is True
        assert client.set.call_args[1]["ex"] == 3 * 86400

    def test_zero_throttle_never_touches_redis(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_TOKEN_EMAIL_THROTTLE_DAYS", "0")
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        with patch(f"{_RATE}.shared_redis_client") as redis, \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True):
            assert notify_linkedin_token_expiring(7) is True
        redis.assert_not_called()

    def test_redis_outage_fails_open_and_still_emails(self):
        # The reported bug was a warning with no email behind it — losing Redis must not
        # reintroduce silence.
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        with patch(f"{_RATE}.shared_redis_client", side_effect=RuntimeError("no redis")), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True) as send:
            assert notify_linkedin_token_expiring(7, 2) is True
        send.assert_called_once()

    def test_no_redis_configured_still_emails(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        with patch(f"{_RATE}.shared_redis_client", return_value=None), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True):
            assert notify_linkedin_token_expiring(7, 2) is True

    def test_user_without_email_returns_false(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value=None), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email") as send:
            assert notify_linkedin_token_expiring(7, 2) is False
        send.assert_not_called()
        client.delete.assert_called_once_with("lem:linkedin_token_expiry_email:7")

    def test_exception_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", side_effect=RuntimeError("db down")):
            assert notify_linkedin_token_expiring(7, 2) is False
        client.delete.assert_called_once_with("lem:linkedin_token_expiry_email:7")

    def test_failed_send_releases_the_throttle_slot(self):
        # Claiming the slot then failing to send would silence this user for a week — the exact
        # shape of the bug #600 exists to fix.
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=False):
            assert notify_linkedin_token_expiring(7, 2) is False
        client.delete.assert_called_once_with("lem:linkedin_token_expiry_email:7")

    def test_successful_send_keeps_the_throttle_slot(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=True):
            assert notify_linkedin_token_expiring(7, 2) is True
        client.delete.assert_not_called()

    def test_release_failure_is_swallowed(self):
        from cqc_lem.utilities.notifications import notify_linkedin_token_expiring
        client = _redis()
        client.delete.side_effect = RuntimeError("redis gone")
        with patch(f"{_RATE}.shared_redis_client", return_value=client), \
             patch(f"{_MOD}.get_user_email", return_value="u@e.com"), \
             patch(f"{_EMAIL}.send_linkedin_token_expiring_email", return_value=False):
            assert notify_linkedin_token_expiring(7, 2) is False


class TestLinkedInTokenExpiringEmail:
    def test_counts_down_in_days(self, monkeypatch):
        monkeypatch.setenv("LEM_APP_URL", "https://app.example.com/")
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            assert send_linkedin_token_expiring_email("u@e.com", 5, "Sep 1, 2026") is True
        to_email, subject, html = dispatch.call_args[0]
        assert to_email == "u@e.com"
        assert "expiring" in subject
        assert "expires in 5 days" in html
        assert "Sep 1, 2026" in html
        assert "https://app.example.com/account" in html

    def test_single_day_is_singular(self):
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_linkedin_token_expiring_email("u@e.com", 1)
        html = dispatch.call_args[0][2]
        assert "expires in 1 day<" in html or "expires in 1 day " in html
        assert "1 days" not in html

    def test_already_expired_copy_and_subject(self):
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_linkedin_token_expiring_email("u@e.com", 0)
        subject, html = dispatch.call_args[0][1], dispatch.call_args[0][2]
        assert "has expired" in subject
        assert "already expired" in html

    def test_iso_expiry_is_rendered_as_a_human_date(self):
        # resolve_token_status hands the beat an ISO-8601 timestamp; a customer must never read
        # "(on 2026-09-14T08:30:12+00:00)".
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_linkedin_token_expiring_email("u@e.com", 4, "2026-09-14T08:30:12+00:00")
        html = dispatch.call_args[0][2]
        assert "(on Sep 14, 2026)" in html
        assert "T08:30" not in html

    def test_unparseable_expiry_passes_through(self):
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_linkedin_token_expiring_email("u@e.com", 4, "next Tuesday")
        assert "(on next Tuesday)" in dispatch.call_args[0][2]

    def test_unknown_expiry_avoids_a_number(self):
        from cqc_lem.utilities.email import send_linkedin_token_expiring_email
        with patch(f"{_EMAIL}._dispatch_email", return_value=True) as dispatch:
            send_linkedin_token_expiring_email("u@e.com", None)
        html = dispatch.call_args[0][2]
        assert "expires in" not in html
        assert "needs renewing" in html
