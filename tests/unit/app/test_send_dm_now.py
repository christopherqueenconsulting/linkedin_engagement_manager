"""Unit tests for the shared DM send path (issue #1030).

`send_dm_now` is the ONE place four lanes send a message — private DM, scheduled DM, appreciation and
catch-up congratulations — so when its Message-button selector died against LinkedIn's SDUI, all four
went silent together. These tests hold the two properties that failure exposed: the send is only ever
reached through a composer that names its recipient, and 'sent' is the message LANDING, never a click
being accepted.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common import NoSuchElementException, StaleElementReferenceException, WebDriverException

from cqc_lem.utilities.db import LogResultType
from cqc_lem.utilities.linkedin.message_thread import ComposerOpen

pytestmark = pytest.mark.unit

_OUT = "cqc_lem.app.engagement.outreach"

ADDRESSED = ComposerOpen(opened=True, recipient="Jane Doe", urn="urn:li:fsd_profile:X",
                         surface="page")


def _send(composer=ADDRESSED, landed=True, **extra):
    """Run send_dm_now with every I/O boundary stubbed; returns (result, log_mock, composer_mock)."""
    from cqc_lem.app.engagement import outreach as ra
    with patch(f"{_OUT}.get_user_password_pair_by_id", return_value=("e", "p")), \
         patch(f"{_OUT}.get_driver_wait_pair", return_value=(MagicMock(), MagicMock())), \
         patch(f"{_OUT}.login_to_linkedin"), \
         patch(f"{_OUT}.open_addressed_composer", return_value=composer) as opened, \
         patch(f"{_OUT}.click_element_wait_retry") as clicked, \
         patch(f"{_OUT}.get_element_wait_retry", return_value=MagicMock()), \
         patch(f"{_OUT}._dm_send_landed", return_value=landed), \
         patch(f"{_OUT}.simulate_typing") as typed, \
         patch(f"{_OUT}.time.sleep"), \
         patch(f"{_OUT}.insert_new_log") as logged, \
         patch(f"{_OUT}.record_action"), \
         patch(f"{_OUT}.quit_gracefully") as quit_driver:
        result = ra.send_dm_now(1, "https://www.linkedin.com/in/jane", "Congrats Jane!", **extra)
    return result, {"log": logged, "opened": opened, "typed": typed, "clicked": clicked,
                    "quit": quit_driver}


class TestAddressedComposerGate:
    def test_an_addressed_composer_sends(self):
        result, m = _send()
        assert result is True
        m["typed"].assert_called_once()

    @pytest.mark.parametrize("reason", ["unaddressed", "no_urn", "composer_missing",
                                        "profile_unreachable"])
    def test_nothing_is_typed_when_the_composer_names_nobody(self, reason):
        # The one outcome worse than a missed congratulations is a message to the wrong person.
        result, m = _send(composer=ComposerOpen(opened=False, reason=reason))
        assert result is False
        m["typed"].assert_not_called()
        m["clicked"].assert_not_called()

    def test_an_open_but_unaddressed_composer_is_still_refused(self):
        # opened=True is exactly the trap: the compose screen renders with an empty recipient field.
        result, m = _send(composer=ComposerOpen(opened=True, recipient="", reason="unaddressed"))
        assert result is False
        m["typed"].assert_not_called()

    def test_the_recipient_name_is_passed_through_for_the_fallback_route(self):
        _, m = _send(person_name="Jane Doe")
        assert m["opened"].call_args.kwargs["person_name"] == "Jane Doe"


class TestOutcomeIsRecorded:
    def test_a_refused_send_is_logged_as_a_failure_not_skipped_silently(self):
        _, m = _send(composer=ComposerOpen(opened=False, reason="no_urn"))
        assert m["log"].call_args.kwargs["result"] is LogResultType.FAILURE

    def test_a_landed_send_is_logged_as_success(self):
        _, m = _send()
        assert m["log"].call_args.kwargs["result"] is LogResultType.SUCCESS

    def test_a_send_that_did_not_land_is_a_failure(self):
        result, m = _send(landed=False)
        assert result is False
        assert m["log"].call_args.kwargs["result"] is LogResultType.FAILURE

    def test_the_browser_is_always_closed(self):
        for composer in (ADDRESSED, ComposerOpen(opened=False, reason="no_urn")):
            _, m = _send(composer=composer)
            m["quit"].assert_called_once()


class TestSendLanded:
    """'Sent' has to be the message being THERE. The old path set sent=True the moment the Send
    button accepted a click, which is why 8 failures reported as 8 dispatches.
    """

    @staticmethod
    def _driver(composer_text=""):
        driver = MagicMock()
        el = MagicMock()
        el.text = composer_text
        el.is_displayed.return_value = True
        driver.find_elements.return_value = [el]
        return driver

    def test_our_text_as_the_newest_message_confirms_the_send(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.read_last_message", return_value="Congrats Jane!"):
            assert ra._dm_send_landed(self._driver(), "Congrats Jane!") is True

    def test_text_still_sitting_in_the_composer_disproves_it(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.read_last_message", return_value="an older message"), \
             patch(f"{_OUT}.DM_SEND_CONFIRM_SECONDS", 0):
            assert ra._dm_send_landed(self._driver("Congrats Jane!"), "Congrats Jane!") is False

    def test_an_unreadable_thread_trusts_the_click_rather_than_inviting_a_duplicate(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.read_last_message", return_value=""), \
             patch(f"{_OUT}.DM_SEND_CONFIRM_SECONDS", 0):
            assert ra._dm_send_landed(self._driver(), "Congrats Jane!") is True

    def test_a_driver_that_raises_on_the_composer_read_still_trusts_the_click(self):
        from cqc_lem.app.engagement import outreach as ra
        driver = MagicMock()
        driver.find_elements.side_effect = WebDriverException("gone")
        with patch(f"{_OUT}.read_last_message", return_value=""), \
             patch(f"{_OUT}.DM_SEND_CONFIRM_SECONDS", 0):
            assert ra._dm_send_landed(driver, "Congrats Jane!") is True

    def test_a_long_message_is_matched_on_its_head(self):
        from cqc_lem.app.engagement import outreach as ra
        message = "Congrats on the new role, Jane! " + "x" * 400
        with patch(f"{_OUT}.read_last_message", return_value=message):
            assert ra._dm_send_landed(self._driver(), message) is True


class TestTypingSurvivesTheComposerRemount:
    """The compose overlay re-mounts while LinkedIn hydrates it, so a handle taken when the box first
    appears goes stale a keystroke later. That is what killed the re-queued sends on 2026-08-04 once
    the composer had started resolving.
    """

    @staticmethod
    def _box(stale_on_send=0):
        """A composer whose send_keys raises StaleElementReference the first `stale_on_send` times."""
        box = MagicMock()
        calls = {"n": 0}

        def _send(*_a):
            calls["n"] += 1
            if calls["n"] <= stale_on_send:
                raise StaleElementReferenceException("re-mounted")

        box.send_keys.side_effect = _send
        return box

    def test_a_stale_box_is_re_found_and_the_message_still_goes_in(self):
        from cqc_lem.app.engagement import outreach as ra
        boxes = [self._box(stale_on_send=1), self._box()]
        with patch(f"{_OUT}.get_element_wait_retry", side_effect=boxes), \
             patch(f"{_OUT}.simulate_typing") as typed, \
             patch(f"{_OUT}.time.sleep"):
            ra._type_dm_into_composer(MagicMock(), MagicMock(), "Congrats Jane!")
        typed.assert_called_once()
        assert typed.call_args[0][1] is boxes[1]  # the RE-FOUND box, never the stale handle

    def test_every_attempt_clears_first_so_a_retry_cannot_double_type(self):
        from cqc_lem.app.engagement import outreach as ra
        box = self._box()
        with patch(f"{_OUT}.get_element_wait_retry", return_value=box), \
             patch(f"{_OUT}.simulate_typing"), \
             patch(f"{_OUT}.time.sleep"):
            ra._type_dm_into_composer(MagicMock(), MagicMock(), "Congrats Jane!")
        assert box.send_keys.call_count == 2  # select-all then delete, before any typing

    def test_a_composer_stale_on_every_attempt_raises_rather_than_sending_blind(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.get_element_wait_retry",
                   side_effect=lambda *a, **k: self._box(stale_on_send=99)), \
             patch(f"{_OUT}.simulate_typing") as typed, \
             patch(f"{_OUT}.time.sleep"):
            with pytest.raises(StaleElementReferenceException):
                ra._type_dm_into_composer(MagicMock(), MagicMock(), "Congrats Jane!")
        typed.assert_not_called()

    def test_no_composer_at_all_is_an_error_not_a_silent_skip(self):
        from cqc_lem.app.engagement import outreach as ra
        with patch(f"{_OUT}.get_element_wait_retry", return_value=None), \
             patch(f"{_OUT}.simulate_typing"), \
             patch(f"{_OUT}.time.sleep"):
            with pytest.raises(NoSuchElementException):
                ra._type_dm_into_composer(MagicMock(), MagicMock(), "Congrats Jane!")
