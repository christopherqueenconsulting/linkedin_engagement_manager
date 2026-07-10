"""Unit tests for sweep_reply_comments — the recent-posts reply sweep that replaces the 24h loop."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestSweepReplyComments:
    def test_sweeps_each_recent_post(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={"reply_max_post_age_days": 3}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10, 11, 12]) as grp, \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", return_value="Replied to 1 comments") as rep, \
             patch(f"{_RA}.quit_gracefully") as quit_:
            result = sweep_reply_comments.run(user_id=1)
        grp.assert_called_once_with(1, days=3)
        assert rep.call_count == 3
        assert "3/3" in result
        quit_.assert_called_once()

    def test_no_recent_posts_short_circuits_without_session(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[]), \
             patch(f"{_RA}.get_current_profile") as gcp:
            result = sweep_reply_comments.run(user_id=1)
        assert "No recent posts" in result
        gcp.assert_not_called()

    def test_rate_limited_session_returns_clean_skip(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_RA}.log_warning") as warn:
            result = sweep_reply_comments.run(user_id=1)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_one_post_failure_does_not_abort_sweep(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10, 11]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", side_effect=[Exception("boom"), "ok"]), \
             patch(f"{_RA}.log_warning"), \
             patch(f"{_RA}.quit_gracefully"):
            result = sweep_reply_comments.run(user_id=1)
        assert "1/2" in result  # first post errored, second succeeded
