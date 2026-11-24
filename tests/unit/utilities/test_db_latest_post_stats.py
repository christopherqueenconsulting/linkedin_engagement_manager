"""Unit tests for get_latest_post_stats — the scrape's own numbers, read back for the #645 probe."""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestGetLatestPostStats:
    def test_returns_the_latest_capture(self, mock_database_connection):
        row = {"reactions": 12, "comments": 3, "reposts": 1, "impressions": 72, "saves": 4}
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = row
            from cqc_lem.utilities.db import get_latest_post_stats
            assert get_latest_post_stats(1, 9) == row
        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "ORDER BY (source='api') DESC, id DESC" in sql and "LIMIT 1" in sql

    def test_record_post_stats_tags_source(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import record_post_stats
            assert record_post_stats(1, 9, 2, 1, source="api")
            assert record_post_stats(1, 9, 2, 1)
        calls = [c for c in mock_database_connection["cursor"].execute.call_args_list
                 if "INSERT INTO post_stats" in c[0][0]]
        assert calls[0][0][1][-1] == "api"
        assert calls[1][0][1][-1] == "scrape"

    def test_record_post_stats_rejects_unknown_source(self):
        from cqc_lem.utilities.db import record_post_stats
        with pytest.raises(ValueError):
            record_post_stats(1, 9, 2, 1, source="merged")

    def test_readers_prefer_the_api_row(self):
        from cqc_lem.platform.db.repositories import posts
        assert "(source='api') DESC" in posts._LATEST_STATS_IDS
        assert posts._LATEST_STATS_IDS.count("%s") == 1
        assert "%s" not in posts._LATEST_STATS_IDS_ALL_USERS

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
