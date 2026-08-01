"""Unit tests for the feed 'Recent' sort — issue #569 (`Error during feed sort` paging 11x/24h)
and issue #817 (a silent miss let the recency-dominant scorer rank the algorithmic feed)."""

import pytest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"

_FEED_URL = "https://www.linkedin.com/feed/"


def _driver(url: str = _FEED_URL) -> MagicMock:
    driver = MagicMock()
    driver.current_url = url
    return driver


def _control(label: str) -> MagicMock:
    control = MagicMock()
    control.text = label
    control.get_attribute.return_value = ""
    return control


class TestSwitchFeedToRecent:
    def test_reports_missing_when_sort_control_absent(self):
        from cqc_lem.app.run_automation import FEED_SORT_MISSING, _switch_feed_to_recent
        driver = _driver()
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_MISSING
        assert find_first.call_count == 1
        driver.execute_script.assert_not_called()

    def test_missing_sort_control_is_not_a_warning_on_a_group_feed(self):
        """Group feeds never render the 'Sort by' control (issue #872) — a WARNING there repeats
        every group run and escalates into a filed defect for working behaviour.

        #817 keeps that guarantee by a stronger mechanism than #872's `warn_on_miss=False`: the
        surface is rejected BEFORE the lookup, so there is no miss to warn about, and none of the
        wall clock a lookup costs on a surface that never had the control."""
        from cqc_lem.app.run_automation import FEED_SORT_NOT_APPLICABLE, _switch_feed_to_recent
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/groups/12345/"
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_NOT_APPLICABLE
        find_first.assert_not_called()

    def test_missing_sort_control_still_warns_on_the_home_feed(self):
        """The home feed DOES render the control — silencing the miss there would hide the selector
        rot that leaves the recency-dominant engine reading a 'Top' feed. `find_first` warns on a
        miss by default, so the home feed is the one surface where the lookup actually runs."""
        from cqc_lem.app.run_automation import FEED_SORT_MISSING, _switch_feed_to_recent
        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/feed/"
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_MISSING
        find_first.assert_called_once()
        # Not silenced: the default is warn_on_miss=True and #817 must not opt out of it.
        assert find_first.call_args.kwargs.get("warn_on_miss", True) is True

    def test_unreadable_url_does_not_warn(self):
        """A dead session can't say which surface it was on — never escalate on a guess (#872).
        The URL read must not raise out of `_is_home_feed` either, or the outer handler logs
        `Feed recent-sort failed` at WARNING and files the defect by the other door."""
        from cqc_lem.app.run_automation import FEED_SORT_NOT_APPLICABLE, _switch_feed_to_recent

        class _DeadSession:
            @property
            def current_url(self):
                raise RuntimeError("invalid session id")

        with patch(f"{_MOD}.find_first", return_value=None) as find_first, \
             patch(f"{_MOD}.log_warning") as log_warning:
            state = _switch_feed_to_recent(_DeadSession(), MagicMock())
        assert state == FEED_SORT_NOT_APPLICABLE
        find_first.assert_not_called()
        log_warning.assert_not_called()

    def test_skips_when_already_sorted_by_recent(self):
        from cqc_lem.app.run_automation import FEED_SORT_RECENT, _switch_feed_to_recent
        driver = _driver()
        with patch(f"{_MOD}.find_first", return_value=_control("Sort by: Recent")):
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_RECENT
        driver.execute_script.assert_not_called()

    def test_clicks_sort_then_recent_option_and_verifies(self):
        from cqc_lem.app.run_automation import FEED_SORT_RECENT, _switch_feed_to_recent
        driver = _driver()
        btn, opt, after = _control("Sort by: Top"), _control("Recent"), _control("Sort by: Recent")
        with patch(f"{_MOD}.find_first", side_effect=[btn, opt, after]), \
             patch(f"{_MOD}.time.sleep"):
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_RECENT
        clicked = [c.args[1] for c in driver.execute_script.call_args_list]
        assert clicked == [btn, opt]

    def test_unverified_flip_is_never_reported_as_sorted(self):
        """The control re-renders unreadable after the click: 'we could not tell' must not be
        recorded as 'recent', or #622's effect gets measured against runs that never sorted."""
        from cqc_lem.app.run_automation import FEED_SORT_UNKNOWN, _switch_feed_to_recent
        with patch(f"{_MOD}.find_first",
                   side_effect=[_control("Sort by: Top"), _control("Recent"), None]), \
             patch(f"{_MOD}.time.sleep"):
            state = _switch_feed_to_recent(_driver(), MagicMock())
        assert state == FEED_SORT_UNKNOWN

    def test_reports_top_when_recent_option_missing(self):
        from cqc_lem.app.run_automation import FEED_SORT_TOP, _switch_feed_to_recent
        with patch(f"{_MOD}.find_first", side_effect=[_control("Sort by: Top"), None]), \
             patch(f"{_MOD}.time.sleep"):
            state = _switch_feed_to_recent(_driver(), MagicMock())
        assert state == FEED_SORT_TOP

    def test_reads_sort_state_from_aria_label_when_button_text_is_empty(self):
        from cqc_lem.app.run_automation import FEED_SORT_RECENT, _switch_feed_to_recent
        control = MagicMock()
        control.text = ""
        control.get_attribute.return_value = "Sort by dropdown, currently RECENT"
        driver = _driver()
        with patch(f"{_MOD}.find_first", return_value=control):
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_RECENT
        driver.execute_script.assert_not_called()

    def test_a_label_naming_both_sorts_is_not_read_as_already_recent(self):
        """A trigger that spells its OPTIONS into the accessible name mentions 'Recent' while the
        feed is still on Top. Reading that as sorted would skip the flip AND record the run as
        recency-sorted — both halves of the lie #817 exists to stop, in one label."""
        from cqc_lem.app.run_automation import FEED_SORT_RECENT, _switch_feed_to_recent
        ambiguous = MagicMock()
        ambiguous.text = ""
        ambiguous.get_attribute.return_value = "Sort by, currently Top, options Top and Recent"
        opt = _control("Recent")
        driver = _driver()
        with patch(f"{_MOD}.find_first",
                   side_effect=[ambiguous, opt, _control("Sort by: Recent")]), \
             patch(f"{_MOD}.time.sleep"):
            state = _switch_feed_to_recent(driver, MagicMock())
        # The flip actually ran instead of being short-circuited by the ambiguous label.
        assert [c.args[1] for c in driver.execute_script.call_args_list] == [ambiguous, opt]
        assert state == FEED_SORT_RECENT

    def test_an_ambiguous_label_after_the_flip_reads_unknown_not_recent(self):
        from cqc_lem.app.run_automation import FEED_SORT_UNKNOWN, _switch_feed_to_recent
        both = MagicMock()
        both.text = "Top Recent"
        both.get_attribute.return_value = ""
        with patch(f"{_MOD}.find_first",
                   side_effect=[_control("Sort by: Top"), _control("Recent"), both]), \
             patch(f"{_MOD}.time.sleep"):
            state = _switch_feed_to_recent(_driver(), MagicMock())
        assert state == FEED_SORT_UNKNOWN

    def test_group_feed_is_an_expected_no_op_not_a_warning(self):
        """A group feed reuses the commenting engine but never had a home-feed sort control, so a
        miss there is working behaviour — warning on it would file a defect for it (#817)."""
        from cqc_lem.app.run_automation import FEED_SORT_NOT_APPLICABLE, _switch_feed_to_recent
        driver = _driver("https://www.linkedin.com/groups/12345/")
        with patch(f"{_MOD}.find_first") as find_first, \
             patch(f"{_MOD}.log_warning") as log_warning:
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_NOT_APPLICABLE
        find_first.assert_not_called()
        log_warning.assert_not_called()

    def test_fails_fast_instead_of_burning_the_retry_budget(self):
        from cqc_lem.app.run_automation import _switch_feed_to_recent
        with patch(f"{_MOD}.find_first", return_value=None) as find_first:
            _switch_feed_to_recent(_driver(), MagicMock())
        assert find_first.call_args.kwargs["max_try"] == 1

    def test_selector_miss_warns_and_does_not_raise(self):
        from cqc_lem.app.run_automation import FEED_SORT_UNKNOWN, _switch_feed_to_recent
        driver = _driver()
        driver.execute_script.side_effect = RuntimeError("element not interactable")
        with patch(f"{_MOD}.find_first", return_value=_control("Sort by: Top")), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.log_warning") as log_warning, \
             patch(f"{_MOD}.log_error") as log_error:
            state = _switch_feed_to_recent(driver, MagicMock())
        assert state == FEED_SORT_UNKNOWN
        assert log_warning.called
        log_error.assert_not_called()


