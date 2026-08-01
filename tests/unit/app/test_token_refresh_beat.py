"""Unit tests for the daily LinkedIn token renewal beat (issue #600)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_TR = "cqc_lem.utilities.linkedin.token_refresh"
_NOTIFY = "cqc_lem.utilities.notifications"


def _status(**overrides):
    base = {
        "connected": True,
        "token_expiry_date": "2026-09-01T00:00:00+00:00",
        "days_remaining": 10,
        "is_expiring_soon": True,
        "is_expired": False,
        "can_auto_refresh": True,
        "refresh_attempted": True,
        "refresh_succeeded": False,
    }
    base.update(overrides)
    return base


class TestAutoRefreshLinkedInTokens:
    def test_refreshed_user_is_not_emailed(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[1]), \
             patch(f"{_TR}.resolve_token_status",
                   return_value=_status(refresh_succeeded=True, is_expiring_soon=False)), \
             patch(f"{_NOTIFY}.notify_linkedin_token_expiring") as notify:
            result = auto_refresh_linkedin_tokens()
        notify.assert_not_called()
        assert "Refreshed 1" in result and "emailed 0" in result

    def test_unrefreshable_user_is_emailed_with_the_countdown(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[7]), \
             patch(f"{_TR}.resolve_token_status",
                   return_value=_status(can_auto_refresh=False, refresh_attempted=False,
                                        days_remaining=4)), \
             patch(f"{_NOTIFY}.notify_linkedin_token_expiring", return_value=True) as notify:
            result = auto_refresh_linkedin_tokens()
        notify.assert_called_once_with(7, 4, "2026-09-01T00:00:00+00:00")
        assert "emailed 1" in result

    def test_already_expired_user_is_emailed(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[7]), \
             patch(f"{_TR}.resolve_token_status",
                   return_value=_status(is_expired=True, days_remaining=0)), \
             patch(f"{_NOTIFY}.notify_linkedin_token_expiring", return_value=True) as notify:
            auto_refresh_linkedin_tokens()
        assert notify.call_args[0][1] == 0

    def test_healthy_user_is_left_alone(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[1]), \
             patch(f"{_TR}.resolve_token_status",
                   return_value=_status(is_expiring_soon=False, refresh_attempted=False,
                                        days_remaining=52)), \
             patch(f"{_NOTIFY}.notify_linkedin_token_expiring") as notify:
            result = auto_refresh_linkedin_tokens()
        notify.assert_not_called()
        assert "Refreshed 0 and emailed 0 of 1" in result

    def test_one_users_failure_does_not_stop_the_pass(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[1, 2]), \
             patch(f"{_TR}.resolve_token_status",
                   side_effect=[RuntimeError("boom"), _status(refresh_succeeded=True)]), \
             patch(f"{_NOTIFY}.notify_linkedin_token_expiring"):
            result = auto_refresh_linkedin_tokens()
        assert "Refreshed 1 and emailed 0 of 2" in result

    def test_no_connected_users_is_a_no_op(self):
        from cqc_lem.app.run_scheduler import auto_refresh_linkedin_tokens
        with patch(f"{_DB}.get_linkedin_token_user_ids", return_value=[]), \
             patch(f"{_TR}.resolve_token_status") as resolve:
            result = auto_refresh_linkedin_tokens()
        resolve.assert_not_called()
        assert "of 0" in result


class TestBeatSchedule:
    def test_renewal_runs_before_the_missing_session_pass(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["refresh-linkedin-tokens"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_refresh_linkedin_tokens"
        # 8:30 — a token renewed here must not trigger a 9:00 reconnect email.
        assert entry["schedule"].hour == {8}
        assert entry["schedule"].minute == {30}
