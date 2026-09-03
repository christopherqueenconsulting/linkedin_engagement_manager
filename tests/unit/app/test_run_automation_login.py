"""Unit tests for LinkedIn login / get_current_profile error handling in automation tasks.

These tests verify that a LinkedIn challenge or TimeoutException during login does
not propagate as an "unexpected" Celery failure — each task must catch the error
from get_current_profile and return a descriptive string instead.
"""

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import TimeoutException

pytestmark = pytest.mark.unit

# This file drives FIVE tasks that no longer share a module (#1154), so three spellings are live
# here and each class uses the one its own task reads: the two profile-viewer tasks from
# `app.engagement.outreach`, `automate_commenting` from `app.engagement.feed`, and
# `automate_reply_commenting` + `update_stale_profile` from `app.engagement.posting`.
_OUT = "cqc_lem.app.engagement.outreach"
_PATCH_GET_PROFILE = f"{_OUT}.get_current_profile"
_PATCH_LOG_ERROR = f"{_OUT}.log_error"
_PATCH_LOG_WARNING = f"{_OUT}.log_warning"
_FEED = "cqc_lem.app.engagement.feed"
_FEED_GET_PROFILE = f"{_FEED}.get_current_profile"
_FEED_LOG_ERROR = f"{_FEED}.log_error"
_POST = "cqc_lem.app.engagement.posting"
_POST_GET_PROFILE = f"{_POST}.get_current_profile"
_POST_LOG_ERROR = f"{_POST}.log_error"


def _linkedin_challenge_error() -> RuntimeError:
    return RuntimeError("Unsolvable LinkedIn challenge at post-cookie-load: https://www.linkedin.com/uas/login")


def _timeout_error() -> TimeoutException:
    return TimeoutException("Finding Username Field")


# ---------------------------------------------------------------------------
# automate_commenting
# ---------------------------------------------------------------------------

