"""Unit tests for `get_post_quality_rows` — the observation source of the cost-aware routing
experiment (issue #494)."""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_all=None, error=None):
    conn, cur = MagicMock(), MagicMock()
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    if error:
        cur.execute.side_effect = mysql.connector.Error(error)
    conn.cursor.return_value = cur
    return conn, cur


def _row(**overrides):
    row = {"user_id": 1, "post_id": 9, "day": date(2026, 7, 20), "authenticity_score": 82,
           "reactions": 5, "comments": 2, "reposts": 1, "impressions": 900}
    row.update(overrides)
    return row


class TestGetPostQualityRows:
    def test_returns_every_users_latest_stat_row(self):
        conn, cur = _mock_conn(fetch_all=[_row(), _row(user_id=2, post_id=10)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            rows = get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))
        assert [r["user_id"] for r in rows] == [1, 2]
        assert rows[0] == {"user_id": 1, "post_id": 9, "day": "2026-07-20", "reactions": 5,
                           "comments": 2, "reposts": 1, "impressions": 900,
                           "authenticity_score": 82}
        sql, params = cur.execute.call_args[0]
        assert "status='posted'" in sql
        assert "MAX(id) FROM post_stats" in sql  # latest stats row per post
        assert params == (date(2026, 7, 1), date(2026, 8, 1))

    def test_unknown_impressions_and_scores_stay_none(self):
        """A post with no impressions or no authenticity score is UNKNOWN quality, never zero —
        scoring it as 0 would drag an arm's mean down for a missing measurement."""
        conn, _ = _mock_conn(fetch_all=[_row(impressions=None, authenticity_score=None)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            rows = get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))
        assert rows[0]["impressions"] is None
        assert rows[0]["authenticity_score"] is None

    def test_zero_impressions_read_as_unknown(self):
        conn, _ = _mock_conn(fetch_all=[_row(impressions=0)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))[0]["impressions"] is None

    def test_datetime_day_is_serialized(self):
        conn, _ = _mock_conn(fetch_all=[_row(day=datetime(2026, 7, 20, 9, 30))])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))[0]["day"] \
                .startswith("2026-07-20")

    def test_db_error_degrades_to_empty(self):
        conn, _ = _mock_conn(error="table missing")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1)) == []
