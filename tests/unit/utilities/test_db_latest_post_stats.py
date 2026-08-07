"""Unit tests for get_latest_post_stats — the scrape's own numbers, read back for the #645 probe."""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.utilities.db.get_db_connection"


class TestGetLatestPostStats:
    def test_returns_the_latest_capture(self, mock_database_connection):
        row = {"reactions": 12, "comments": 3, "reposts": 1, "impressions": 72, "saves": 4}
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = row
            from cqc_lem.utilities.db import get_latest_post_stats
            assert get_latest_post_stats(1, 9) == row
        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "ORDER BY id DESC" in sql and "LIMIT 1" in sql

    def test_no_capture_yet_is_none(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_latest_post_stats
            assert get_latest_post_stats(1, 9) is None

    def test_a_db_error_is_none_not_a_raise(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
            from cqc_lem.utilities.db import get_latest_post_stats
            assert get_latest_post_stats(1, 9) is None

    def test_scoped_to_the_owning_user(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_latest_post_stats
            get_latest_post_stats(7, 42)
        assert mock_database_connection["cursor"].execute.call_args[0][1] == (7, 42)
