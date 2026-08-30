"""An account-level invite wall is a detected state, not a failed selector (#1733/#1732).

Twenty invites failed in one day with `NO_CONNECT_BUTTON_MESSAGE`, which reads as "this profile
offers no Connect option". A weekly-invitation ceiling reads identically on EVERY profile, so that
grading sends an operator hunting a selector that is fine while the scanner re-dispatches the whole
queue into the same wall — and each of those is a full automated profile visit from the user's
session, spending nothing against `max_invites_per_day` because that cap counts successful sends.

So: a wall LinkedIn NAMES holds the lane and defers the row; a route that merely missed counts
toward a miss streak that holds the lane after three. Neither is ever `pause_automation`, which
would stop commenting, DMs, the feed walk and the newsletter over an invite quota.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"
_RL = "cqc_lem.utilities.linkedin.rate_limit"

_LIMIT_COPY = ("You've reached the weekly invitation limit. Try again next week, or grow your "
               "network by following people.")
_RESTRICTED_COPY = "We've restricted your account from sending invitations."
_ORDINARY_COPY = "Jane Doe · 2nd Head of Platform at Acme Follow Message More Pending"


def _driver_saying(text, raises=False):
    driver = MagicMock()
    if raises:
        driver.find_elements.side_effect = RuntimeError("session gone")
        return driver
    element = MagicMock()
    element.text = text
    driver.find_elements.side_effect = lambda by, sel: [element] if sel == "main" else []
    return driver


class TestRestrictionIsReadFromThePagesOwnWords:
    def test_the_weekly_limit_copy_is_named(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import INVITE_LIMIT_REACHED_MESSAGE
        assert ra._invite_restriction_reason(_driver_saying(_LIMIT_COPY)) \
            == INVITE_LIMIT_REACHED_MESSAGE

    def test_a_restriction_notice_outranks_a_limit_notice(self):
        # A restriction page routinely also mentions invitations, and "restricted" is the heavier
        # operator action, so it must not be reported as a mere quota.
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ACCOUNT_RESTRICTED_MESSAGE
        both = f"{_RESTRICTED_COPY} {_LIMIT_COPY}"
        assert ra._invite_restriction_reason(_driver_saying(both)) == ACCOUNT_RESTRICTED_MESSAGE

    def test_an_ordinary_profile_names_no_wall(self):
        from cqc_lem.app.engagement import invites as ra
        assert ra._invite_restriction_reason(_driver_saying(_ORDINARY_COPY)) is None

    def test_a_page_that_cannot_be_read_claims_nothing(self):
        # A restriction is a CLAIM and a claim needs evidence. An unreadable page must fall through
        # to the ordinary miss, never manufacture an account-wide hold out of a failed read.
        from cqc_lem.app.engagement import invites as ra
        assert ra._invite_restriction_reason(_driver_saying("", raises=True)) is None

    def test_a_blank_page_claims_nothing_either(self):
        from cqc_lem.app.engagement import invites as ra
        assert ra._invite_restriction_reason(_driver_saying("   ")) is None

    def test_the_probes_vocabulary_and_productions_agree(self):
        # The probe imports production, never the other way round, so the two copies of this
        # vocabulary can only be kept honest by asserting they answer the same way.
        import importlib.util
        import pathlib

        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ACCOUNT_RESTRICTED_MESSAGE, INVITE_LIMIT_REACHED_MESSAGE

        path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "linkedin_live_validation.py"
        spec = importlib.util.spec_from_file_location("_lem_probe", path)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        pairs = ((_LIMIT_COPY, "weekly_limit", INVITE_LIMIT_REACHED_MESSAGE),
                 (_RESTRICTED_COPY, "restricted", ACCOUNT_RESTRICTED_MESSAGE),
                 (_ORDINARY_COPY, "", None))
        for copy, signal, message in pairs:
            assert probe.invite_limit_signal(copy) == signal
            assert ra._invite_restriction_reason(_driver_saying(copy)) == message


class TestARestrictionHoldsTheLaneAndDefersTheRow:
    def _invite(self, restriction):
        from cqc_lem.app.engagement import invites as ra
        driver = MagicMock()
        driver.current_url = "about:blank"
        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(driver, MagicMock())), \
             patch(f"{_INV}.login_to_linkedin"), \
             patch(f"{_INV}._profile_is_first_degree", return_value=False), \
             patch(f"{_INV}._open_connect_invite_dialog", return_value=(False, restriction)), \
             patch(f"{_INV}.hold_invites") as hold, \
             patch(f"{_INV}.record_invite_dialog_miss") as miss, \
             patch(f"{_INV}.insert_new_log") as insert_log, \
             patch(f"{_INV}.log_error") as log_error, \
             patch(f"{_INV}.quit_gracefully"):
            sent, reason = ra.invite_to_connect_now(1, "https://www.linkedin.com/in/jane/")
        return sent, reason, hold, miss, insert_log, log_error

    def test_a_named_wall_holds_the_lane_and_is_logged_once(self):
        from cqc_lem.utilities.db import INVITE_LIMIT_REACHED_MESSAGE
        sent, reason, hold, miss, insert_log, log_error = self._invite(INVITE_LIMIT_REACHED_MESSAGE)

        assert sent is False and reason == INVITE_LIMIT_REACHED_MESSAGE
        hold.assert_called_once()
        assert hold.call_args.kwargs["reason"] == INVITE_LIMIT_REACHED_MESSAGE
        miss.assert_not_called()          # a wall is not a selector miss
        insert_log.assert_called_once()   # exactly one row: no second grouped issue (#1038)
        assert insert_log.call_args.kwargs["message"] == INVITE_LIMIT_REACHED_MESSAGE
        log_error.assert_not_called()

    def test_an_ordinary_miss_counts_the_streak_and_holds_nothing(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        sent, reason, hold, miss, insert_log, _err = self._invite(None)

        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        hold.assert_not_called()
        miss.assert_called_once_with(1)
        assert insert_log.call_args.kwargs["message"] == NO_CONNECT_BUTTON_MESSAGE


class TestTheHoldIsCheckedBeforeAnyChromeSessionOpens:
    def test_send_connection_request_defers_without_opening_a_browser(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ConnectionRequestStatus
        # The task imports these at call time from the db facade, so that is where they bind.
        with patch("cqc_lem.utilities.db.get_connection_request",
                   return_value={"id": 7, "user_id": 1, "status": ConnectionRequestStatus.APPROVED,
                                 "recipient_profile_url": "https://www.linkedin.com/in/jane/",
                                 "message": None}), \
             patch(f"{_INV}.is_invites_held", return_value=True), \
             patch(f"{_INV}.invite_hold_reason", return_value="weekly limit"), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as update, \
             patch(f"{_INV}.get_driver_wait_pair") as driver_pair, \
             patch(f"{_INV}.log_debug"):
            result = ra.send_connection_request(7)

        assert "deferred" in result
        # Deferred to APPROVED so the next scan picks it up — nothing was attempted, nothing failed.
        assert update.call_args.args[1] == ConnectionRequestStatus.APPROVED
        driver_pair.assert_not_called()   # no Chrome slot spent on a wall we already know about

    def test_the_roster_ladder_hands_the_target_back_instead_of_burning_its_one_shot(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.platform.db.enums import ConnectStatus
        from cqc_lem.utilities.db import INVITE_LIMIT_REACHED_MESSAGE
        with patch(f"{_INV}.invite_to_connect_now",
                   return_value=(False, INVITE_LIMIT_REACHED_MESSAGE)), \
             patch(f"{_INV}.set_target_connect_status") as status, \
             patch(f"{_INV}.log_debug"):
            ra.send_roster_connect_invite(1, "https://www.linkedin.com/in/jane/")

        assert status.call_args.args[2] == ConnectStatus.NEEDS_CONNECTION

    def test_an_ordinary_failure_is_still_terminal_for_the_roster(self):
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.platform.db.enums import ConnectStatus
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        with patch(f"{_INV}.invite_to_connect_now",
                   return_value=(False, NO_CONNECT_BUTTON_MESSAGE)), \
             patch(f"{_INV}.set_target_connect_status") as status, \
             patch(f"{_INV}.log_debug"):
            ra.send_roster_connect_invite(1, "https://www.linkedin.com/in/jane/")

        assert status.call_args.args[2] == ConnectStatus.FAILED


class TestTheMissStreakBreaker:
    def _client(self, streak):
        client = MagicMock()
        client.incr.return_value = streak
        return client

    def test_a_streak_below_the_limit_holds_nothing(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch(f"{_RL}._redis_client", return_value=self._client(2)), \
             patch(f"{_RL}.hold_invites") as hold:
            assert rl.record_invite_dialog_miss(1) == 2
        hold.assert_not_called()

    def test_the_limit_holds_the_lane_for_the_day_not_the_week(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch(f"{_RL}._redis_client", return_value=self._client(3)), \
             patch(f"{_RL}.is_invites_held", return_value=False), \
             patch(f"{_RL}.hold_invites") as hold:
            assert rl.record_invite_dialog_miss(1) == 3
        hold.assert_called_once()
        assert hold.call_args.args[1] == rl.INVITE_MISS_HOLD_SECONDS
        assert hold.call_args.args[1] < rl.INVITE_HOLD_DEFAULT_SECONDS

    def test_no_redis_counts_nothing_and_holds_nothing(self):
        # Fails OPEN, like every other gate in this module: the worst case is one wasted session
        # that re-detects the wall, which self-heals. Failing closed would freeze a healthy account.
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch(f"{_RL}._redis_client", return_value=None), \
             patch(f"{_RL}.hold_invites") as hold:
            assert rl.record_invite_dialog_miss(1) == 0
        hold.assert_not_called()

    def test_is_invites_held_fails_open_without_redis(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch(f"{_RL}._redis_client", return_value=None):
            assert rl.is_invites_held(1) is False


class TestTheWithdrawalLaneIsDeliberatelyNotHeld:
    def test_clean_stale_invites_never_asks_whether_invites_are_held(self):
        # Withdrawing is the CURE — it lowers the outstanding count the ceiling counts. Gating it on
        # the hold would guarantee a walled account never recovers.
        import inspect

        from cqc_lem.app.engagement import invites as ra
        source = inspect.getsource(ra.clean_stale_invites)
        assert "is_invites_held" not in source
