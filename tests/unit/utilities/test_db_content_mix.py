"""Unit tests for the posts.content_mix DB helpers (issue #618): the mix class is written with the
planned post, read back for a regenerate, returned with the buffer's planned rows, and aggregated for
the analytics dashboard's mix-compliance ratio."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_all=None, fetch_one=None, rowcount=1):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    cur.fetchone.return_value = fetch_one
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


class TestInsertPlannedPost:
    def test_persists_the_mix_class(self):
        from cqc_lem.utilities.db import PostType, insert_planned_post
        conn, cur = _mock_conn()
        when = datetime(2026, 8, 1, 14, 0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert insert_planned_post(1, when, PostType.TEXT, "awareness",
                                       content_mix="promo") is True
        sql, params = cur.execute.call_args[0]
        assert "content_mix" in sql
        assert params == (when, "text", 1, "awareness", "promo", "planning", "TBD")

    def test_unclassified_post_stores_null(self):
        from cqc_lem.utilities.db import PostType, insert_planned_post
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            insert_planned_post(1, datetime(2026, 8, 1, 14, 0), PostType.VIDEO, "decision")
        assert cur.execute.call_args[0][1][4] is None


class TestGetPostContentMix:
    def test_returns_the_class(self):
        from cqc_lem.utilities.db import get_post_content_mix
        conn, cur = _mock_conn(fetch_one={"content_mix": "authority"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_post_content_mix(9) == "authority"
        assert cur.execute.call_args[0][1] == (9,)

    def test_none_for_unclassified_or_missing_post(self):
        from cqc_lem.utilities.db import get_post_content_mix
        conn, _ = _mock_conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_post_content_mix(9) is None

    def test_none_on_db_error(self):
        import mysql.connector
        from cqc_lem.utilities.db import get_post_content_mix
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_post_content_mix(9) is None


class TestPlannedPostsCarryTheMix:
    def test_selected_with_the_planned_rows(self):
        from cqc_lem.utilities.db import get_planned_posts_within_buffer
        rows = [{"user_id": 1, "id": 3, "post_type": "text", "buyer_stage": "awareness",
                 "content_mix": "promo"}]
        conn, cur = _mock_conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            out = get_planned_posts_within_buffer(1, days=7, max_posts=5)
        assert out[0]["content_mix"] == "promo"
        assert "content_mix" in cur.execute.call_args[0][0]


class TestGetContentMixCounts:
    def test_groups_by_class_and_buckets_nulls(self):
        from cqc_lem.utilities.db import get_content_mix_counts
        conn, cur = _mock_conn(fetch_all=[("value", 21), ("authority", 6), ("promo", 3), (None, 4)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            out = get_content_mix_counts(1)
        assert out == {"unclassified": 4, "value": 21, "authority": 6, "promo": 3}
        sql, params = cur.execute.call_args[0]
        # Rejected drafts were never part of the mix the audience saw.
        assert "status <> 'rejected'" in sql and "INTERVAL" not in sql
        assert params == (1,)

    def test_windows_on_days(self):
        from cqc_lem.utilities.db import get_content_mix_counts
        conn, cur = _mock_conn(fetch_all=[("value", 2)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            out = get_content_mix_counts(1, days=90)
        assert out["value"] == 2
        sql, params = cur.execute.call_args[0]
        assert "INTERVAL %s DAY" in sql and params == (1, 90)

    def test_empty_on_db_error(self):
        import mysql.connector
        from cqc_lem.utilities.db import get_content_mix_counts
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_content_mix_counts(1) == {"unclassified": 0}
