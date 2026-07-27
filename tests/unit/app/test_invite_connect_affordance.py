"""The Connect-affordance hunt on the invite send path (issue #571).

`Failed to find more or connect button` paged the error cron for what is an ordinary outcome — a
profile with no Connect option at all (invite already pending, Follow/Message-only, selector drift).
Worse, the old code carried on into the note/send steps with no Connect dialog open, so the real
reason was overwritten by a second, misleading error. These cover both halves.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"

_INVITE_XPATH = '//main//button[contains(@aria-label, "Invite ")]'
_MORE_XPATH = '//main//button[contains(@aria-label,"More actions")]'
_MENU_CONNECT_XPATH = '//main//div[contains(@aria-label,"connect")]'


def _clicker(found: set[str]):
    """A click_element_wait_retry stand-in that only 'finds' the xpaths in `found`."""

    def click(driver, wait, xpath, label, **kwargs):
        if xpath in found:
            return MagicMock()
        raise Exception(f"no element for {xpath}")

    return MagicMock(side_effect=click)


def _invite(found: set[str], message: str = None):
    from cqc_lem.app import run_automation as ra
    click = _clicker(found)
    with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
         patch(f"{_RA}.login_to_linkedin"), \
         patch(f"{_RA}._profile_is_first_degree", return_value=False), \
         patch(f"{_RA}.click_element_wait_retry", click), \
         patch(f"{_RA}.log_error") as log_error, \
         patch(f"{_RA}.log_warning") as log_warning, \
         patch(f"{_RA}.insert_new_log") as insert_log, \
         patch(f"{_RA}.record_action"), \
         patch(f"{_RA}.quit_gracefully"):
        sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane", message)
    return sent, reason, click, insert_log, log_error, log_warning


def _clicked(click) -> list[str]:
    return [call.args[2] for call in click.call_args_list]


class TestNoConnectAffordance:
    def test_a_profile_with_no_connect_option_stops_with_a_named_reason(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        sent, reason, _click, insert_log, _err, _warn = _invite(found=set())
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        insert_log.assert_called_once()
        assert insert_log.call_args.kwargs["message"] == NO_CONNECT_BUTTON_MESSAGE

    def test_it_never_attempts_the_note_or_send_steps(self):
        # With no Connect dialog open those can only fail, and their errors buried the real reason.
        _sent, _reason, click, _log, _err, _warn = _invite(found=set(), message="hi jane")
        attempted = _clicked(click)
        assert attempted == [_INVITE_XPATH, _MORE_XPATH]

    def test_the_miss_is_a_warning_not_an_error(self):
        # It is an expected outcome and it degrades gracefully — it must not page the error cron.
        _sent, _reason, _click, _log, log_error, log_warning = _invite(found=set())
        log_error.assert_not_called()
        log_warning.assert_called_once()
        assert log_warning.call_args.kwargs["user_id"] == 1


class TestConnectAffordanceStillWorks:
    def test_the_direct_connect_button_sends_without_a_note(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        send_xpath = '//button[contains(@aria-label,"Send without a note")]'
        sent, reason, click, _log, log_error, _warn = _invite(found={_INVITE_XPATH, send_xpath})
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _MORE_XPATH not in _clicked(click)  # the overflow is only a fallback
        log_error.assert_not_called()

    def test_the_more_menu_is_the_fallback_when_the_direct_button_is_absent(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        send_xpath = '//button[contains(@aria-label,"Send without a note")]'
        sent, reason, click, _log, _err, log_warning = _invite(
            found={_MORE_XPATH, _MENU_CONNECT_XPATH, send_xpath})
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _clicked(click)[:3] == [_INVITE_XPATH, _MORE_XPATH, _MENU_CONNECT_XPATH]
        log_warning.assert_not_called()

    def test_a_more_menu_without_a_connect_item_is_still_a_miss(self):
        from cqc_lem.utilities.db import NO_CONNECT_BUTTON_MESSAGE
        sent, reason, _click, _log, log_error, _warn = _invite(found={_MORE_XPATH})
        assert sent is False and reason == NO_CONNECT_BUTTON_MESSAGE
        log_error.assert_not_called()
