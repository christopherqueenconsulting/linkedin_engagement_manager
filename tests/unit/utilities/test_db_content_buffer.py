"""Unit tests for the bounded rolling content-buffer DB helpers (issue #544)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestCountReadyPostsWithinBuffer:
    def test_counts_generated_posts_due_in_window(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(3,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import READY_POST_STATUSES, count_ready_posts_within_buffer
            assert count_ready_posts_within_buffer(1, 5) == 3
        sql, params = cur.execute.call_args[0]
        assert "scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY" in sql
        assert params == (1, *READY_POST_STATUSES, 5)

    def test_planning_posts_are_not_counted_as_ready(self):
        from cqc_lem.utilities.db import READY_POST_STATUSES
        assert "planning" not in READY_POST_STATUSES
        # pending/approved/scheduled all already have content and must not be re-generated
        assert set(READY_POST_STATUSES) == {"pending", "approved", "scheduled"}

    def test_zero_when_no_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_ready_posts_within_buffer
            assert count_ready_posts_within_buffer(1) == 0

    def test_zero_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_ready_posts_within_buffer
            assert count_ready_posts_within_buffer(1, 5) == 0


class TestGetPlannedPostsWithinBuffer:
    _ROWS = [{"user_id": 1, "id": 9, "post_type": "text", "buyer_stage": "awareness"}]

    def test_selects_forward_window_ordered_soonest_first(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=self._ROWS)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, 0) == self._ROWS
        sql, params = cur.execute.call_args[0]
        assert "status = 'planning'" in sql
        assert "scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY" in sql
        assert "ORDER BY scheduled_time ASC" in sql
        # No YEARWEEK anywhere — the calendar-week window is what left the mid-week gap
        assert "YEARWEEK" not in sql
        assert params == (1, 5, 5)

    def test_limit_is_only_the_delta_to_the_cap(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=self._ROWS)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            get_planned_posts_within_buffer(1, 5, 5, 3)
        _, params = cur.execute.call_args[0]
        assert params[2] == 2

    @pytest.mark.parametrize("already_ready", [5, 6, 99])
    def test_full_buffer_queries_nothing(self, already_ready, fake_cursor):
        conn, cur = fake_cursor(fetch_all=self._ROWS)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn) as get_conn:
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, already_ready) == []
        get_conn.assert_not_called()
        cur.execute.assert_not_called()

    def test_empty_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, 0) == []


class TestGetNextPlannedPostsAfterBuffer:
    """The pull-forward list for an explicitly requested run (issue #719)."""
    _ROWS = [{"user_id": 1, "id": 31, "post_type": "text", "buyer_stage": "awareness"}]

    def test_selects_only_slots_beyond_the_window(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=self._ROWS)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_planned_posts_after_buffer
            assert get_next_planned_posts_after_buffer(1, 5, 4) == self._ROWS
        sql, params = cur.execute.call_args[0]
        assert "status = 'planning'" in sql
        assert "scheduled_time > NOW() + INTERVAL %s DAY" in sql
        assert "ORDER BY scheduled_time ASC" in sql
        assert "scheduled_time FROM posts" in sql  # the day-type key rides along (issue #621)
        assert params == (1, 5, 4)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_no_budget_queries_nothing(self, limit, fake_cursor):
        conn, cur = fake_cursor(fetch_all=self._ROWS)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn) as get_conn:
            from cqc_lem.utilities.db import get_next_planned_posts_after_buffer
            assert get_next_planned_posts_after_buffer(1, 5, limit) == []
        get_conn.assert_not_called()
        cur.execute.assert_not_called()

    def test_empty_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_planned_posts_after_buffer
            assert get_next_planned_posts_after_buffer(1, 5, 4) == []


class TestGetNextPlannedPostDate:
    def test_returns_the_soonest_upcoming_planning_slot(self, fake_cursor):
        import datetime as dt
        due = dt.datetime(2026, 8, 3, 13, 30)
        conn, cur = fake_cursor(fetch_one=(due,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_planned_post_date
            assert get_next_planned_post_date(1) == due
        sql, params = cur.execute.call_args[0]
        assert "MIN(scheduled_time)" in sql
        assert "status = 'planning'" in sql
        assert "scheduled_time > NOW()" in sql
        assert params == (1,)

    def test_none_when_nothing_is_planned(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_planned_post_date
            assert get_next_planned_post_date(1) is None

    def test_none_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_next_planned_post_date
            assert get_next_planned_post_date(1) is None


class TestGetUserIdsWithPlannedPostsWithinBuffer:
    def test_returns_distinct_user_ids(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(4,), (9,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_with_planned_posts_within_buffer
            assert get_user_ids_with_planned_posts_within_buffer(30) == [4, 9]
        sql, params = cur.execute.call_args[0]
        assert "SELECT DISTINCT user_id FROM posts" in sql
        assert params == (30,)

    def test_defaults_to_the_max_window_so_long_buffers_are_not_missed(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import MAX_CONTENT_BUFFER_DAYS, get_user_ids_with_planned_posts_within_buffer
            get_user_ids_with_planned_posts_within_buffer()
        assert cur.execute.call_args[0][1] == (MAX_CONTENT_BUFFER_DAYS,)

    def test_empty_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_with_planned_posts_within_buffer
            assert get_user_ids_with_planned_posts_within_buffer() == []


class TestUserPreferencesBufferColumns:
    def test_defaults_include_buffer_knobs(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (
                DEFAULT_CONTENT_BUFFER_DAYS,
                DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                get_user_preferences,
            )
            prefs = get_user_preferences(1)
        assert prefs["content_buffer_days"] == DEFAULT_CONTENT_BUFFER_DAYS
        assert prefs["content_buffer_max_posts"] == DEFAULT_CONTENT_BUFFER_MAX_POSTS

    def test_row_values_are_returned(self, fake_cursor):
        row = {"last_login_inactivate_delay": 90, "auto_schedule_posts": 1,
               "content_buffer_days": 7, "content_buffer_max_posts": 3}
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_preferences
            assert get_user_preferences(1) == row
        assert "content_buffer_days" in cur.execute.call_args[0][0]

    def test_update_leaves_buffer_untouched_when_not_supplied(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, 90, True) is True
        sql, params = cur.execute.call_args[0]
        assert "content_buffer" not in sql
        assert params == (90, 1, 1)

    def test_update_persists_buffer_values(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, None, False, content_buffer_days=7,
                                           content_buffer_max_posts=4) is True
        sql, params = cur.execute.call_args[0]
        assert "content_buffer_days = %s" in sql and "content_buffer_max_posts = %s" in sql
        assert params == (None, 0, 7, 4, 1)

    def test_update_clamps_out_of_range_buffer_values(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import MAX_CONTENT_BUFFER_DAYS, MAX_CONTENT_BUFFER_POSTS, update_user_preferences
            update_user_preferences(1, None, True, content_buffer_days=999,
                                    content_buffer_max_posts=0)
        params = cur.execute.call_args[0][1]
        assert params[2] == MAX_CONTENT_BUFFER_DAYS
        assert params[3] == 1
        assert MAX_CONTENT_BUFFER_POSTS >= 1

    def test_update_false_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, 90, True, content_buffer_days=5) is False
