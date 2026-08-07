"""Unit tests for selenium_util.get_visible_element_wait_retry.

LinkedIn's redesigned login renders duplicate hidden+visible copies of each field,
so the finder must skip invisible matches and try locators in order.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.selenium_util import get_visible_element_wait_retry

pytestmark = pytest.mark.unit

_MODULE = "cqc_lem.utilities.selenium_util"


def _el(displayed: bool, tag: str = "el") -> MagicMock:
    e = MagicMock(name=tag)
    e.is_displayed.return_value = displayed
    return e


class _FakeWait:
    """Minimal WebDriverWait stand-in: evaluates the predicate; raises on falsey."""

    def __init__(self, driver):
        self.driver = driver

    def until(self, method, message=""):
        result = method(self.driver)
        if result:
            return result
        raise TimeoutException(message)


def _driver_with(mapping):
    """mapping: {(by, value): [elements]} → driver.find_elements lookups."""
    driver = MagicMock()
    driver.find_elements.side_effect = lambda by, value: mapping.get((by, value), [])
    return driver


def test_returns_first_displayed_skipping_hidden():
    hidden, visible = _el(False, "hidden"), _el(True, "visible")
    loc = (By.CSS_SELECTOR, "input[type='password']")
    driver = _driver_with({loc: [hidden, visible]})

    result = get_visible_element_wait_retry(driver, _FakeWait(driver), [loc], "pw")

    assert result is visible
    hidden.is_displayed.assert_called()


def test_tries_locators_in_order():
    visible = _el(True, "second")
    loc1 = (By.ID, "username")            # legacy — absent
    loc2 = (By.CSS_SELECTOR, "input[autocomplete~='username']")  # modern — present
    driver = _driver_with({loc1: [], loc2: [visible]})

    result = get_visible_element_wait_retry(driver, _FakeWait(driver), [loc1, loc2], "user")

    assert result is visible


def test_raises_when_none_visible_and_expected():
    loc = (By.CSS_SELECTOR, "input[type='email']")
    driver = _driver_with({loc: [_el(False)]})  # only a hidden copy

    with patch(f"{_MODULE}.time.sleep"), \
         pytest.raises(TimeoutException):
        get_visible_element_wait_retry(driver, _FakeWait(driver), [loc], "user", max_try=1)


def test_returns_none_when_not_expected():
    loc = (By.CSS_SELECTOR, "input[type='email']")
    driver = _driver_with({loc: []})

    with patch(f"{_MODULE}.time.sleep"):
        result = get_visible_element_wait_retry(
            driver, _FakeWait(driver), [loc], "user",
            max_try=1, element_always_expected=False)

    assert result is None
