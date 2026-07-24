"""Unit tests for db.get_post_performance_rows — the per-post source for the analytics
dashboard (issue #395)."""

import mysql.connector
import pytest
from datetime import datetime

pytestmark = pytest.mark.unit


class TestGetPostPerformanceRows:
    def test_maps_rows_to_attribution_dicts(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = [
            (9, datetime(2026, 7, 20, 14, 0), 42, 7, 3, 900, 5,
             "case_study", "bold_claim", "carousel", "AI onboarding", "consideration"),
        ]
        from cqc_lem.utilities.db import get_post_performance_rows
        rows = get_post_performance_rows(1)
        assert rows == [{
            "post_id": 9, "scheduled_time": datetime(2026, 7, 20, 14, 0),
            "reactions": 42, "comments": 7, "reposts": 3, "impressions": 900, "saves": 5,
            "archetype": "case_study", "hook_style": "bold_claim", "format": "carousel",
            "topic": "AI onboarding", "buyer_stage": "consideration"}]

    def test_no_window_filter_by_default(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = []
        from cqc_lem.utilities.db import get_post_performance_rows
        get_post_performance_rows(42)
        sql, params = cursor.execute.call_args[0]
        assert "INTERVAL" not in sql
        assert "status='posted'" in sql
        assert params == (42, 42)

    def test_windows_by_days_when_given(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.fetchall.return_value = []
        from cqc_lem.utilities.db import get_post_performance_rows
        get_post_performance_rows(42, days=30)
        sql, params = cursor.execute.call_args[0]
        assert "INTERVAL %s DAY" in sql
        assert params == (42, 42, 30)

    def test_db_error_returns_empty(self, mock_database_connection):
        cursor = mock_database_connection["cursor"]
        cursor.execute.side_effect = mysql.connector.Error("boom")
        from cqc_lem.utilities.db import get_post_performance_rows
        assert get_post_performance_rows(1) == []
