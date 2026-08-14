"""Unit tests for `get_post_quality_rows` — the observation source of the cost-aware routing
experiment (issue #494).
"""

from datetime import date, datetime
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _row(**overrides):
    row = {"user_id": 1, "post_id": 9, "day": date(2026, 7, 20), "authenticity_score": 82,
           "reactions": 5, "comments": 2, "reposts": 1, "impressions": 900}
    row.update(overrides)
    return row


class TestGetPostQualityRows:
    def test_returns_every_users_latest_stat_row(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[_row(), _row(user_id=2, post_id=10)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
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

    def test_unknown_impressions_and_scores_stay_none(self, fake_cursor):
        """A post with no impressions or no authenticity score is UNKNOWN quality, never zero —
        scoring it as 0 would drag an arm's mean down for a missing measurement.
        """
        conn, _ = fake_cursor(fetch_all=[_row(impressions=None, authenticity_score=None)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            rows = get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))
        assert rows[0]["impressions"] is None
        assert rows[0]["authenticity_score"] is None

    def test_zero_impressions_read_as_unknown(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[_row(impressions=0)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))[0]["impressions"] is None

    def test_datetime_day_is_serialized(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[_row(day=datetime(2026, 7, 20, 9, 30))])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1))[0]["day"] \
                .startswith("2026-07-20")

    def test_db_error_degrades_to_empty(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("table missing"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_quality_rows
            assert get_post_quality_rows(date(2026, 7, 1), date(2026, 8, 1)) == []
