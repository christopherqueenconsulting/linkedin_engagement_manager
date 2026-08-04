"""The note half of the invite send path (issues #573 + #1039).

`Failed to add a note to connection request` paged the error cron, and behind that error the whole
invite was abandoned with the Connect dialog already open — LinkedIn hides the note affordance once
a free account's personalized-invite quota is spent, and an AI-written note routinely carries emoji
that ChromeDriver's send_keys refuses to type. Both now degrade to a bare invite; only a dialog with
no Send button at all is still an error.

#1039: degrading was not enough. The miss still went out as a WARNING carrying `exc=`, so a routine
quota-spent invite filed a fingerprinted `TimeoutException: Finding Add a Note Button` defect for
working behaviour. An ABSENT affordance is now a DEBUG no-op — graded against the bare-send control
the dialog still shows — and only a failure AFTER the affordance answered warns.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import TimeoutException

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"

_NOTE_XPATH = '//button[contains(@aria-label,"Add a note")]'
_TEXTAREA_XPATH = '//textarea[@id="custom-message"]'
_SEND_XPATH = '//button[contains(@aria-label,"Send invitation")]'
_SEND_BARE_XPATH = '//button[contains(@aria-label,"Send without a note")]'

_NOTE_DIALOG = {_NOTE_XPATH, _TEXTAREA_XPATH, _SEND_XPATH}


def _clicker(found: set[str], box: MagicMock = None):
    """A click_element_wait_retry stand-in that only 'finds' the xpaths in `found`."""

    def click(driver, wait, xpath, label, **kwargs):
        if xpath not in found:
            raise Exception(f"no element for {xpath}")
        return box if (box is not None and xpath == _TEXTAREA_XPATH) else MagicMock()

    return MagicMock(side_effect=click)


def _finder(found: set[str]):
    """find_first stand-in: the first locator whose xpath is on the page wins, else None."""

    def find(driver, wait, locators, label, **kwargs):
        return MagicMock() if any(value in found for _by, value in locators) else None

    return MagicMock(side_effect=find)


def _first_clicker(found: set[str]):
    """click_first stand-in — raises when nothing matches and the caller required a hit."""

    def click(driver, wait, locators, label, **kwargs):
        if any(value in found for _by, value in locators):
            return MagicMock()
        if kwargs.get("required", True):
            raise TimeoutException(label)
        return None

    return MagicMock(side_effect=click)


class _Result:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _invite(found: set[str], message: str = None, box: MagicMock = None, refined: str = "short note"):
    from cqc_lem.app import run_automation as ra
    click = _clicker(found, box)
    with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
         patch(f"{_RA}.login_to_linkedin"), \
         patch(f"{_RA}._profile_is_first_degree", return_value=False), \
         patch(f"{_RA}._open_connect_invite_dialog", return_value=True), \
         patch(f"{_RA}.click_element_wait_retry", click), \
         patch(f"{_RA}.find_first", _finder(found)) as find_first, \
         patch(f"{_RA}.click_first", _first_clicker(found)) as click_first, \
         patch(f"{_RA}.get_ai_message_refinement", return_value=refined) as refine, \
         patch(f"{_RA}.time.sleep"), \
         patch(f"{_RA}.log_error") as log_error, \
         patch(f"{_RA}.log_warning") as log_warning, \
         patch(f"{_RA}.log_debug") as log_debug, \
         patch(f"{_RA}.insert_new_log") as insert_log, \
         patch(f"{_RA}.record_action") as record_action, \
         patch(f"{_RA}.quit_gracefully"):
        sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane", message)
    return _Result(sent=sent, reason=reason, click=click, find_first=find_first,
                   click_first=click_first, insert_log=insert_log, log_error=log_error,
                   log_warning=log_warning, log_debug=log_debug, refine=refine,
                   record_action=record_action)


def _clicked(click) -> list[str]:
    return [call.args[2] for call in click.call_args_list]


def _messages(logger) -> list[str]:
    return [call.args[0] for call in logger.call_args_list]


class TestNoteIsBestEffort:
    def test_a_missing_note_affordance_still_sends_the_invite(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        # LinkedIn offers only the bare dialog once the personalized-invite quota is spent.
        r = _invite(found={_SEND_BARE_XPATH}, message="hi jane")
        assert r.sent is True and r.reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _SEND_BARE_XPATH in _clicked(r.click)
        assert r.insert_log.call_args.kwargs["message"] == CONNECTION_REQUEST_SENT_MESSAGE
        r.record_action.assert_called_once()  # a bare invite still spends the account-level budget
        r.log_error.assert_not_called()

    def test_a_missing_note_affordance_never_warns(self):
        """#1039: this is the documented quota-spent fallback, and a repeated warning would file a
        grouped PostHog issue — and a GitHub issue off it — for working behaviour."""
        r = _invite(found={_SEND_BARE_XPATH}, message="hi jane")
        r.log_warning.assert_not_called()
        assert any("quota" in message for message in _messages(r.log_debug))

    def test_the_quota_reading_is_grounded_in_the_dialogs_own_bare_send_control(self):
        """The absence is only called quota-spent because the dialog is still showing the control
        that means 'no note on offer here' — never assumed from the missing note button alone."""
        r = _invite(found={_SEND_BARE_XPATH}, message="hi jane")
        looked_up = [call.args[3] for call in r.find_first.call_args_list]
        assert looked_up[:2] == ["Add a note button", "Send without a note"]

    def test_a_dialog_showing_neither_control_is_still_not_a_warning(self):
        """The lost invite it costs us is what _submit_connect_invite logs as an ERROR; warning here
        too would file a second grouped issue for the same event (the #1038 fault)."""
        from cqc_lem.utilities.db import INVITE_NOT_SENT_MESSAGE
        r = _invite(found=set(), message="hi jane")
        assert r.sent is False and r.reason == INVITE_NOT_SENT_MESSAGE
        r.log_warning.assert_not_called()
        r.log_error.assert_called_once()  # exactly one signal for the one lost invite
        assert any("no longer showing its own controls" in m for m in _messages(r.log_debug))

    def test_a_note_that_cannot_be_typed_still_sends_the_invite(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        box = MagicMock()
        box.send_keys.side_effect = Exception("ChromeDriver only supports characters in the BMP")
        r = _invite(found=_NOTE_DIALOG | {_SEND_BARE_XPATH}, message="hi jane", box=box)
        assert r.sent is True and r.reason == CONNECTION_REQUEST_SENT_MESSAGE
        r.log_error.assert_not_called()
        r.log_warning.assert_called_once()

    def test_a_failure_after_the_affordance_answered_still_warns_with_the_exception(self):
        """The note button was there and the textarea was not: that is drift or a broken dialog, not
        the quota fallback, so it keeps the WARNING + `exc=` that makes it greppable."""
        r = _invite(found={_NOTE_XPATH, _SEND_BARE_XPATH}, message="hi jane")
        r.log_warning.assert_called_once()
        assert isinstance(r.log_warning.call_args.kwargs.get("exc"), Exception)
        assert r.sent is True  # and the invite still goes out bare

    def test_the_note_is_stripped_of_non_bmp_characters_before_typing(self):
        box = MagicMock()
        _invite(found=_NOTE_DIALOG, message="hi jane \U0001F600", box=box)
        typed = box.send_keys.call_args.args[0]
        assert "\U0001F600" not in typed and typed.startswith("hi jane")

    def test_an_over_long_note_is_refined_and_capped(self):
        from cqc_lem.utilities.db import CONNECT_NOTE_MAX_CHARS
        box = MagicMock()
        r = _invite(found=_NOTE_DIALOG, message="x" * 400, box=box, refined="y" * 500)
        assert r.refine.call_args.args[1] == CONNECT_NOTE_MAX_CHARS
        # Refinement can come back long; the textarea's own maxlength would truncate it silently.
        assert len(box.send_keys.call_args.args[0]) == CONNECT_NOTE_MAX_CHARS


class TestNoteHappyPath:
    def test_a_note_is_typed_and_the_invite_sent(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        box = MagicMock()
        r = _invite(found=_NOTE_DIALOG, message="hi jane", box=box)
        assert r.sent is True and r.reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _clicked(r.click) == [_TEXTAREA_XPATH, _SEND_XPATH]
        assert [call.args[3] for call in r.click_first.call_args_list] == ["Add a note button"]
        box.clear.assert_called_once()
        box.send_keys.assert_called_once_with("hi jane")
        r.refine.assert_not_called()  # already under the limit
        r.log_error.assert_not_called()
        r.log_warning.assert_not_called()

    def test_an_invite_with_no_note_never_looks_for_the_note_composer(self):
        r = _invite(found={_SEND_BARE_XPATH})
        assert _clicked(r.click) == [_SEND_BARE_XPATH]
        r.find_first.assert_not_called()
        r.click_first.assert_not_called()


class TestSendFailureIsStillAnError:
    def test_a_dialog_with_no_send_button_reports_a_named_failure(self):
        from cqc_lem.utilities.db import INVITE_NOT_SENT_MESSAGE
        box = MagicMock()
        r = _invite(found=_NOTE_DIALOG - {_SEND_XPATH}, message="hi jane", box=box)
        assert r.sent is False and r.reason == INVITE_NOT_SENT_MESSAGE
        assert r.insert_log.call_args.kwargs["message"] == INVITE_NOT_SENT_MESSAGE
        r.record_action.assert_not_called()  # nothing was sent, so nothing is spent
        # Both Send labels are tried before giving up — the dialog can be in either state.
        assert _clicked(r.click)[-2:] == [_SEND_XPATH, _SEND_BARE_XPATH]
        r.log_error.assert_called_once()
        # exc= is what makes it a fingerprinted PostHog issue rather than a log line nobody reads.
        assert isinstance(r.log_error.call_args.kwargs.get("exc"), Exception)
