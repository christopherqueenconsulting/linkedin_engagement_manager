"""`clean_stale_invites` — the daily beat that was a no-op stub while looking operational (#969).

The task half is thin on purpose (the walk lives in `utilities/linkedin/stale_invites.py`), so what
is protected here is the shape that made the stub invisible: EVERY run emits `stale_invite_run`,
including the ones that do nothing, and a run paced (or switched) to zero must never cost a Chrome
session slot.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The connect rail moved to its own module (#1154); patches for it must bind THERE, because that
# is the module whose globals the invite code reads.
_INV = "cqc_lem.app.engagement.invites"


def _plan(allowance=0, status="disabled"):
    return {"allowance": allowance, "status": status, "cap": 10, "withdrawn_today": 0,
            "threshold_days": 21}


class TestCleanStaleInvites:
    def test_zero_allowance_opens_no_browser_and_still_reports(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan()), \
                patch(f"{_INV}.get_driver_wait_pair") as driver_pair, \
                patch(f"{_INV}.track_stale_invite_run") as track:
            result = clean_stale_invites.run(user_id=42)
        driver_pair.assert_not_called()
        assert result["status"] == "disabled"
        track.assert_called_once()
        assert track.call_args[0][0] == 42

    def test_the_off_by_default_skip_is_debug_not_info(self):
        """It is the default, it repeats for every active user every night, and it is working
        behaviour — an INFO line here is one row per user per day saying nothing happened.
        """
        from cqc_lem.app.run_automation import clean_stale_invites
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan()), \
                patch(f"{_INV}.track_stale_invite_run"), \
                patch(f"{_INV}.log_debug") as debug, patch(f"{_INV}.log_info") as info:
            clean_stale_invites.run(user_id=1)
        assert debug.called
        info.assert_not_called()

    def test_a_real_run_walks_the_page_and_quits_the_driver(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        driver = MagicMock()
        report = {"status": "withdrew", "withdrawn": 2}
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan(allowance=3, status="withdrew")), \
                patch(f"{_INV}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
                patch(f"{_INV}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
                patch(f"{_INV}.login_to_linkedin"), \
                patch(f"{_INV}.withdraw_stale_invites", return_value=report), \
                patch(f"{_INV}.quit_gracefully") as quit_driver, \
                patch(f"{_INV}.track_stale_invite_run") as track:
            result = clean_stale_invites.run(user_id=5)
        assert result["withdrawn"] == 2
        quit_driver.assert_called_once_with(driver)
        assert track.call_args[0][1]["status"] == "withdrew"

    def test_a_session_that_never_starts_is_its_own_status(self):
        """'The browser never came up' and 'LinkedIn's markup moved' need different fixes, and an
        escaping exception would emit nothing — indistinguishable from a day paced to zero.
        """
        from cqc_lem.app.run_automation import clean_stale_invites
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan(allowance=3, status="withdrew")), \
                patch(f"{_INV}.get_driver_wait_pair", side_effect=RuntimeError("grid full")), \
                patch(f"{_INV}.log_error"), \
                patch(f"{_INV}.track_stale_invite_run") as track:
            result = clean_stale_invites.run(user_id=5)
        assert result["status"] == "session_failed"
        assert track.call_args[0][1]["status"] == "session_failed"

    def test_the_breaker_opening_mid_run_defers_rather_than_failing(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan(allowance=3, status="withdrew")), \
                patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
                patch(f"{_INV}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
                patch(f"{_INV}.login_to_linkedin", side_effect=LinkedInRateLimited("429")), \
                patch(f"{_INV}.quit_gracefully"), \
                patch(f"{_INV}.log_error") as error, \
                patch(f"{_INV}.track_stale_invite_run"):
            result = clean_stale_invites.run(user_id=5)
        assert result["status"] == "paused"
        error.assert_not_called()

    def test_a_failing_walk_still_reports_and_quits(self):
        from cqc_lem.app.run_automation import clean_stale_invites
        driver = MagicMock()
        with patch(f"{_INV}.plan_withdrawals", return_value=_plan(allowance=3, status="withdrew")), \
                patch(f"{_INV}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
                patch(f"{_INV}.get_user_password_pair_by_id", return_value=("a@b.c", "pw")), \
                patch(f"{_INV}.login_to_linkedin"), \
                patch(f"{_INV}.withdraw_stale_invites", side_effect=RuntimeError("boom")), \
                patch(f"{_INV}.quit_gracefully") as quit_driver, \
                patch(f"{_INV}.log_error") as error, \
                patch(f"{_INV}.track_stale_invite_run") as track:
            result = clean_stale_invites.run(user_id=5)
        assert result["status"] == "failed"
        assert error.called
        quit_driver.assert_called_once_with(driver)
        track.assert_called_once()