class TestFeedSortLocators:
    def test_locator_chain_is_ordered_and_never_keys_on_class_names(self):
        from cqc_lem.app.run_automation import _FEED_RECENT_OPTION_LOCATORS, _FEED_SORT_LOCATORS
        assert len(_FEED_SORT_LOCATORS) >= 4
        for _by, value in _FEED_SORT_LOCATORS + _FEED_RECENT_OPTION_LOCATORS:
            assert "@class" not in value and "contains(@id" not in value

    def test_every_label_comparison_is_case_folded(self):
        """LinkedIn renders 'Recent', not 'recent'; a case-sensitive literal silently never fires."""
        from cqc_lem.app.run_automation import _FEED_RECENT_OPTION_LOCATORS, _FEED_SORT_LOCATORS
        for _by, value in _FEED_SORT_LOCATORS + _FEED_RECENT_OPTION_LOCATORS:
            assert "translate(" in value


class TestIsHomeFeed:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.linkedin.com/feed/", True),
        ("https://www.linkedin.com/feed", True),
        ("https://www.linkedin.com/feed/?highlightedUpdateType=x", True),
        ("https://www.linkedin.com/groups/12345/", False),
        ("https://www.linkedin.com/in/someone/recent-activity/all/", False),
        ("https://www.linkedin.com/feed/update/urn:li:activity:123/", False),
        ("", False),
    ])
    def test_only_the_home_feed_has_a_sort_control(self, url, expected):
        from cqc_lem.app.run_automation import _is_home_feed
        driver = MagicMock()
        driver.current_url = url
        assert _is_home_feed(driver) is expected


