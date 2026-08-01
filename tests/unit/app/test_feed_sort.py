"""Unit tests for the feed 'Recent' sort — issue #569 (`Error during feed sort` paging 11x/24h)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"


class TestSwitchFeedToRecent:
    def test_no_op_when_sort_control_missing(self):
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            _switch_feed_to_recent(driver, MagicMock())
        assert find_first.call_count == 1
        driver.execute_script.assert_not_called()

    def test_missing_sort_control_is_not_a_warning_on_a_group_feed(self):
        """Group feeds never render the 'Sort by' control (issue #872) — a WARNING there repeats
        every group run and escalates into a filed defect for working behaviour. The miss is also
        not worth retrying out at 15s a try on a surface that never has the control."""
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/groups/12345/"
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            _switch_feed_to_recent(driver, MagicMock())
        assert find_first.call_args.kwargs["warn_on_miss"] is False
        assert find_first.call_args.kwargs["max_try"] == 1

    def test_missing_sort_control_still_warns_on_the_home_feed(self):
        """The home feed DOES render the control — silencing the miss there would hide the selector
        rot that leaves the recency-dominant engine reading a 'Top' feed."""
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            _switch_feed_to_recent(driver, MagicMock())
        assert find_first.call_args.kwargs["warn_on_miss"] is True
        assert find_first.call_args.kwargs["max_try"] > 1

    def test_unreadable_url_does_not_warn(self):
        """A dead session can't say which surface it was on — never escalate on a guess."""
        from cqc_lem.app.run_automation import _switch_feed_to_recent

        class _DeadSession:
            @property
            def current_url(self):
                raise RuntimeError("invalid session id")

        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            _switch_feed_to_recent(_DeadSession(), MagicMock())
        assert find_first.call_args.kwargs["warn_on_miss"] is False

    def test_skips_when_already_sorted_by_recent(self):
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        btn = MagicMock()
        btn.text = "Sort by: Recent"
        with patch(f"{_MOD}.find_first", return_value=btn):
            _switch_feed_to_recent(driver, MagicMock())
        driver.execute_script.assert_not_called()

    def test_clicks_sort_then_recent_option(self):
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        btn = MagicMock()
        btn.text = "Sort by: Top"
        opt = MagicMock()
        with patch(f"{_MOD}.find_first", side_effect=[btn, opt]), \
             patch(f"{_MOD}.time.sleep"):
            _switch_feed_to_recent(driver, MagicMock())
        clicked = [c.args[1] for c in driver.execute_script.call_args_list]
        assert clicked == [btn, opt]

    def test_selector_miss_warns_and_does_not_raise(self):
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("element not interactable")
        btn = MagicMock()
        btn.text = "Sort by: Top"
        with patch(f"{_MOD}.find_first", return_value=btn), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.log_warning") as log_warning, \
             patch(f"{_MOD}.log_error") as log_error:
            _switch_feed_to_recent(driver, MagicMock())
        assert log_warning.called
        log_error.assert_not_called()


class TestNavigateToFeed:
    def test_navigates_then_delegates_sort(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/someone/"
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}._switch_feed_to_recent") as switch:
            navigate_to_feed(driver, MagicMock())
        driver.get.assert_called_once_with("https://www.linkedin.com/feed/")
        assert switch.called

    def test_skips_navigation_when_already_on_feed(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}._switch_feed_to_recent") as switch:
            navigate_to_feed(driver, MagicMock())
        driver.get.assert_not_called()
        assert switch.called

    def test_sort_failure_does_not_page_as_error(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        driver.execute_script.side_effect = RuntimeError("stale sort control")
        btn = MagicMock()
        btn.text = "Sort by: Top"
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}.find_first", return_value=btn), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.log_error") as log_error, \
             patch(f"{_MOD}.log_warning") as log_warning:
            navigate_to_feed(driver, MagicMock())
        log_error.assert_not_called()
        assert log_warning.called
