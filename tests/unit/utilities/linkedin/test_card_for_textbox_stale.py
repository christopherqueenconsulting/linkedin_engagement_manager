"""`_card_for_textbox` treats a stale text box as "no card", not an exception (#2852)."""

from unittest.mock import MagicMock

from selenium.common.exceptions import StaleElementReferenceException

from cqc_lem.utilities.linkedin.cards import _card_for_textbox


def test_stale_box_returns_none():
    driver = MagicMock()
    driver.execute_script.side_effect = StaleElementReferenceException("stale element reference")
    assert _card_for_textbox(driver, MagicMock()) is None


def test_card_is_returned_when_box_is_live():
    driver = MagicMock()
    card = object()
    driver.execute_script.return_value = card
    assert _card_for_textbox(driver, MagicMock()) is card
