"""Unit tests for get_planned_tasks and the default_video_quality getter/setter."""

import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn_fetchall(sides):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.side_effect = sides
    conn.cursor.return_value = cur
    return conn, cur


def _conn_fetchone(row):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn, cur


class TestGetPlannedTasks:
    def test_merges_and_labels_by_kind_sorted(self):
        t0 = datetime.datetime(2026, 7, 10, 9, 0)
        t1 = datetime.datetime(2026, 7, 11, 9, 0)
        t2 = datetime.datetime(2026, 7, 12, 9, 0)
        posts = [{"id": 1, "content": "Post body", "scheduled_time": t2, "status": "approved"}]
        dms = [{"id": 5, "recipient_name": "Alice", "message": "hi", "scheduled_time": t0,
                "status": "pending"}]
        editions = [{"id": 9, "title": "Weekly Roundup", "scheduled_for": t1, "status": "draft"}]
        conn, cur = _conn_fetchall([posts, dms, editions])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_tasks
            out = get_planned_tasks(1, limit=10)
        # soonest-first: DM (t0), Newsletter (t1), Post (t2)
        assert [t["kind"] for t in out] == ["DM", "Newsletter", "Post"]
        assert out[0]["title"] == "Alice"
        assert out[1]["title"] == "Weekly Roundup"
        assert out[2]["title"] == "Post body"
        # every query filters out terminal states and future-dates
        for call in cur.execute.call_args_list:
            sql = call[0][0]
            assert "UTC_TIMESTAMP()" in sql
        assert "posted" not in "".join(c[0][0] for c in cur.execute.call_args_list)

    def test_excludes_nothing_when_all_terminal(self):
        conn, _ = _conn_fetchall([[], [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_tasks
            assert get_planned_tasks(1) == []

    def test_uses_non_terminal_status_enums_for_posts(self):
        conn, cur = _conn_fetchall([[], [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import PostStatus, ScheduledDmStatus, get_planned_tasks
            get_planned_tasks(1)
        post_params = cur.execute.call_args_list[0][0][1]
        assert PostStatus.PENDING.value in post_params
        assert PostStatus.APPROVED.value in post_params
        assert PostStatus.SCHEDULED.value in post_params
        assert PostStatus.POSTED.value not in post_params
        dm_params = cur.execute.call_args_list[1][0][1]
        assert ScheduledDmStatus.SENT.value not in dm_params

    def test_limit_caps_merged_result(self):
        rows = [{"id": i, "content": f"p{i}",
                 "scheduled_time": datetime.datetime(2026, 7, 10 + i, 9, 0),
                 "status": "approved"} for i in range(3)]
        conn, _ = _conn_fetchall([rows, [], []])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_tasks
            out = get_planned_tasks(1, limit=2)
        assert len(out) == 2

    def test_db_error_returns_empty(self):
        import mysql.connector
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn.cursor.return_value = cur
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_planned_tasks
            assert get_planned_tasks(1) == []


class TestDefaultVideoQuality:
    def test_get_returns_stored_valid_value(self):
        conn, _ = _conn_fetchone({"default_video_quality": "premium"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_default_video_quality
            assert get_default_video_quality(1) == "premium"

    def test_get_defaults_standard_when_unset(self):
        conn, _ = _conn_fetchone(None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_default_video_quality
            assert get_default_video_quality(1) == "standard"

    def test_get_coerces_invalid_to_standard(self):
        conn, _ = _conn_fetchone({"default_video_quality": "bogus"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_default_video_quality
            assert get_default_video_quality(1) == "standard"

    def test_get_db_error_returns_standard(self):
        import mysql.connector
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn.cursor.return_value = cur
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_default_video_quality
            assert get_default_video_quality(1) == "standard"

    def test_set_valid_delegates_to_upsert(self):
        with patch(f"{_DB}.update_engagement_preferences", return_value=True) as up:
            from cqc_lem.utilities.db import set_default_video_quality
            assert set_default_video_quality(1, "premium") is True
        up.assert_called_once_with(1, {"default_video_quality": "premium"})

    def test_set_invalid_coerced_to_standard(self):
        with patch(f"{_DB}.update_engagement_preferences", return_value=True) as up:
            from cqc_lem.utilities.db import set_default_video_quality
            set_default_video_quality(1, "bogus")
        up.assert_called_once_with(1, {"default_video_quality": "standard"})
