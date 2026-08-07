"""Unit tests for db.get_shape_performance — the outcomes side of the performance→content
feedback loop (issue #389 / B4).
"""

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestGetShapePerformance:
    def test_aggregates_format_and_hook_buckets(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        # First execute → archetype (format) rows, second → hook_style rows.
        cursor.fetchall.side_effect = [
            [("how_to", 3, 30, 4, 1, 0, 0), ("contrarian_take", 2, 5, 0, 0, 0, 0)],
            [("bold_claim", 4, 40, 2, 0, 1200, 4)],
        ]
        from cqc_lem.utilities.db import get_shape_performance
        perf = get_shape_performance(7)
        assert perf["format"]["how_to"] == {
            "samples": 3, "reactions": 30, "comments": 4, "reposts": 1,
            "impressions": 0, "impression_samples": 0}
        assert perf["format"]["contrarian_take"]["samples"] == 2
        assert perf["hook"]["bold_claim"]["impression_samples"] == 4
        assert perf["hook"]["bold_claim"]["impressions"] == 1200
        assert cursor.execute.call_count == 2

    def test_empty_when_no_rows(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.side_effect = [[], []]
        from cqc_lem.utilities.db import get_shape_performance
        assert get_shape_performance(7) == {"format": {}, "hook": {}}

    def test_db_error_returns_empty_shape(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.execute.side_effect = mysql.connector.Error("boom")
        from cqc_lem.utilities.db import get_shape_performance
        assert get_shape_performance(7) == {"format": {}, "hook": {}}

    def test_query_filters_user_and_window(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.side_effect = [[], []]
        from cqc_lem.utilities.db import get_shape_performance
        get_shape_performance(42, days=30)
        sql, params = cursor.execute.call_args_list[0][0]
        assert "JOIN post_stats" in sql and "GROUP BY p.archetype" in sql
        assert params == (42, 30, 42)