class TestAutomateCommentingLoginError:
    def test_returns_error_string_on_runtime_error(self):
        """automate_commenting returns a failure string (not raise) when login challenge occurs."""
        with patch(_FEED_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_FEED_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.feed import automate_commenting

            result = automate_commenting.run(user_id=1)

        assert "Failed to start auto commenting" in result
        mock_log.assert_called_once()

    def test_returns_error_string_on_timeout_exception(self):
        """automate_commenting returns a failure string when username field is not found."""
        with patch(_FEED_GET_PROFILE, side_effect=_timeout_error()), \
             patch(_FEED_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.feed import automate_commenting

            result = automate_commenting.run(user_id=1)

        assert "Failed to start auto commenting" in result
        mock_log.assert_called_once()

    def test_does_not_call_quit_gracefully_on_profile_failure(self):
        """When get_current_profile raises, the already-closed driver is not quit again."""
        with patch(_FEED_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_FEED_LOG_ERROR), \
             patch(f"{_FEED}.quit_gracefully") as mock_quit:
            from cqc_lem.app.engagement.feed import automate_commenting

            automate_commenting.run(user_id=1)

        mock_quit.assert_not_called()


# ---------------------------------------------------------------------------
# automate_reply_commenting
# ---------------------------------------------------------------------------

class TestAutomateReplyCommentingLoginError:
    def test_returns_error_string_on_runtime_error(self):
        """automate_reply_commenting returns a failure string when login challenge occurs."""
        with patch(_POST_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_POST_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.posting import automate_reply_commenting

            result = automate_reply_commenting.run(user_id=1, post_id=42)

        assert "Failed to start reply commenting" in result
        mock_log.assert_called_once()

    def test_returns_error_string_on_timeout_exception(self):
        """automate_reply_commenting returns a failure string when username field times out."""
        with patch(_POST_GET_PROFILE, side_effect=_timeout_error()), \
             patch(_POST_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.posting import automate_reply_commenting

            result = automate_reply_commenting.run(user_id=1, post_id=42)

        assert "Failed to start reply commenting" in result
        mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# automate_profile_viewer_engagement
# ---------------------------------------------------------------------------

class TestAutomateProfileViewerEngagementLoginError:
    def test_returns_error_string_on_runtime_error(self):
        """automate_profile_viewer_engagement returns error string instead of re-raising."""
        with patch(_PATCH_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_PATCH_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        assert "Failed to start profile viewer engagement" in result
        mock_log.assert_called_once()

    def test_returns_error_string_on_timeout_exception(self):
        """automate_profile_viewer_engagement returns error string on TimeoutException."""
        with patch(_PATCH_GET_PROFILE, side_effect=_timeout_error()), \
             patch(_PATCH_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement

            result = automate_profile_viewer_engagement.run(user_id=1)

        assert "Failed to start profile viewer engagement" in result
        mock_log.assert_called_once()

    def test_does_not_raise(self):
        """automate_profile_viewer_engagement must never raise — even on LinkedIn challenge."""
        with patch(_PATCH_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_PATCH_LOG_ERROR):
            from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement

            try:
                automate_profile_viewer_engagement.run(user_id=1)
            except Exception as exc:
                pytest.fail(f"Task raised unexpectedly: {exc!r}")

    def test_rate_limited_getting_profile_logs_warning_not_error(self):
        # Issue #1943: `LinkedInRateLimited` (429 breaker, manual pause, or a per-account
        # challenge-unsolvable cooldown per #1920) is a known, self-clearing back-off — every
        # sibling task in this module (`send_lead_response`, `process_user_followups`, …) already
        # special-cases it (log_warning, no capture). This task's generic `except Exception` was
        # missing that catch, so every breaker trip during the profile-viewer walk fell through and
        # re-filed an ERROR ($exception) for a condition `get_current_profile` already downgraded
        # and expects to clear on its own.
        from cqc_lem.app.engagement.outreach import automate_profile_viewer_engagement
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        breaker = LinkedInRateLimited("LinkedIn login challenge for this account was unsolvable")
        with patch(_PATCH_GET_PROFILE, side_effect=breaker), \
             patch(_PATCH_LOG_WARNING) as warn, \
             patch(_PATCH_LOG_ERROR) as err:
            result = automate_profile_viewer_engagement.run(user_id=1)

        assert "Skipped" in result
        warn.assert_called_once()
        assert warn.call_args.kwargs.get("exc") is breaker
        err.assert_not_called()


# ---------------------------------------------------------------------------
# engage_with_profile_viewer
# ---------------------------------------------------------------------------

class TestUpdateStaleProfileLoginError:
    def test_defers_on_challenge_unsolvable_cooldown_instead_of_filing_an_error(self):
        """update_stale_profile defers on LinkedInRateLimited instead of filing an ERROR.

        A LinkedInRateLimited (429 breaker / manual pause / challenge-unsolvable cooldown, issue
        #1920) is an expected, self-clearing back-off — issue #1946 saw this task file a fresh
        ERROR on every occurrence instead of the WARNING every sibling task already gives it.
        """
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited

        with patch(_POST_GET_PROFILE,
                   side_effect=LinkedInRateLimited(
                       "LinkedIn login challenge for this account was unsolvable and is cooling "
                       "down for ~11244s before the next attempt.")), \
             patch(_POST_LOG_ERROR) as mock_error, \
             patch(f"{_POST}.log_warning") as mock_warning:
            from cqc_lem.app.engagement.posting import update_stale_profile

            result = update_stale_profile.run(user_id=1)

        assert "Skipped — rate limited" in result
        mock_error.assert_not_called()
        mock_warning.assert_called_once()

    def test_returns_error_string_on_login_challenge(self):
        """update_stale_profile returns error string instead of raising when login fails."""
        with patch(_POST_GET_PROFILE, side_effect=RuntimeError("Unsolvable LinkedIn challenge")), \
             patch(_POST_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.posting import update_stale_profile

            result = update_stale_profile.run(user_id=1)

        assert "Failed to update profile" in result
        mock_log.assert_called_once()

    def test_returns_error_string_on_timeout(self):
        """update_stale_profile returns error string on TimeoutException."""
        from selenium.common.exceptions import TimeoutException
        with patch(_POST_GET_PROFILE, side_effect=TimeoutException("Finding Username Field")), \
             patch(_POST_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.posting import update_stale_profile

            result = update_stale_profile.run(user_id=1)

        assert "Failed to update profile" in result
        mock_log.assert_called_once()

    def test_quits_driver_on_success(self):
        """update_stale_profile calls quit_gracefully when get_current_profile succeeds."""
        mock_driver = MagicMock()
        with patch(_POST_GET_PROFILE, return_value=(mock_driver, MagicMock(), "u@e.com", MagicMock())), \
             patch(f"{_POST}.synthesize_profile", return_value=""), \
             patch(f"{_POST}.quit_gracefully") as mock_quit:
            from cqc_lem.app.engagement.posting import update_stale_profile

            result = update_stale_profile.run(user_id=1)

        mock_quit.assert_called_once_with(mock_driver)
        assert result == "Profile Updated Successfully"

    def test_the_daily_sweep_still_honours_the_profile_cache(self):
        """No `force_refresh` means the beat behaves exactly as it did before issue #1076."""
        with patch(_POST_GET_PROFILE,
                   return_value=(MagicMock(), MagicMock(), "u@e.com", MagicMock())) as get_profile, \
             patch(f"{_POST}.synthesize_profile", return_value=""), \
             patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import update_stale_profile

            update_stale_profile.run(user_id=1)

        assert get_profile.call_args.kwargs["force_refresh"] is False

    def test_an_on_demand_refresh_bypasses_the_profile_cache(self):
        """The whole point of the button (issue #1076).

        A profile cached this morning must NOT be read back when the user edited it a minute ago.
        """
        with patch(_POST_GET_PROFILE,
                   return_value=(MagicMock(), MagicMock(), "u@e.com", MagicMock())) as get_profile, \
             patch(f"{_POST}.synthesize_profile", return_value="voice brief"), \
             patch(f"{_POST}.set_profile_synthesis", return_value=True) as set_synth, \
             patch(f"{_POST}.quit_gracefully"):
            from cqc_lem.app.engagement.posting import update_stale_profile

            result = update_stale_profile.run(user_id=1, force_refresh=True)

        assert get_profile.call_args.kwargs["force_refresh"] is True
        # A fresh scrape is only half the job — the voice brief every generation prompt reads has
        # to be re-distilled from it, or the new headline never reaches the writing.
        set_synth.assert_called_once_with(1, "voice brief")
        assert result == "Profile Updated Successfully"


class TestEngageWithProfileViewerLoginError:
    def test_returns_error_string_on_runtime_error(self):
        """engage_with_profile_viewer returns error string when login challenge occurs."""
        with patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False), \
             patch(_PATCH_GET_PROFILE, side_effect=_linkedin_challenge_error()), \
             patch(_PATCH_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.outreach import engage_with_profile_viewer

            result = engage_with_profile_viewer.run(
                user_id=1, viewer_url="https://linkedin.com/in/test", viewer_name="Test User"
            )

        assert "Failed to start profile viewer engagement" in result
        mock_log.assert_called_once()

    def test_returns_error_string_on_timeout_exception(self):
        """engage_with_profile_viewer returns error string on TimeoutException."""
        with patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=False), \
             patch(_PATCH_GET_PROFILE, side_effect=_timeout_error()), \
             patch(_PATCH_LOG_ERROR) as mock_log:
            from cqc_lem.app.engagement.outreach import engage_with_profile_viewer

            result = engage_with_profile_viewer.run(
                user_id=1, viewer_url="https://linkedin.com/in/test", viewer_name="Test User"
            )

        assert "Failed to start profile viewer engagement" in result
        mock_log.assert_called_once()

    def test_skips_login_when_already_engaged_today(self):
        """If already engaged today, get_current_profile is never called."""
        with patch(f"{_OUT}.has_engaged_url_with_x_days", return_value=True), \
             patch(_PATCH_GET_PROFILE) as mock_profile:
            from cqc_lem.app.engagement.outreach import engage_with_profile_viewer

            result = engage_with_profile_viewer.run(
                user_id=1, viewer_url="https://linkedin.com/in/test", viewer_name="Test User"
            )

        mock_profile.assert_not_called()
        assert "today" in result.lower()
