"""Unit tests for the no-post-day standalone commenting run."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RS = "cqc_lem.app.run_scheduler"
_DB = "cqc_lem.utilities.db"


class TestHasScheduledPostToday:
    def _conn(self, count):
        conn = MagicMock(); cur = MagicMock(); cur.fetchone.return_value = (count,)
        conn.cursor.return_value = cur
        return conn

    def test_true_when_post_today(self):
        with patch(f"{_DB}.get_db_connection", return_value=self._conn(1)):
            from cqc_lem.utilities.db import has_scheduled_post_today
            assert has_scheduled_post_today(1) is True

    def test_false_when_none(self):
        with patch(f"{_DB}.get_db_connection", return_value=self._conn(0)):
            from cqc_lem.utilities.db import has_scheduled_post_today
            assert has_scheduled_post_today(1) is False


class TestAutoDailyEngagement:
    def test_dispatches_daily_for_all_connected_users(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        # Runs every day now (no post-day skip); only the no-session user is excluded.
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u != 3), \
             patch(f"{_RS}.automate_commenting") as ac:
            result = auto_daily_engagement()
        assert ac.apply_async.call_count == 2                 # users 1 & 2 (3 has no session)
        assert "2/3" in result

    def test_golden_hour_stays_on_the_engage_lane(self):
        """Issue #553 gave the pre-post warm-up its own se_prepost lane by overriding queue= at
        that dispatch site only — the daily loop must keep falling through to the task's own
        se_engage queue, or it would crowd out the deadline-bound warm-ups."""
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1]), \
             patch(f"{_RS}.has_linkedin_session", return_value=True), \
             patch(f"{_RS}.automate_commenting") as ac:
            auto_daily_engagement()
        assert "queue" not in ac.apply_async.call_args[1]
