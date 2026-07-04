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
    def test_dispatches_only_for_no_post_connected_users(self):
        from cqc_lem.app.run_scheduler import auto_daily_engagement
        with patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.has_scheduled_post_today", side_effect=lambda u: u == 1), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda u: u != 3), \
             patch(f"{_RS}.automate_commenting") as ac:
            result = auto_daily_engagement()
        # user 1 has a post today (skip), user 3 has no session (skip) → only user 2 dispatched
        ac.apply_async.assert_called_once()
        assert ac.apply_async.call_args.kwargs["kwargs"]["user_id"] == 2
        assert "1/3" in result
