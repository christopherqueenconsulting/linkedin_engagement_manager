"""CMA analytics token watcher, sweep summary line, and scope list (#2698)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin.member_post_analytics import log_member_post_analytics_summary
from cqc_lem.utilities.linkedin.token_refresh import resolve_member_analytics_token_status
from cqc_lem.utilities.notifications import notify_member_analytics_token_expiring

_DB = "cqc_lem.utilities.db"


def _info(days_left: float, token: str = "tok") -> dict:
    now = datetime.now(timezone.utc)
    lifetime = 60 * 86400
    return {"access_token": token,
            "access_token_created_at": now - timedelta(seconds=lifetime - days_left * 86400),
            "access_token_expires_in": lifetime}


class TestStatus:
    @pytest.mark.parametrize("days_left,tier", [(60, None), (29.5, 30), (8.5, 30), (6.5, 7),
                                                (-2, 7)])
    def test_thresholds(self, days_left, tier):
        with patch(f"{_DB}.get_member_analytics_token_info", return_value=_info(days_left)):
            status = resolve_member_analytics_token_status(1)
        assert status["warning_tier"] == tier
        assert status["is_expired"] is (days_left < 0)

    def test_unreadable_expiry_is_unknown_not_expired(self):
        info = {"access_token": "tok", "access_token_created_at": None,
                "access_token_expires_in": None}
        with patch(f"{_DB}.get_member_analytics_token_info", return_value=info):
            status = resolve_member_analytics_token_status(1)
        assert status["connected"] is True
        assert status["days_remaining"] is None
        assert status["is_expired"] is False
        assert status["warning_tier"] is None

    def test_not_connected(self):
        with patch(f"{_DB}.get_member_analytics_token_info", return_value=None):
            assert resolve_member_analytics_token_status(1)["connected"] is False


class TestNotifyDedupe:
    def _redis(self, claims_ok: list[bool]):
        client = MagicMock()
        client.set.side_effect = claims_ok
        return client

    def test_sends_when_both_claims_win(self):
        client = self._redis([True, True])
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=client), \
             patch("cqc_lem.utilities.notifications.get_user_email", return_value="a@b.c"), \
             patch("cqc_lem.utilities.email.send_member_analytics_token_expiring_email",
                   return_value=True) as send:
            assert notify_member_analytics_token_expiring(1, 20, "2027-01-03T12:34:00+00:00", 30)
        send.assert_called_once()
        assert "2027-01-03" in client.set.call_args_list[0][0][0]

    def test_tier_already_sent_is_skipped(self):
        client = self._redis([False])
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=client), \
             patch("cqc_lem.utilities.email.send_member_analytics_token_expiring_email") as send:
            assert notify_member_analytics_token_expiring(1, 20, "2027-01-03", 30) is False
        send.assert_not_called()

    def test_daily_cap_releases_tier_claim(self):
        client = self._redis([True, False])
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=client), \
             patch("cqc_lem.utilities.email.send_member_analytics_token_expiring_email") as send:
            assert notify_member_analytics_token_expiring(1, 5, "2027-01-03", 7) is False
        send.assert_not_called()
        client.delete.assert_called_once()

    def test_failed_send_releases_claims(self):
        client = self._redis([True, True])
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=client), \
             patch("cqc_lem.utilities.notifications.get_user_email", return_value="a@b.c"), \
             patch("cqc_lem.utilities.email.send_member_analytics_token_expiring_email",
                   return_value=False):
            assert notify_member_analytics_token_expiring(1, 5, "2027-01-03", 7) is False
        assert client.delete.call_count == 2

    def test_redis_outage_fails_open(self):
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   side_effect=RuntimeError("down")), \
             patch("cqc_lem.utilities.notifications.get_user_email", return_value="a@b.c"), \
             patch("cqc_lem.utilities.email.send_member_analytics_token_expiring_email",
                   return_value=True):
            assert notify_member_analytics_token_expiring(1, 5, None, 7) is True


class TestBeatTask:
    def _run(self, status):
        from cqc_lem.app.run_scheduler import auto_watch_member_analytics_tokens
        with patch("cqc_lem.utilities.env_constants.LI_MEMBER_POST_ANALYTICS_USER_IDS", {1}), \
             patch("cqc_lem.utilities.linkedin.member_post_analytics."
                   "member_post_analytics_connection_available_for_user", return_value=True), \
             patch("cqc_lem.utilities.linkedin.token_refresh."
                   "resolve_member_analytics_token_status", return_value=status), \
             patch("cqc_lem.utilities.notifications.notify_member_analytics_token_expiring",
                   return_value=True) as notify:
            return auto_watch_member_analytics_tokens(), notify

    def test_emails_inside_window(self):
        result, notify = self._run({"connected": True, "warning_tier": 30, "days_remaining": 20,
                                    "token_expiry_date": "2027-01-03"})
        notify.assert_called_once_with(1, 20, "2027-01-03", 30)
        assert "emailed 1" in result

    def test_healthy_or_unknown_sends_nothing(self):
        result, notify = self._run({"connected": True, "warning_tier": None,
                                    "days_remaining": None, "token_expiry_date": None})
        notify.assert_not_called()
        assert "emailed 0" in result

    def test_scheduled_daily_after_main_beat(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["watch-member-analytics-tokens"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_watch_member_analytics_tokens"
        assert entry["schedule"].hour == {8} and entry["schedule"].minute == {35}


def test_summary_line_carries_counts_and_no_secrets():
    with patch("cqc_lem.utilities.linkedin.member_post_analytics.log_info") as log:
        log_member_post_analytics_summary(1, 3, 2, 12, 1)
    kwargs = log.call_args.kwargs
    assert kwargs["posts_read"] == 3 and kwargs["posts_captured"] == 2
    assert kwargs["metrics_read"] == 12 and kwargs["failures"] == 1
    assert kwargs["calls_max"] == 15
    assert not any("token" in k for k in kwargs)
