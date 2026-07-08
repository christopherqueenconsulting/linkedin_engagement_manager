"""Unit tests for cookie auto-pruning (keep newest per user_id+name)."""

import mysql.connector
import pytest
from unittest.mock import patch

from cqc_lem.utilities.db import prune_superseded_cookies, store_cookies

pytestmark = pytest.mark.unit

_COOKIE = {
    "name": "li_at", "value": "x" * 40, "domain": ".linkedin.com",
    "path": "/", "secure": True, "httpOnly": True, "expiry": 123,
}


class TestPruneSupersededCookies:
    def test_deletes_older_duplicates_and_returns_count(self, mock_database_connection):
        cur = mock_database_connection["cursor"]
        cur.rowcount = 3
        deleted = prune_superseded_cookies(7)
        assert deleted == 3
        sql, params = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        assert "DELETE" in sql.upper() and "cookies" in sql
        # Only removes rows that have a strictly newer same-name sibling for the same user.
        assert "newer.updated_at > older.updated_at" in sql
        assert params == (7,)
        mock_database_connection["connection"].commit.assert_called()

    def test_swallows_db_error(self, mock_database_connection):
        mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("boom")
        assert prune_superseded_cookies(7) == 0  # never raises into the caller

    def test_store_cookies_triggers_prune(self, mock_database_connection):
        with patch("cqc_lem.utilities.db.get_user_id", return_value=9), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies") as prune:
            store_cookies("a@b.com", [_COOKIE])
            prune.assert_called_once_with(9)

    def test_store_cookies_skips_prune_when_user_unknown(self, mock_database_connection):
        with patch("cqc_lem.utilities.db.get_user_id", return_value=None), \
             patch("cqc_lem.utilities.db.prune_superseded_cookies") as prune:
            store_cookies("nobody@example.com", [_COOKIE])
            prune.assert_not_called()
