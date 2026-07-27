"""Unit tests for automate_profile_viewer_engagement's best-effort read of the profile-views page.

The analytics list never resolves the wait when nobody viewed the profile — find_elements
returns [] and the helper polls to a TimeoutException. That is "nothing to do", not a task
failure, so it must degrade to a warning instead of paging the PostHog error cron (issue #572).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"


def _profile_pair():
    driver = MagicMock(name="driver")
    wait = MagicMock(name="wait")
    profile = MagicMock(name="profile")
    profile.email = "user@example.com"
    return driver, wait, "user@example.com", profile


def _viewer_row(name: str, url: str) -> MagicMock:
    """A row whose title/caption nodes carry real text, so getText runs unpatched."""
    row = MagicMock(name=f"row_{name}")
    row.get_attribute.return_value = url

    def _find(by, value):
        node = MagicMock(name=f"node_{name}")
        node.text = name if "lockup__title" in value else "viewed 1h ago"
        return node

    row.find_element.side_effect = _find
    return row


def _engaged_urls(mock_engage) -> list[str]:
    return [c.kwargs["kwargs"]["viewer_url"] for c in mock_engage.apply_async.call_args_list]


class TestNoViewersFound:
    def test_timeout_finding_viewers_is_a_warning_not_an_error(self):
        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale",
                   side_effect=TimeoutException("Finding Profile Viewers")), \
             patch(f"{_MOD}.quit_gracefully") as mock_quit, \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        mock_warning.assert_called_once()
        assert "Engaged with 0 viewers" in result
        mock_quit.assert_called_once()

    def test_timeout_does_not_dispatch_any_engagement(self):
        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale",
                   side_effect=TimeoutException("Finding Profile Viewers")), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning"), \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            automate_profile_viewer_engagement.run(user_id=1)

        mock_engage.apply_async.assert_not_called()

    def test_stale_list_is_also_treated_as_no_viewers(self):
        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale",
                   side_effect=StaleElementReferenceException("gone")), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        mock_warning.assert_called_once()
        assert "Engaged with 0 viewers" in result


class TestUnreadableViewerRow:
    def test_stale_row_is_skipped_and_the_readable_one_still_engages(self):
        good = MagicMock(name="good_row")
        good.get_attribute.return_value = "https://www.linkedin.com/in/good"

        def _stale_title_only(by, value):
            # The row survives the date read but its title node has been re-rendered away.
            if "lockup__title" in value:
                raise StaleElementReferenceException("row re-rendered")
            return MagicMock(name="viewed_on")

        stale = MagicMock(name="stale_row")
        stale.find_element.side_effect = _stale_title_only

        # First lookup is the loop's own "is the last viewer older than a day?" check — an old
        # date breaks the walk; the two that follow are the date filter over both rows.
        dates = [datetime.now() - timedelta(days=5),
                 datetime.now(), datetime.now()]

        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale", return_value=[stale, good]), \
             patch(f"{_MOD}.convert_viewed_on_to_date", side_effect=dates), \
             patch(f"{_MOD}.getText", return_value="Some Viewer"), \
             patch(f"{_MOD}.get_user_id", return_value=1), \
             patch(f"{_MOD}.close_tab"), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error, \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        mock_warning.assert_called_once()
        assert "Engaged with 1 viewers" in result
        mock_engage.apply_async.assert_called_once()
        assert mock_engage.apply_async.call_args.kwargs["kwargs"]["viewer_url"] == \
            "https://www.linkedin.com/in/good"


class TestUnexpectedFailureStillErrors:
    def test_non_selenium_failure_is_still_logged_as_an_error(self):
        driver, wait, email, profile = _profile_pair()
        driver.get.side_effect = RuntimeError("browser crashed")

        with patch(f"{_MOD}.get_current_profile", return_value=(driver, wait, email, profile)), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_error") as mock_error:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_called_once()
        assert "Error while engaging with profile viewers" in result


class TestWalkTermination:
    """The walk is `while True` — every exit path has to actually be reachable."""

    def test_list_that_stops_growing_ends_the_walk(self):
        """All viewers in range and no more to scroll: without a growth check this never exits."""
        row = _viewer_row("Recent Viewer", "https://www.linkedin.com/in/recent")

        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale", return_value=[row]), \
             patch(f"{_MOD}.convert_viewed_on_to_date", return_value=datetime.now()), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.get_user_id", return_value=1), \
             patch(f"{_MOD}.close_tab"), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_error") as mock_error, \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        assert "Engaged with 1 viewers" in result
        assert _engaged_urls(mock_engage) == ["https://www.linkedin.com/in/recent"]

    def test_mid_walk_timeout_keeps_the_viewers_already_found(self):
        row = _viewer_row("Recent Viewer", "https://www.linkedin.com/in/recent")

        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale",
                   side_effect=[[row], TimeoutException("Finding Profile Viewers")]), \
             patch(f"{_MOD}.convert_viewed_on_to_date", return_value=datetime.now()), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.get_user_id", return_value=1), \
             patch(f"{_MOD}.close_tab"), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error, \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        # The warning must not claim zero when a viewer was already collected.
        assert "1 viewer(s)" in mock_warning.call_args_list[0].args[0]
        assert "Engaged with 1 viewers" in result
        assert _engaged_urls(mock_engage) == ["https://www.linkedin.com/in/recent"]

    def test_unparseable_viewed_on_date_warns_instead_of_failing_the_task(self):
        """convert_viewed_on_to_date raises ValueError on a caption it can't parse."""
        row = _viewer_row("Odd Caption", "https://www.linkedin.com/in/odd")

        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale", return_value=[row]), \
             patch(f"{_MOD}.convert_viewed_on_to_date",
                   side_effect=ValueError("invalid datetime as string: ")), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error, \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        assert mock_warning.call_count == 2  # ends the walk, then drops the unreadable row
        assert "Engaged with 0 viewers" in result
        mock_engage.apply_async.assert_not_called()


class TestDateFilter:
    def test_unreadable_date_does_not_leak_out_of_range_viewers(self):
        """The walk stops ON an out-of-range viewer, so it is always in the pre-filter list."""
        odd = _viewer_row("Odd Caption", "https://www.linkedin.com/in/odd")
        recent = _viewer_row("Recent Viewer", "https://www.linkedin.com/in/recent")
        old = _viewer_row("Old Viewer", "https://www.linkedin.com/in/old")

        long_ago = datetime.now() - timedelta(days=30)
        # walk reads the last row, then the filter reads all three in order.
        dates = [long_ago, ValueError("invalid datetime as string: "), datetime.now(), long_ago]

        with patch(f"{_MOD}.get_current_profile", return_value=_profile_pair()), \
             patch(f"{_MOD}.get_elements_as_list_wait_stale", return_value=[odd, recent, old]), \
             patch(f"{_MOD}.convert_viewed_on_to_date", side_effect=dates), \
             patch(f"{_MOD}.time.sleep"), \
             patch(f"{_MOD}.get_user_id", return_value=1), \
             patch(f"{_MOD}.close_tab"), \
             patch(f"{_MOD}.quit_gracefully"), \
             patch(f"{_MOD}.log_warning") as mock_warning, \
             patch(f"{_MOD}.log_error") as mock_error, \
             patch(f"{_MOD}.engage_with_profile_viewer") as mock_engage:
            from cqc_lem.app.run_automation import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        mock_error.assert_not_called()
        mock_warning.assert_called_once()
        assert "Engaged with 1 viewers" in result
        assert _engaged_urls(mock_engage) == ["https://www.linkedin.com/in/recent"]
