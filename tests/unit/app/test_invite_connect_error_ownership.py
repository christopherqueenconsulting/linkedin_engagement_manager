"""One lost invite must file ONE issue, not two (issues #1038 / #1042).

The 2026-08-03 rail hazard (#1012) storm surfaced this: `_submit_connect_invite` logs the lost
invite at ERROR with `exc=` — the deliberate #573 error, owned by the step that saw it — and then
every task wrapper re-logged the SAME reason at WARNING. Under the escalation contract a repeated
warning is promoted to ERROR and files its own grouped `$exception`, so a single failed invite
arrived as two PostHog issues and two auto-filed GitHub issues (#1038 for the TimeoutException,
#1042 for the restatement). The wrappers now log the reason at DEBUG: the owning step keeps the
severity, the wrappers keep the breadcrumb.
"""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.db import (ALREADY_CONNECTED_MESSAGE, INVITE_NOT_SENT_MESSAGE,
                                  NO_CONNECT_BUTTON_MESSAGE)

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"

# Every reason invite_to_connect_now can hand back with sent=False. Each is already logged by the
# step that owns it, so none of them may re-emit from a wrapper.
_OWNED_REASONS = [ALREADY_CONNECTED_MESSAGE, NO_CONNECT_BUTTON_MESSAGE, INVITE_NOT_SENT_MESSAGE,
                  "Error while inviting to connect: boom"]


class TestInviteToConnectWrapper:
    @pytest.mark.parametrize("reason", _OWNED_REASONS)
    def test_a_failure_the_core_already_logged_is_not_re_warned(self, reason):
        from cqc_lem.app import run_automation as ra
        with patch(f"{_RA}.invite_to_connect_now", return_value=(False, reason)), \
             patch(f"{_RA}.log_warning") as log_warning, \
             patch(f"{_RA}.log_error") as log_error, \
             patch(f"{_RA}.log_debug") as log_debug:
            out = ra.invite_to_connect(1, "https://x/in/jane")

        assert reason in out
        log_warning.assert_not_called()
        log_error.assert_not_called()
        log_debug.assert_called_once()  # the breadcrumb stays, at a level that never escalates
        assert log_debug.call_args.kwargs["user_id"] == 1


class TestRosterWrapper:
    def test_a_failed_roster_invite_badges_the_row_without_re_warning(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.db import ConnectStatus
        with patch(f"{_RA}.invite_to_connect_now", return_value=(False, INVITE_NOT_SENT_MESSAGE)), \
             patch(f"{_RA}.set_target_connect_status") as status, \
             patch(f"{_RA}.log_warning") as log_warning, \
             patch(f"{_RA}.log_debug") as log_debug:
            out = ra.send_roster_connect_invite(1, "https://x/in/jane")

        status.assert_called_once_with(1, "https://x/in/jane", ConnectStatus.FAILED)
        assert INVITE_NOT_SENT_MESSAGE in out
        log_warning.assert_not_called()
        log_debug.assert_called_once()


class TestProactiveWrapper:
    def test_a_failed_request_stores_the_reason_without_re_warning(self):
        from cqc_lem.app import run_automation as ra
        from cqc_lem.utilities.db import ConnectionRequestStatus
        req = {"id": 3, "user_id": 1, "recipient_profile_url": "https://x/in/jane",
               "message": "hi jane", "status": "approved"}
        with patch("cqc_lem.utilities.db.get_connection_request", return_value=req), \
             patch("cqc_lem.utilities.db.count_invites_sent_today", return_value=0), \
             patch(f"{_RA}.get_engagement_preferences", return_value={"max_invites_per_day": 10}), \
             patch(f"{_RA}.invite_to_connect_now", return_value=(False, INVITE_NOT_SENT_MESSAGE)), \
             patch("cqc_lem.utilities.db.update_connection_request_status") as upd, \
             patch(f"{_RA}.log_warning") as log_warning, \
             patch(f"{_RA}.log_debug") as log_debug:
            ra.send_connection_request(3)

        # The failure is still recorded where it is actionable — on the request row.
        upd.assert_called_once_with(3, ConnectionRequestStatus.FAILED,
                                    failure_reason=INVITE_NOT_SENT_MESSAGE)
        log_warning.assert_not_called()
        log_debug.assert_called_once()


class TestOneLostInviteFilesOneIssue:
    def test_the_dialog_with_no_send_button_escalates_exactly_once(self):
        """End to end through the real core: the ONE escalating log is the owner's, with exc=."""
        from cqc_lem.app import run_automation as ra

        def click(driver, wait, xpath, label, **kwargs):
            raise Exception(f"no element for {xpath}")  # no Send affordance in either state

        with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
             patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
             patch(f"{_RA}.login_to_linkedin"), \
             patch(f"{_RA}._profile_is_first_degree", return_value=False), \
             patch(f"{_RA}._open_connect_invite_dialog", return_value=True), \
             patch(f"{_RA}.click_element_wait_retry", side_effect=click), \
             patch(f"{_RA}.time.sleep"), \
             patch(f"{_RA}.insert_new_log"), \
             patch(f"{_RA}.record_action"), \
             patch(f"{_RA}.quit_gracefully"), \
             patch(f"{_RA}.log_warning") as log_warning, \
             patch(f"{_RA}.log_error") as log_error:
            out = ra.invite_to_connect(1, "https://x/in/jane")

        assert INVITE_NOT_SENT_MESSAGE in out
        log_error.assert_called_once()
        assert isinstance(log_error.call_args.kwargs.get("exc"), Exception)
        log_warning.assert_not_called()  # no second fingerprint for the same lost invite
