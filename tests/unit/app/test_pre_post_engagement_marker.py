"""Unit tests for the per-post pre-post engagement marker in automate_commenting (issue #547)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.run_automation"


@pytest.fixture
def commenting_env():
    """Patch away Selenium/LinkedIn so only the marker wiring is under test."""
    with patch(f"{_MOD}.get_current_profile", return_value=(MagicMock(), MagicMock(), "a@b.c", MagicMock())), \
         patch(f"{_MOD}.navigate_to_feed"), \
         patch(f"{_MOD}.quit_gracefully"), \
         patch(f"{_MOD}.comment_on_feed_inline", return_value=3) as feed, \
         patch(f"{_MOD}.record_pre_post_run") as record:
        yield feed, record


class TestPrePostEngagementMarker:
    def test_records_run_when_dispatched_for_a_post(self, commenting_env):
        _, record = commenting_env
        from cqc_lem.app.run_automation import automate_commenting

        automate_commenting.run(user_id=7, post_id=42)

        record.assert_called_once_with(42, 7, 3)

    def test_no_marker_for_the_daily_golden_hour_run(self, commenting_env):
        """The daily/golden-hour run has no post to attribute to — it must not write a marker."""
        _, record = commenting_env
        from cqc_lem.app.run_automation import automate_commenting

        automate_commenting.run(user_id=7)

        record.assert_not_called()

    def test_post_id_rides_the_self_requeue(self, commenting_env):
        """The window is walked by a self-requeueing loop, so post_id must survive each hop or
        only the first pass would be recorded."""
        from cqc_lem.app.run_automation import automate_commenting

        with patch.object(automate_commenting, "apply_async") as requeue:
            automate_commenting.run(user_id=7, loop_for_duration=900, post_id=42)

        requeue.assert_called_once()
        kwargs = requeue.call_args[1]["kwargs"]
        assert kwargs["post_id"] == 42
        assert kwargs["user_id"] == 7
        assert kwargs["loop_for_duration"] < 900

    def test_marker_not_written_when_another_run_holds_the_lock(self, commenting_env):
        _, record = commenting_env
        from cqc_lem.app.run_automation import automate_commenting

        with patch(f"{_MOD}.acquire_run_lock", return_value=None):
            result = automate_commenting.run(user_id=7, post_id=42)

        assert "Skipped" in result
        record.assert_not_called()
