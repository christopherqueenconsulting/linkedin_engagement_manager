"""The note half of the invite send path (issue #573).

`Failed to add a note to connection request` paged the error cron, and behind that error the whole
invite was abandoned with the Connect dialog already open — LinkedIn hides the note affordance once
a free account's personalized-invite quota is spent, and an AI-written note routinely carries emoji
that ChromeDriver's send_keys refuses to type. Both now degrade to a bare invite; only a dialog with
no Send button at all is still an error.
"""

from unittest.mock import MagicMock, patch

import pytest

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


def _invite(found: set[str], message: str = None, box: MagicMock = None, refined: str = "short note"):
    from cqc_lem.app import run_automation as ra
    click = _clicker(found, box)
    with patch(f"{_RA}.get_user_password_pair_by_id", return_value=("e@x", "pw")), \
         patch(f"{_RA}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
         patch(f"{_RA}.login_to_linkedin"), \
         patch(f"{_RA}._profile_is_first_degree", return_value=False), \
         patch(f"{_RA}._open_connect_invite_dialog", return_value=True), \
         patch(f"{_RA}.click_element_wait_retry", click), \
         patch(f"{_RA}.get_ai_message_refinement", return_value=refined) as refine, \
         patch(f"{_RA}.time.sleep"), \
         patch(f"{_RA}.log_error") as log_error, \
         patch(f"{_RA}.log_warning") as log_warning, \
         patch(f"{_RA}.insert_new_log") as insert_log, \
         patch(f"{_RA}.record_action") as record_action, \
         patch(f"{_RA}.quit_gracefully"):
        sent, reason = ra.invite_to_connect_now(1, "https://x/in/jane", message)
    return sent, reason, click, insert_log, log_error, log_warning, refine, record_action


def _clicked(click) -> list[str]:
    return [call.args[2] for call in click.call_args_list]


class TestNoteIsBestEffort:
    def test_a_missing_note_affordance_still_sends_the_invite(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        # LinkedIn offers only the bare dialog once the personalized-invite quota is spent.
        sent, reason, click, insert_log, log_error, log_warning, _r, record = _invite(
            found={_SEND_BARE_XPATH}, message="hi jane")
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _SEND_BARE_XPATH in _clicked(click)
        assert insert_log.call_args.kwargs["message"] == CONNECTION_REQUEST_SENT_MESSAGE
        record.assert_called_once()  # a bare invite still spends the account-level budget
        log_error.assert_not_called()
        log_warning.assert_called_once()

    def test_a_note_that_cannot_be_typed_still_sends_the_invite(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        box = MagicMock()
        box.send_keys.side_effect = Exception("ChromeDriver only supports characters in the BMP")
        sent, reason, _click, _log, log_error, log_warning, _r, _rec = _invite(
            found=_NOTE_DIALOG | {_SEND_BARE_XPATH}, message="hi jane", box=box)
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        log_error.assert_not_called()
        log_warning.assert_called_once()

    def test_the_note_is_stripped_of_non_bmp_characters_before_typing(self):
        box = MagicMock()
        _invite(found=_NOTE_DIALOG, message="hi jane \U0001F600", box=box)
        typed = box.send_keys.call_args.args[0]
        assert "\U0001F600" not in typed and typed.startswith("hi jane")

    def test_an_over_long_note_is_refined_and_capped(self):
        from cqc_lem.utilities.db import CONNECT_NOTE_MAX_CHARS
        box = MagicMock()
        _sent, _reason, _click, _log, _err, _warn, refine, _rec = _invite(
            found=_NOTE_DIALOG, message="x" * 400, box=box, refined="y" * 500)
        assert refine.call_args.args[1] == CONNECT_NOTE_MAX_CHARS
        # Refinement can come back long; the textarea's own maxlength would truncate it silently.
        assert len(box.send_keys.call_args.args[0]) == CONNECT_NOTE_MAX_CHARS


class TestNoteHappyPath:
    def test_a_note_is_typed_and_the_invite_sent(self):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE
        box = MagicMock()
        sent, reason, click, _log, log_error, log_warning, refine, _rec = _invite(
            found=_NOTE_DIALOG, message="hi jane", box=box)
        assert sent is True and reason == CONNECTION_REQUEST_SENT_MESSAGE
        assert _clicked(click) == [_NOTE_XPATH, _TEXTAREA_XPATH, _SEND_XPATH]
        box.clear.assert_called_once()
        box.send_keys.assert_called_once_with("hi jane")
        refine.assert_not_called()  # already under the limit
        log_error.assert_not_called()
        log_warning.assert_not_called()

    def test_an_invite_with_no_note_never_opens_the_note_composer(self):
        _sent, _reason, click, _log, _err, _warn, _r, _rec = _invite(
            found={_SEND_BARE_XPATH})
        assert _clicked(click) == [_SEND_BARE_XPATH]


class TestSendFailureIsStillAnError:
    def test_a_dialog_with_no_send_button_reports_a_named_failure(self):
        from cqc_lem.utilities.db import INVITE_NOT_SENT_MESSAGE
        box = MagicMock()
        sent, reason, click, insert_log, log_error, _warn, _r, record = _invite(
            found=_NOTE_DIALOG - {_SEND_XPATH}, message="hi jane", box=box)
        assert sent is False and reason == INVITE_NOT_SENT_MESSAGE
        assert insert_log.call_args.kwargs["message"] == INVITE_NOT_SENT_MESSAGE
        record.assert_not_called()  # nothing was sent, so nothing is spent
        # Both Send labels are tried before giving up — the dialog can be in either state.
        assert _clicked(click)[-2:] == [_SEND_XPATH, _SEND_BARE_XPATH]
        log_error.assert_called_once()
        # exc= is what makes it a fingerprinted PostHog issue rather than a log line nobody reads.
        assert isinstance(log_error.call_args.kwargs.get("exc"), Exception)
