"""A stale Send button on the Connect dialog is a DOM race, not a missing button (issue #1745).

`click_element_wait_retry` never retries a `StaleElementReferenceException` it raises at click
time (it re-locates during the FIND phase, but the click itself can still land on a node the
dialog's own animation — or the note step just before it — has already swapped out). Before this
fix `_submit_connect_invite` treated that race exactly like "no Send button at all" and filed the
deliberate #573 ERROR for a send that a re-locate-and-click would have completed.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common import StaleElementReferenceException

pytestmark = pytest.mark.unit

_INV = "cqc_lem.app.engagement.invites"


class TestSendButtonStaleElementRetry:
    def test_a_stale_send_button_is_retried_and_succeeds(self):
        from cqc_lem.app.engagement import invites as ra

        calls = {"n": 0}

        def click(driver, wait, xpath, label, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise StaleElementReferenceException("stale element reference: stale element "
                                                      "not found in the current frame")
            return MagicMock()

        with patch(f"{_INV}.click_element_wait_retry", side_effect=click), \
             patch(f"{_INV}.log_error") as log_error, \
             patch(f"{_INV}.log_debug") as log_debug:
            sent = ra._submit_connect_invite(MagicMock(), MagicMock(), user_id=1, with_note=False)

        assert sent is True
        assert calls["n"] == 2  # one stale miss, one successful re-locate-and-click
        log_error.assert_not_called()
        log_debug.assert_called_once()  # the retry breadcrumb, not an escalating warning

    def test_a_persistently_stale_button_still_escalates_once_both_labels_are_exhausted(self):
        from cqc_lem.app.engagement import invites as ra

        def click(driver, wait, xpath, label, **kwargs):
            raise StaleElementReferenceException("stale element reference: stale element not "
                                                  "found in the current frame")

        with patch(f"{_INV}.click_element_wait_retry", side_effect=click) as mock_click, \
             patch(f"{_INV}.log_error") as log_error:
            sent = ra._submit_connect_invite(MagicMock(), MagicMock(), user_id=1, with_note=False)

        assert sent is False
        # 2 attempts per label x 2 labels — the retry budget is bounded, never unbounded.
        assert mock_click.call_count == 4
        log_error.assert_called_once()
        assert isinstance(log_error.call_args.kwargs.get("exc"), StaleElementReferenceException)

    def test_a_wrong_label_miss_still_falls_through_to_the_other_xpath_without_retrying(self):
        """A non-stale miss (e.g. the wrong Send label for this dialog state) keeps the existing
        single-attempt-per-label behaviour — only staleness gets the extra try.
        """
        from cqc_lem.app.engagement import invites as ra

        def click(driver, wait, xpath, label, **kwargs):
            raise Exception(f"no element for {xpath}")

        with patch(f"{_INV}.click_element_wait_retry", side_effect=click) as mock_click, \
             patch(f"{_INV}.log_error") as log_error:
            sent = ra._submit_connect_invite(MagicMock(), MagicMock(), user_id=1, with_note=False)

        assert sent is False
        assert mock_click.call_count == 2  # one attempt per label, no stale-retry budget spent
        log_error.assert_called_once()
