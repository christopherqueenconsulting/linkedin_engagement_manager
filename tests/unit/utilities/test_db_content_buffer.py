"""Unit tests for the bounded rolling content-buffer DB helpers (issue #544)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_one=None, fetch_all=None, dict_cursor=False):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    conn.cursor.return_value = cur
    return conn, cur


class TestCountReadyPostsWithinBuffer:
    def test_counts_generated_posts_due_in_window(self):
        conn, cur = _mock_conn(fetch_one=(3,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_ready_posts_within_buffer, READY_POST_STATUSES
            assert count_ready_posts_within_buffer(1, 5) == 3
        sql, params = cur.execute.call_args[0]
        assert "scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY" in sql
        assert params == (1, *READY_POST_STATUSES, 5)

    def test_planning_posts_are_not_counted_as_ready(self):
        from cqc_lem.utilities.db import READY_POST_STATUSES
        assert "planning" not in READY_POST_STATUSES
        # pending/approved/scheduled all already have content and must not be re-generated
        assert set(READY_POST_STATUSES) == {"pending", "approved", "scheduled"}

    def test_zero_when_no_row(self):
        conn, _ = _mock_conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_ready_posts_within_buffer
            assert count_ready_posts_within_buffer(1) == 0

    def test_zero_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_ready_posts_within_buffer
            assert count_ready_posts_within_buffer(1, 5) == 0


class TestGetPlannedPostsWithinBuffer:
    _ROWS = [{"user_id": 1, "id": 9, "post_type": "text", "buyer_stage": "awareness"}]

    def test_selects_forward_window_ordered_soonest_first(self):
        conn, cur = _mock_conn(fetch_all=self._ROWS)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, 0) == self._ROWS
        sql, params = cur.execute.call_args[0]
        assert "status = 'planning'" in sql
        assert "scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY" in sql
        assert "ORDER BY scheduled_time ASC" in sql
        # No YEARWEEK anywhere — the calendar-week window is what left the mid-week gap
        assert "YEARWEEK" not in sql
        assert params == (1, 5, 5)

    def test_limit_is_only_the_delta_to_the_cap(self):
        conn, cur = _mock_conn(fetch_all=self._ROWS)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            get_planned_posts_within_buffer(1, 5, 5, 3)
        _, params = cur.execute.call_args[0]
        assert params[2] == 2

    @pytest.mark.parametrize("already_ready", [5, 6, 99])
    def test_full_buffer_queries_nothing(self, already_ready):
        conn, cur = _mock_conn(fetch_all=self._ROWS)
        with patch(f"{_DB}.get_db_connection", return_value=conn) as get_conn:
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, already_ready) == []
        get_conn.assert_not_called()
        cur.execute.assert_not_called()

    def test_empty_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_posts_within_buffer
            assert get_planned_posts_within_buffer(1, 5, 5, 0) == []


class TestGetUserIdsWithPlannedPostsWithinBuffer:
    def test_returns_distinct_user_ids(self):
        conn, cur = _mock_conn(fetch_all=[(4,), (9,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_with_planned_posts_within_buffer
            assert get_user_ids_with_planned_posts_within_buffer(30) == [4, 9]
        sql, params = cur.execute.call_args[0]
        assert "SELECT DISTINCT user_id FROM posts" in sql
        assert params == (30,)

    def test_defaults_to_the_max_window_so_long_buffers_are_not_missed(self):
        conn, cur = _mock_conn(fetch_all=[])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (get_user_ids_with_planned_posts_within_buffer,
                                              MAX_CONTENT_BUFFER_DAYS)
            get_user_ids_with_planned_posts_within_buffer()
        assert cur.execute.call_args[0][1] == (MAX_CONTENT_BUFFER_DAYS,)

    def test_empty_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_with_planned_posts_within_buffer
            assert get_user_ids_with_planned_posts_within_buffer() == []


class TestUserPreferencesBufferColumns:
    def test_defaults_include_buffer_knobs(self):
        conn, _ = _mock_conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (get_user_preferences, DEFAULT_CONTENT_BUFFER_DAYS,
                                              DEFAULT_CONTENT_BUFFER_MAX_POSTS)
            prefs = get_user_preferences(1)
        assert prefs["content_buffer_days"] == DEFAULT_CONTENT_BUFFER_DAYS
        assert prefs["content_buffer_max_posts"] == DEFAULT_CONTENT_BUFFER_MAX_POSTS

    def test_row_values_are_returned(self):
        row = {"last_login_inactivate_delay": 90, "auto_schedule_posts": 1,
               "content_buffer_days": 7, "content_buffer_max_posts": 3}
        conn, cur = _mock_conn(fetch_one=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_preferences
            assert get_user_preferences(1) == row
        assert "content_buffer_days" in cur.execute.call_args[0][0]

    def test_update_leaves_buffer_untouched_when_not_supplied(self):
        conn, cur = _mock_conn()
        cur.rowcount = 1
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, 90, True) is True
        sql, params = cur.execute.call_args[0]
        assert "content_buffer" not in sql
        assert params == (90, 1, 1)

    def test_update_persists_buffer_values(self):
        conn, cur = _mock_conn()
        cur.rowcount = 1
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, None, False, content_buffer_days=7,
                                           content_buffer_max_posts=4) is True
        sql, params = cur.execute.call_args[0]
        assert "content_buffer_days = %s" in sql and "content_buffer_max_posts = %s" in sql
        assert params == (None, 0, 7, 4, 1)

    def test_update_clamps_out_of_range_buffer_values(self):
        conn, cur = _mock_conn()
        cur.rowcount = 1
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (update_user_preferences, MAX_CONTENT_BUFFER_DAYS,
                                              MAX_CONTENT_BUFFER_POSTS)
            update_user_preferences(1, None, True, content_buffer_days=999,
                                    content_buffer_max_posts=0)
        params = cur.execute.call_args[0][1]
        assert params[2] == MAX_CONTENT_BUFFER_DAYS
        assert params[3] == 1
        assert MAX_CONTENT_BUFFER_POSTS >= 1

    def test_update_false_on_db_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_preferences
            assert update_user_preferences(1, 90, True, content_buffer_days=5) is False
