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
