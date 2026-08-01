"""Unit tests for the resilient find_first / click_first / find_all_first helpers."""

import pytest
from unittest.mock import MagicMock, patch
from selenium.common.exceptions import TimeoutException

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.selenium_util"


class _FakeWait:
    """Minimal WebDriverWait stand-in: evaluate the predicate once; truthy → return, else Timeout."""
    def __init__(self, driver):
        self.driver = driver

    def until(self, fn, msg=None):
        result = fn(self.driver)
        if result:
            return result
        raise TimeoutException(msg)


def _driver_with(find_elements_map):
    """driver.find_elements(by, value) returns find_elements_map.get(value, [])."""
    driver = MagicMock()
    driver.current_url = "https://www.linkedin.com/feed/"
    driver.find_elements.side_effect = lambda by, value: find_elements_map.get(value, [])
    return driver


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_MOD}.time.sleep"):
        yield


class TestFindFirst:
    def test_returns_first_matching_in_order(self):
        from cqc_lem.utilities.selenium_util import find_first
        el = MagicMock()
        driver = _driver_with({"miss": [], "hit": [el]})
        wait = _FakeWait(driver)
        got = find_first(driver, wait, [("css", "miss"), ("css", "hit")], "thing")
        assert got is el

    def test_miss_required_raises(self):
        from cqc_lem.utilities.selenium_util import find_first
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with pytest.raises(TimeoutException):
            find_first(driver, wait, [("css", "a"), ("css", "b")], "thing", max_try=1)

    def test_miss_not_required_logs_and_returns_none(self):
        from cqc_lem.utilities.selenium_util import find_first
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with patch(f"{_MOD}.log_warning") as warn:
            got = find_first(driver, wait, [("css", "a"), ("css", "b")], "postbox",
                             required=False, max_try=1, user_id=7, post_id=3)
        assert got is None
        warn.assert_called_once()
        # structured context includes the tried selectors + url
        kwargs = warn.call_args.kwargs
        assert kwargs["user_id"] == 7 and kwargs["post_id"] == 3
        assert any("a" in s for s in kwargs["selectors"])

    def test_expected_miss_logs_debug_not_warning(self):
        """warn_on_miss=False keeps a legitimately-absent control out of recurrence escalation."""
        from cqc_lem.utilities.selenium_util import find_first
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with patch(f"{_MOD}.log_warning") as warn, patch(f"{_MOD}.log_debug") as debug:
            got = find_first(driver, wait, [("css", "a")], "Feed sort control",
                             required=False, warn_on_miss=False, max_try=1, user_id=7)
        assert got is None
        warn.assert_not_called()
        debug.assert_called_once()
        assert debug.call_args.args[0] == "Selector miss: Feed sort control"
        assert debug.call_args.kwargs["user_id"] == 7

    def test_expected_miss_survives_the_retry_pass(self):
        """The retry recursion must carry warn_on_miss through, or the last attempt warns anyway."""
        from cqc_lem.utilities.selenium_util import find_first
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with patch(f"{_MOD}.log_warning") as warn, patch(f"{_MOD}.log_debug") as debug:
            find_first(driver, wait, [("css", "a")], "Feed sort control",
                       required=False, warn_on_miss=False, max_try=2)
        warn.assert_not_called()
        debug.assert_called_once()

    def test_visible_only_skips_hidden(self):
        from cqc_lem.utilities.selenium_util import find_first
        hidden = MagicMock(); hidden.is_displayed.return_value = False
        shown = MagicMock(); shown.is_displayed.return_value = True
        driver = _driver_with({"box": [hidden, shown]})
        wait = _FakeWait(driver)
        got = find_first(driver, wait, [("css", "box")], "composer", visible_only=True)
        assert got is shown


class TestFindAllFirst:
    def test_returns_first_nonempty_list(self):
        from cqc_lem.utilities.selenium_util import find_all_first
        a, b = MagicMock(), MagicMock()
        driver = _driver_with({"empty": [], "full": [a, b]})
        got = find_all_first(driver, [("css", "empty"), ("css", "full")])
        assert got == [a, b]

    def test_returns_empty_when_none_match(self):
        from cqc_lem.utilities.selenium_util import find_all_first
        driver = _driver_with({})
        assert find_all_first(driver, [("css", "x")]) == []


class TestClickFirst:
    def test_clicks_the_found_element(self):
        from cqc_lem.utilities import selenium_util as su
        el = MagicMock(); el.is_displayed.return_value = True
        driver = _driver_with({"btn": [el]})
        wait = _FakeWait(driver)
        with patch.object(su.EC, "element_to_be_clickable", return_value=lambda d: el):
            # _FakeWait.until will call the clickable predicate and return el
            out = su.click_first(driver, wait, [("css", "btn")], "button")
        el.click.assert_called_once()
        assert out is el

    def test_carries_warn_on_miss_into_find_first(self):
        """click_first is the only way most call sites reach find_first — without the passthrough a
        caller with a working fallback (issue #873) has no way to keep its miss out of escalation."""
        from cqc_lem.utilities import selenium_util as su
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with patch(f"{_MOD}.log_warning") as warn, patch(f"{_MOD}.log_debug") as debug:
            out = su.click_first(driver, wait, [("css", "btn")], "Open reactions menu",
                                 required=False, warn_on_miss=False, max_try=1, user_id=4)
        assert out is None
        warn.assert_not_called()
        debug.assert_called_once()
        assert debug.call_args.args[0] == "Selector miss: Open reactions menu"

    def test_warns_on_miss_by_default(self):
        from cqc_lem.utilities import selenium_util as su
        driver = _driver_with({})
        wait = _FakeWait(driver)
        with patch(f"{_MOD}.log_warning") as warn:
            su.click_first(driver, wait, [("css", "btn")], "Open reactions menu",
                           required=False, max_try=1)
        warn.assert_called_once()