class TestScanRecordsTheSortItRanAgainst:
    """#817's second acceptance criterion: a miss is RECORDED, not just logged. Without this the
    funnel and the feed_scan series cannot tell a recency-sorted scan from an algorithmic one."""

    @staticmethod
    def _scan(sort_state):
        from cqc_lem.app import run_automation as ra

        driver = _driver()
        driver.find_elements.return_value = []          # empty feed — straight to the funnel write
        funnel_holder, scans = {}, []
        with ExitStack() as es:
            p = lambda name, **kw: es.enter_context(patch(f"{_MOD}.{name}", **kw))
            p("get_engagement_preferences", return_value={"max_comments_per_day": 20})
            p("get_recent_engagers", return_value=set())
            p("get_recent_comment_texts", return_value=[])
            p("count_comments_today", return_value=0)
            p("remaining_actions", return_value=5)
            p("get_engagement_targets", return_value=[])
            p("get_or_create_profile_synthesis", return_value="voice")
            p("_switch_feed_to_recent", return_value=sort_state)
            p("set_feed_funnel", side_effect=lambda uid, f: funnel_holder.update(f))
            p("track_feed_scan", side_effect=lambda uid, f: scans.append((uid, dict(f))))
            p("time.sleep")
            ra.comment_on_feed_inline(driver, MagicMock(), MagicMock(), user_id=7, max_posts=3)
        return funnel_holder, scans

    def test_a_miss_lands_on_the_funnel_and_the_event(self):
        from cqc_lem.app.run_automation import FEED_SORT_MISSING
        funnel, scans = self._scan(FEED_SORT_MISSING)
        assert funnel["feed_sort"] == FEED_SORT_MISSING
        assert scans == [(7, funnel)]

    def test_a_sorted_scan_is_recorded_as_sorted(self):
        from cqc_lem.app.run_automation import FEED_SORT_RECENT
        funnel, scans = self._scan(FEED_SORT_RECENT)
        assert funnel["feed_sort"] == FEED_SORT_RECENT
        assert scans[0][1]["feed_sort"] == FEED_SORT_RECENT


class TestNavigateToFeed:
    def test_navigates_then_delegates_sort(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = _driver("https://www.linkedin.com/in/someone/")
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}._switch_feed_to_recent") as switch:
            navigate_to_feed(driver, MagicMock())
        driver.get.assert_called_once_with(_FEED_URL)
        assert switch.called

    def test_skips_navigation_when_already_on_feed(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = _driver()
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}._switch_feed_to_recent") as switch:
            navigate_to_feed(driver, MagicMock())
        driver.get.assert_not_called()
        assert switch.called

    def test_sort_failure_does_not_page_as_error(self):
        from cqc_lem.app.run_automation import navigate_to_feed
        driver = _driver()
        driver.execute_script.side_effect = RuntimeError("stale sort control")
        with patch(f"{_MOD}.wait_for_ajax"), \
             patch(f"{_MOD}.find_first", return_value=_control("Sort by: Top")), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.log_error") as log_error, \
             patch(f"{_MOD}.log_warning") as log_warning:
            navigate_to_feed(driver, MagicMock())
        log_error.assert_not_called()
        assert log_warning.called
