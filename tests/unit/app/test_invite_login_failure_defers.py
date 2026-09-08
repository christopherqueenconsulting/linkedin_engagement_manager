"""A login failure inside `invite_to_connect_now` must defer, not burn a target's attempts (#1924).

Before this fix, a `TimeoutException` raised by `login_to_linkedin` (e.g. an unrecognized
challenge/checkpoint page, #1908) fell into `invite_to_connect_now`'s generic
`except Exception` handler and came back as an ordinary per-target failure reason
("Error while inviting to connect: Message: Finding Username Field"). `send_connection_request`
then charged it against the connection request's attempt ceiling like a real per-target miss,
and the Nth occurrence escalated a `log_warning` ("... exhausted its attempts; giving up: ...")
into a grouped PostHog `$exception` — even though nothing was ever learned about THAT target,
only that this session's login was broken. Login failures must convert to `LinkedInRateLimited`
so every caller (`invite_to_connect`, `send_roster_connect_invite`, `send_connection_request`)
defers exactly like it already does for a 429.

The plain `RuntimeError` `login_to_linkedin` raises when every automated way to clear a login
challenge failed (`helper._handle_challenge`, issue #1920) is the SAME class of fact — an
account-level login failure, not a per-target one — and hit the same generic `except Exception`
handler until this fix. `_handle_challenge` already records a per-account cooldown right before
raising, so the failure degrades gracefully on its own; the missing piece was this handler
logging a fresh ERROR (and burning the request's attempt ceiling) on every occurrence instead of
deferring like the TimeoutException case above — 79 occurrences for one account in under 5h
(#1918).
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common import TimeoutException

from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

pytestmark = pytest.mark.unit

# The connect rail lives in its own module (#1154); patches must bind THERE.
_INV = "cqc_lem.app.engagement.invites"


class TestLoginFailureDuringInvite:
    def test_login_timeout_raises_rate_limited_not_a_target_failure(self):
        from cqc_lem.app.engagement import invites as ra

        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin",
                   side_effect=TimeoutException("Finding Username Field")), \
             patch(f"{_INV}.quit_gracefully") as quit_gracefully, \
             patch(f"{_INV}.insert_new_log") as insert_new_log, \
             patch(f"{_INV}.log_error") as log_error:
            with pytest.raises(LinkedInRateLimited):
                ra.invite_to_connect_now(1, "https://x/in/jane", "hi jane")

        # The driver is still released...
        quit_gracefully.assert_called_once()
        # ...but nothing is recorded as a failure ABOUT the target, and the generic error path
        # (which would file a second, misleading $exception) never runs.
        insert_new_log.assert_not_called()
        log_error.assert_not_called()

    def test_reactive_wrapper_defers_on_login_timeout(self):
        # invite_to_connect (reactive) already defers silently on LinkedInRateLimited — this
        # proves a login timeout now reaches that same path end to end.
        from cqc_lem.app.engagement import invites as ra

        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin",
                   side_effect=TimeoutException("Finding Username Field")), \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.insert_new_log"), \
             patch(f"{_INV}.log_debug") as log_debug, \
             patch(f"{_INV}.log_warning") as log_warning:
            out = ra.invite_to_connect(1, "https://x/in/jane")

        assert "throttled" in out.lower()
        log_warning.assert_not_called()
        log_debug.assert_called_once()

    def test_proactive_send_defers_without_burning_the_attempt_ceiling(self):
        # send_connection_request must treat this exactly like `test_defers_when_throttled` in
        # test_connection_requests.py: deferred back to APPROVED, attempts untouched.
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ConnectionRequestStatus

        req = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
               "message": "hi jane", "status": "approved", "recipient_email": None}

        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin",
                   side_effect=TimeoutException("Finding Username Field")), \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.insert_new_log"), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec, \
             patch(f"{_INV}.log_warning") as log_warning:
            out = ra.send_connection_request(3)

        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred, not failed
        rec.assert_not_called()  # nothing learned about this target — attempts must not move
        log_warning.assert_not_called()  # never reaches the "exhausted its attempts" escalation
        assert "throttled" in out.lower()


class TestUnsolvableChallengeDuringInvite:
    """Same contract as `TestLoginFailureDuringInvite`.

    Covers the unsolvable-challenge RuntimeError (#1918) instead of a login timeout (#1924).
    """

    def test_unsolvable_challenge_raises_rate_limited_not_a_target_failure(self):
        from cqc_lem.app.engagement import invites as ra

        with patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin",
                   side_effect=RuntimeError(
                       "Unsolvable LinkedIn challenge at post-submit: "
                       "https://www.linkedin.com/checkpoint/challenge/x")), \
             patch(f"{_INV}.quit_gracefully") as quit_gracefully, \
             patch(f"{_INV}.insert_new_log") as insert_new_log, \
             patch(f"{_INV}.log_error") as log_error:
            with pytest.raises(LinkedInRateLimited):
                ra.invite_to_connect_now(1, "https://x/in/jane", "hi jane")

        # The driver is still released...
        quit_gracefully.assert_called_once()
        # ...but nothing is recorded as a failure ABOUT the target, and the generic error path
        # (which would file a fresh $exception on every occurrence) never runs.
        insert_new_log.assert_not_called()
        log_error.assert_not_called()

    def test_proactive_send_defers_without_burning_the_attempt_ceiling(self):
        # send_connection_request must treat this exactly like the TimeoutException case: deferred
        # back to APPROVED, attempts untouched.
        from cqc_lem.app.engagement import invites as ra
        from cqc_lem.utilities.db import ConnectionRequestStatus

        req = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
               "message": "hi jane", "status": "approved", "recipient_email": None}

        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_INV}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_INV}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_INV}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_INV}.login_to_linkedin",
                   side_effect=RuntimeError(
                       "Unsolvable LinkedIn challenge at post-submit: "
                       "https://www.linkedin.com/checkpoint/challenge/x")), \
             patch(f"{_INV}.quit_gracefully"), \
             patch(f"{_INV}.insert_new_log"), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch("cqc_lem.utilities.db.record_connection_request_attempt") as rec, \
             patch(f"{_INV}.log_warning") as log_warning, \
             patch(f"{_INV}.log_error") as log_error:
            out = ra.send_connection_request(3)

        upd.assert_called_once_with(3, ConnectionRequestStatus.APPROVED)  # deferred, not failed
        rec.assert_not_called()  # nothing learned about this target — attempts must not move
        log_warning.assert_not_called()  # never reaches the "exhausted its attempts" escalation
        log_error.assert_not_called()  # no fresh $exception for a failure that already backed off
        assert "throttled" in out.lower()
